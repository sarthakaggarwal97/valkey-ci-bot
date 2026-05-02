"""Fix-validate loop — generate fix via Claude Code, push, dispatch CI, retry.

Orchestrates the core loop:
  1. Generate a fix via Claude Code CLI (reads repo files natively)
  2. Push the fix to a branch on the fork
  3. Dispatch daily.yml to validate the fix (if test file available)
  4. If validation fails, feed the failure back and retry
  5. If validation passes, return the diff + validation evidence
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scripts.agent_runtime import run_agent
from scripts.ci_validator import dispatch_validation, poll_run
from scripts.git_auth import GitAuth, github_https_url

if TYPE_CHECKING:
    from scripts.models import FailureReport, RootCauseReport

logger = logging.getLogger(__name__)


@dataclass
class FixResult:
    """Result of the fix-validate loop."""

    succeeded: bool
    patch: str
    validation_run_url: str
    attempts: int
    last_error: str = ""


def run_fix_loop(
    *,
    report: FailureReport,
    root_cause: RootCauseReport,
    fork_repo: str,
    fork_token: str,
    base_sha: str,
    test_file: str,
    job_name: str,
    repo_checkout: str = "",
    loop_count: int = 100,
    max_attempts: int = 3,
    issue_gh: Any = None,
    issue_repo: str = "",
    issue_number: int = 0,
    fix_generator: Any = None,
) -> FixResult:
    """Run the fix-validate loop using Claude Code CLI."""
    branch_name = f"bot/fix/{report.job_name[:40]}/{base_sha[:8]}"
    validation_error = ""
    last_patch = ""

    # Clone the repo once for Claude Code to read files from.
    own_tmpdir = None
    git_auth: GitAuth | None = None

    try:
        # GitAuth is needed for the push regardless of whether the caller
        # supplied their own checkout. Instantiate unconditionally so the
        # push branch below always has credentials via GIT_ASKPASS.
        git_auth = GitAuth(fork_token, prefix="fix-loop-git-askpass-")
        git_auth.__enter__()

        if not repo_checkout:
            own_tmpdir = tempfile.mkdtemp(prefix="valkey-fix-")
            _run(
                ["git", "clone", "--depth", "50", "--branch", "unstable", github_https_url(fork_repo), own_tmpdir],
                env=git_auth.env(),
            )
            repo_checkout = own_tmpdir

        for attempt in range(1, max_attempts + 1):
            logger.info(
                "Fix attempt %d/%d for %s (branch=%s).",
                attempt, max_attempts, report.job_name, branch_name,
            )

            # 1. Generate fix via Claude Code CLI
            patch = _generate_fix(
                report, root_cause, validation_error, repo_checkout,
            )
            if not patch:
                _comment_on_issue(
                    issue_gh, issue_repo, issue_number,
                    f"**Attempt {attempt}/{max_attempts}:** Claude returned no patch.",
                )
                continue

            last_patch = patch

            # 2. Commit and push the edited files directly from the checkout
            try:
                msg = f"[bot-fix] Fix {report.job_name} (attempt {attempt})"
                _run(["git", "config", "user.name", "valkey-ci-agent"], cwd=repo_checkout)
                _run(["git", "config", "user.email", "ci-agent@valkey.io"], cwd=repo_checkout)
                _run(["git", "checkout", "-B", branch_name], cwd=repo_checkout)
                _run(["git", "add", "-A"], cwd=repo_checkout)
                _run(["git", "commit", "-m", msg, "--allow-empty"], cwd=repo_checkout)
                _run(
                    ["git", "push", "--force", "origin", branch_name],
                    cwd=repo_checkout,
                    env=git_auth.env(),
                )
                logger.info("Pushed fix to %s/%s.", fork_repo, branch_name)
            except Exception as exc:
                logger.error("Push failed (attempt %d): %s", attempt, exc)
                _comment_on_issue(
                    issue_gh, issue_repo, issue_number,
                    f"**Attempt {attempt}/{max_attempts}:** Push failed: `{exc}`",
                )
                continue

            # 3. If no test file, return as unvalidated
            if not test_file:
                logger.info("No test file; returning fix as unvalidated.")
                _comment_on_issue(
                    issue_gh, issue_repo, issue_number,
                    f"**Attempt {attempt}/{max_attempts}:** Fix generated (no test file "
                    f"for CI validation). Branch: `{branch_name}`",
                )
                return FixResult(
                    succeeded=True, patch=patch,
                    validation_run_url="", attempts=attempt,
                )

            # 4. Dispatch validation
            run_id = dispatch_validation(
                token=fork_token, fork_repo=fork_repo,
                fix_branch=branch_name, job_name=job_name,
                test_file=test_file, loop_count=loop_count,
            )
            if run_id is None:
                _comment_on_issue(
                    issue_gh, issue_repo, issue_number,
                    f"**Attempt {attempt}/{max_attempts}:** CI dispatch failed.",
                )
                continue

            run_url = f"https://github.com/{fork_repo}/actions/runs/{run_id}"
            _comment_on_issue(
                issue_gh, issue_repo, issue_number,
                f"**Attempt {attempt}/{max_attempts}:** Validation dispatched.\n"
                f"- Branch: `{branch_name}`\n"
                f"- Run: {run_url}\n"
                f"- Test: `{test_file}` × {loop_count}",
            )

            # 5. Poll for result
            passed, conclusion, run_url = poll_run(fork_token, fork_repo, run_id)

            if passed:
                _comment_on_issue(
                    issue_gh, issue_repo, issue_number,
                    f"**Attempt {attempt}/{max_attempts}:** ✅ Validation passed! {run_url}",
                )
                return FixResult(
                    succeeded=True, patch=patch,
                    validation_run_url=run_url, attempts=attempt,
                )

            validation_error = f"Validation failed ({conclusion}). Run: {run_url}"
            msg = (
                f"**Attempt {attempt}/{max_attempts}:** ❌ Failed ({conclusion}). {run_url}"
            )
            if attempt < max_attempts:
                msg += "\nRetrying with failure context."
            else:
                msg += "\nAll attempts exhausted. Needs human attention."
            _comment_on_issue(issue_gh, issue_repo, issue_number, msg)

        return FixResult(
            succeeded=False, patch=last_patch,
            validation_run_url="", attempts=max_attempts,
            last_error=validation_error,
        )
    finally:
        if git_auth is not None:
            git_auth.cleanup()
        if own_tmpdir:
            import shutil
            shutil.rmtree(own_tmpdir, ignore_errors=True)


def _generate_fix(
    report: FailureReport,
    root_cause: RootCauseReport,
    validation_error: str,
    cwd: str,
) -> str | None:
    """Call Claude Code to edit files directly, then capture git diff.

    Instead of asking Claude to output a diff (which often has formatting
    issues), we let Claude use its Write tool to edit files in the checkout,
    then run ``git diff`` to get a clean, apply-compatible patch.
    """
    failure_desc = ""
    if report.parsed_failures:
        pf = report.parsed_failures[0]
        failure_desc = (
            f"Test: {pf.test_name or pf.failure_identifier}\n"
            f"File: {pf.file_path}\n"
            f"Error: {pf.error_message}\n"
        )
    else:
        failure_desc = f"Job: {report.job_name}\n"

    prompt = (
        f"You are fixing a CI test failure in the Valkey project (C key-value store).\n\n"
        f"Failure:\n{failure_desc}\n"
        f"Job: {report.job_name}\n"
        f"Root cause: {root_cause.description}\n"
        f"Files to investigate: {', '.join(root_cause.files_to_change) or 'unknown'}\n"
    )
    if validation_error:
        prompt += f"\nPrevious attempt failed:\n{validation_error}\n"
    prompt += (
        "\nRead the relevant source files, find the root cause, and EDIT "
        "the files directly to fix the issue. Use the Write tool to modify "
        "files in place. Make minimal changes following Valkey C coding style. "
        "Do NOT output a diff — just edit the files."
    )

    try:
        # Reset any previous changes
        _run(["git", "checkout", "."], cwd=cwd)

        # Let Claude edit files
        agent_result = run_agent("fix_generate_patch", prompt, cwd=cwd)
        logger.info(
            "Claude fix generation output (%d chars):\n%s",
            len(agent_result.stdout),
            agent_result.stdout[:1500],
        )
        if agent_result.returncode != 0:
            logger.warning(
                "Claude exited %d during fix generation; ignoring worktree edits.",
                agent_result.returncode,
            )
            return None

        patch = _capture_worktree_diff(cwd)
        if not patch:
            logger.warning("Claude edited no files (git diff empty).")
            return None
        logger.info("Claude produced a %d-line diff:\n%s", patch.count("\n"), patch[:1000])
        return patch
    except Exception as exc:
        logger.error("Claude Code failed: %s", exc)
        return None


def _push_patch_to_branch(
    repo: str, token: str, branch: str, base_sha: str,
    patch: str, commit_message: str,
) -> None:
    """Clone, apply patch, push to branch."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with GitAuth(token, prefix="fix-push-git-askpass-") as git_auth:
            git_env = git_auth.env()
            _run(
                ["git", "clone", "--depth", "1", "--branch", "unstable", github_https_url(repo), tmpdir],
                env=git_env,
            )
            _run(["git", "checkout", "-B", branch], cwd=tmpdir)

            patch_file = Path(tmpdir) / "fix.patch"
            patch_file.write_text(patch)
            result = subprocess.run(
                ["git", "apply", "--check", str(patch_file)],
                cwd=tmpdir, capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Patch doesn't apply: {result.stderr[:500]}")
            _run(["git", "apply", str(patch_file)], cwd=tmpdir)
            _run(["git", "add", "-A"], cwd=tmpdir)
            _run(["git", "commit", "-m", commit_message, "--allow-empty"], cwd=tmpdir)
            _run(["git", "push", "--force", "origin", branch], cwd=tmpdir, env=git_env)
        logger.info("Pushed fix to %s/%s.", repo, branch)


def _capture_worktree_diff(cwd: str) -> str:
    """Capture tracked and newly-created files as a unified patch."""
    subprocess.run(
        ["git", "add", "-N", "."],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _run(
    cmd: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}: {result.stderr[:500]}")


def _comment_on_issue(gh: Any, repo: str, issue_number: int, body: str) -> None:
    if not gh or not repo or not issue_number:
        return
    try:
        repo_obj = gh.get_repo(repo)
        issue = repo_obj.get_issue(issue_number)
        issue.create_comment(body)
    except Exception as exc:
        logger.warning("Failed to comment on issue #%d: %s", issue_number, exc)

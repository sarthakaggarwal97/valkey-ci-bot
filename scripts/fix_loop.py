"""Fix-validate loop — generate fix, push, dispatch CI, retry on failure.

Orchestrates the core loop:
  1. Generate a fix (via the existing FixGenerator agentic mode)
  2. Push the fix to a branch on the fork
  3. Dispatch daily.yml to validate the fix
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

from scripts.ci_validator import dispatch_validation, poll_run

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
    report: "FailureReport",
    root_cause: "RootCauseReport",
    fix_generator: Any,
    fork_repo: str,
    fork_token: str,
    base_sha: str,
    test_file: str,
    job_name: str,
    loop_count: int = 100,
    max_attempts: int = 3,
    issue_gh: Any = None,
    issue_repo: str = "",
    issue_number: int = 0,
) -> FixResult:
    """Run the fix-validate loop.

    For each attempt:
      1. Generate a patch via fix_generator.
      2. Push the patch to a branch on the fork.
      3. Dispatch daily.yml with the right skipjobs + test_args.
      4. Poll until the run completes.
      5. If passed, return success. If failed, retry with error context.

    Posts a comment on the tracking issue for each attempt.
    """
    branch_name = f"bot/fix/{report.job_name[:40]}/{base_sha[:8]}"
    validation_error = ""
    last_patch = ""

    for attempt in range(1, max_attempts + 1):
        logger.info(
            "Fix attempt %d/%d for %s (branch=%s).",
            attempt, max_attempts, report.job_name, branch_name,
        )

        # 1. Generate fix
        try:
            source_files: dict[str, str] = {}  # FixGenerator fetches its own files in agentic mode
            if validation_error:
                patch = fix_generator.generate(
                    root_cause, source_files,
                    validation_error=validation_error,
                    repo_ref=base_sha,
                )
            else:
                patch = fix_generator.generate(
                    root_cause, source_files,
                    repo_ref=base_sha,
                )
        except Exception as exc:
            logger.error("Fix generation failed (attempt %d): %s", attempt, exc)
            _comment_on_issue(
                issue_gh, issue_repo, issue_number,
                f"**Attempt {attempt}/{max_attempts}:** Fix generation failed: `{exc}`",
            )
            continue

        if not patch:
            logger.warning("Fix generator returned empty patch (attempt %d).", attempt)
            _comment_on_issue(
                issue_gh, issue_repo, issue_number,
                f"**Attempt {attempt}/{max_attempts}:** Fix generator returned no patch.",
            )
            continue

        last_patch = patch

        # 2. Push the patch to a branch on the fork
        try:
            _push_patch_to_branch(
                fork_repo, fork_token, branch_name, base_sha, patch,
                commit_message=f"[bot-fix] Fix {report.job_name} (attempt {attempt})",
            )
        except Exception as exc:
            logger.error("Push failed (attempt %d): %s", attempt, exc)
            _comment_on_issue(
                issue_gh, issue_repo, issue_number,
                f"**Attempt {attempt}/{max_attempts}:** Push failed: `{exc}`",
            )
            continue

        # 3. Dispatch validation
        run_id = dispatch_validation(
            token=fork_token,
            fork_repo=fork_repo,
            fix_branch=branch_name,
            job_name=job_name,
            test_file=test_file,
            loop_count=loop_count,
        )
        if run_id is None:
            logger.error("Dispatch failed (attempt %d).", attempt)
            _comment_on_issue(
                issue_gh, issue_repo, issue_number,
                f"**Attempt {attempt}/{max_attempts}:** CI dispatch failed.",
            )
            continue

        run_url = f"https://github.com/{fork_repo}/actions/runs/{run_id}"
        _comment_on_issue(
            issue_gh, issue_repo, issue_number,
            f"**Attempt {attempt}/{max_attempts}:** Fix generated, validation dispatched.\n"
            f"- Branch: `{branch_name}`\n"
            f"- Validation run: {run_url}\n"
            f"- Test: `{test_file}` × {loop_count}",
        )

        # 4. Poll for result
        passed, conclusion, run_url = poll_run(
            fork_token, fork_repo, run_id,
        )

        if passed:
            logger.info(
                "Validation passed on attempt %d for %s. Run: %s",
                attempt, report.job_name, run_url,
            )
            _comment_on_issue(
                issue_gh, issue_repo, issue_number,
                f"**Attempt {attempt}/{max_attempts}:** ✅ Validation passed!\n"
                f"- Run: {run_url}\n"
                f"- Conclusion: `{conclusion}`",
            )
            return FixResult(
                succeeded=True,
                patch=patch,
                validation_run_url=run_url,
                attempts=attempt,
            )

        # 5. Failed — capture error for retry
        validation_error = f"Validation failed (conclusion={conclusion}). Run: {run_url}"
        logger.warning(
            "Validation failed on attempt %d: %s. %s",
            attempt, conclusion, run_url,
        )
        _comment_on_issue(
            issue_gh, issue_repo, issue_number,
            f"**Attempt {attempt}/{max_attempts}:** ❌ Validation failed.\n"
            f"- Run: {run_url}\n"
            f"- Conclusion: `{conclusion}`\n"
            f"- Will retry with failure context." if attempt < max_attempts else
            f"- Conclusion: `{conclusion}`\n"
            f"- All attempts exhausted. Needs human attention.",
        )

    return FixResult(
        succeeded=False,
        patch=last_patch,
        validation_run_url="",
        attempts=max_attempts,
        last_error=validation_error,
    )


def _push_patch_to_branch(
    repo: str,
    token: str,
    branch: str,
    base_sha: str,
    patch: str,
    commit_message: str,
) -> None:
    """Clone the repo, apply the patch, push to the branch.

    Uses git CLI for simplicity — the GitHub Data API approach in
    PRManager is more complex and not needed here since we have
    a token with push access.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        _run(["git", "clone", "--depth", "1", "--branch", "unstable", clone_url, tmpdir])
        _run(["git", "checkout", "-B", branch], cwd=tmpdir)
        _run(["git", "reset", "--hard", base_sha], cwd=tmpdir)

        # Write patch to a file and apply
        patch_file = Path(tmpdir) / "fix.patch"
        patch_file.write_text(patch)
        result = subprocess.run(
            ["git", "apply", "--check", str(patch_file)],
            cwd=tmpdir, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Patch does not apply cleanly: {result.stderr[:500]}")
        _run(["git", "apply", str(patch_file)], cwd=tmpdir)
        _run(["git", "add", "-A"], cwd=tmpdir)
        _run(["git", "commit", "-m", commit_message, "--allow-empty"], cwd=tmpdir)
        _run(["git", "push", "--force", "origin", branch], cwd=tmpdir)
        logger.info("Pushed fix to %s/%s.", repo, branch)


def _run(cmd: list[str], cwd: str | None = None) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd[:4])}: {result.stderr[:500]}"
        )


def _comment_on_issue(
    gh: Any, repo: str, issue_number: int, body: str,
) -> None:
    """Post a comment on the tracking issue. Best-effort."""
    if not gh or not repo or not issue_number:
        return
    try:
        repo_obj = gh.get_repo(repo)
        issue = repo_obj.get_issue(issue_number)
        issue.create_comment(body)
    except Exception as exc:
        logger.warning("Failed to comment on issue #%d: %s", issue_number, exc)

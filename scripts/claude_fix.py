"""One-shot Claude Code fix: give it the raw CI log, get a fix back.

Replaces the separate root-cause-analysis + fix-generation steps with
a single Claude Code call that reads the log, understands the failure,
reads the source code, and edits the fix directly.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any

from scripts.claude_code import run_claude_code
from scripts.issue_tracker import create_or_update_issue
from scripts.models import ParsedFailure
from scripts.publish_guard import check_publish_allowed

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)


def fix_from_log(
    *,
    job_name: str,
    log_excerpt: str,
    parsed_failures: list[ParsedFailure],
    fork_repo: str,
    fork_token: str,
    base_sha: str,
    target_branch: str,
    run_url: str,
    gh: Github,
    job_id: int = 0,
    repo_full_name: str = "",
) -> dict[str, Any]:
    """One-shot: analyze the raw CI log + fix the code.

    1. Clone the repo
    2. Give Claude the raw log + repo: "What failed? Fix it."
    3. git diff → commit → push
    4. Open issue + draft PR

    Returns dict with outcome, issue_url, pr_url, etc.
    """
    result: dict[str, Any] = {"job_name": job_name, "outcome": "error"}

    # Fetch the actual job log if we don't have it
    if not log_excerpt and job_id and repo_full_name:
        try:
            from scripts.log_retriever import LogRetriever
            retriever = LogRetriever(gh)
            log_excerpt = retriever.get_job_log(repo_full_name, job_id)
            logger.info("Fetched %d chars of log for job %s.", len(log_excerpt), job_name)
        except Exception as exc:
            logger.warning("Could not fetch log for job %s: %s", job_name, exc)

    # Build a useful failure summary from parsed failures (if any)
    failure_summary = ""
    first_pf = parsed_failures[0] if parsed_failures else None
    if first_pf:
        failure_summary = (
            f"Test: {first_pf.test_name or first_pf.failure_identifier}\n"
            f"File: {first_pf.file_path}\n"
            f"Error: {first_pf.error_message}\n"
        )
        if first_pf.stack_trace:
            failure_summary += f"Stack trace:\n{first_pf.stack_trace[:2000]}\n"

    # Truncate log to last 2000 lines
    log_lines = (log_excerpt or "").splitlines()
    if len(log_lines) > 2000:
        log_lines = log_lines[-2000:]
    log_text = "\n".join(log_lines)

    # Clone the repo
    tmpdir = tempfile.mkdtemp(prefix="valkey-fix-")
    clone_url = f"https://x-access-token:{fork_token}@github.com/{fork_repo}.git"
    try:
        _run(["git", "clone", "--depth", "50", "--branch", target_branch, clone_url, tmpdir])
        _run(["git", "config", "user.name", "valkey-ci-agent"], cwd=tmpdir)
        _run(["git", "config", "user.email", "ci-agent@valkey.io"], cwd=tmpdir)
    except Exception as exc:
        logger.error("Clone failed: %s", exc)
        result["error"] = str(exc)
        return result

    # One Claude Code call: analyze + fix
    prompt = (
        f"You are a Valkey core developer fixing a CI test failure.\n\n"
        f"## CI Job: {job_name}\n"
        f"## Failing run: {run_url}\n"
        f"## Commit: {base_sha}\n\n"
    )
    if failure_summary:
        prompt += f"## Parsed failure info:\n{failure_summary}\n"
    if log_text:
        prompt += (
            f"## Raw CI log (last {len(log_lines)} lines):\n"
            f"```\n{log_text[-8000:]}\n```\n\n"
        )
    prompt += (
        "## Your task:\n"
        "1. Read the CI log above to understand what exactly failed\n"
        "2. Read the relevant source/test files in this repo\n"
        "3. Edit the files to fix the root cause\n"
        "4. Make minimal changes following Valkey C coding style\n\n"
        "Do NOT output a diff. Just edit the files directly using the Write tool."
    )

    logger.info("Calling Claude Code for %s (log=%d chars)...", job_name, len(log_text))
    stdout, stderr, rc = run_claude_code(prompt, cwd=tmpdir, timeout=600)
    logger.info("Claude output for %s (%d chars):\n%s", job_name, len(stdout), stdout[:3000])

    # Capture git diff
    diff_result = subprocess.run(
        ["git", "diff"], cwd=tmpdir, capture_output=True, text=True,
    )
    patch = diff_result.stdout.strip()

    if not patch:
        logger.warning("Claude edited no files for %s.", job_name)
        # Still create the issue even if no fix
        _create_issue_best_effort(
            gh, fork_repo, parsed_failures, job_name, run_url,
            f"Claude analyzed the failure but could not produce a fix.\n\nClaude output:\n```\n{stdout[:2000]}\n```",
        )
        result["outcome"] = "no-fix-generated"
        return result

    logger.info("Claude produced %d-line diff for %s.", patch.count("\n"), job_name)

    # Commit + push
    branch_name = f"bot/fix/{job_name[:40]}/{base_sha[:8]}"
    try:
        _run(["git", "checkout", "-B", branch_name], cwd=tmpdir)
        _run(["git", "add", "-A"], cwd=tmpdir)
        _run(["git", "commit", "-m", f"[bot-fix] Fix {job_name}"], cwd=tmpdir)
        _run(["git", "push", "--force", "origin", branch_name], cwd=tmpdir)
        logger.info("Pushed fix to %s/%s.", fork_repo, branch_name)
    except Exception as exc:
        logger.error("Push failed for %s: %s", job_name, exc)
        result["outcome"] = "push-failed"
        result["error"] = str(exc)
        return result

    # Create issue
    issue_url, issue_number = "", 0
    if first_pf:
        try:
            from scripts.models import FailureReport
            dummy_report = FailureReport(
                workflow_name="Daily", job_name=job_name,
                matrix_params={}, commit_sha=base_sha,
                failure_source="trusted", parsed_failures=parsed_failures,
                workflow_file="daily.yml", repo_full_name=fork_repo,
                target_branch=target_branch,
            )
            issue_url, issue_number, _ = create_or_update_issue(
                gh, fork_repo, first_pf, dummy_report, run_url,
            )
        except Exception as exc:
            logger.warning("Issue creation failed: %s", exc)

    # Open draft PR
    try:
        check_publish_allowed(target_repo=fork_repo, action="create_pull")
        from scripts.github_client import retry_github_call
        from scripts.pr_manager import upsert_pull_request

        repo_obj = retry_github_call(
            lambda: gh.get_repo(fork_repo), retries=2,
            description=f"get repo {fork_repo}",
        )

        test_name = first_pf.test_name or first_pf.failure_identifier if first_pf else job_name
        title = f"[bot-fix] Fix {test_name} in {job_name}"[:256]

        body_lines = []
        if issue_number:
            body_lines.append(f"Fixes #{issue_number}\n")
        body_lines.append(f"## Automated fix for `{job_name}`\n")
        body_lines.append(f"- Failing run: {run_url}")
        body_lines.append(f"- Commit: `{base_sha[:12]}`")
        body_lines.append(f"- Branch: `{branch_name}`\n")
        body_lines.append("### Claude's analysis\n")
        body_lines.append(f"```\n{stdout[:3000]}\n```\n")
        body_lines.append("---")
        body_lines.append("*Generated by valkey-ci-agent using Claude Code.*")

        pr = upsert_pull_request(
            repo_obj, head=branch_name, base=target_branch,
            title=title, body="\n".join(body_lines),
            draft=True, labels=("bot-fix",),
        )
        pr_url = str(getattr(pr, "html_url", ""))
        result["pr_url"] = pr_url
        result["outcome"] = "pr-created"
        logger.info("Draft PR created: %s", pr_url)

        # Comment on issue with PR link
        if issue_number:
            try:
                issue_obj = repo_obj.get_issue(issue_number)
                issue_obj.create_comment(f"Draft PR opened: {pr_url}")
            except Exception:
                pass

    except Exception as exc:
        logger.error("PR creation failed for %s: %s", job_name, exc)
        result["outcome"] = "pr-failed"
        result["error"] = str(exc)

    # Cleanup
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    return result


def _create_issue_best_effort(
    gh: Github, repo: str, parsed_failures: list[ParsedFailure],
    job_name: str, run_url: str, extra_comment: str,
) -> None:
    """Create an issue even when no fix was generated."""
    if not parsed_failures:
        return
    try:
        from scripts.models import FailureReport
        dummy = FailureReport(
            workflow_name="Daily", job_name=job_name,
            matrix_params={}, commit_sha="",
            failure_source="trusted", parsed_failures=parsed_failures,
            workflow_file="daily.yml",
        )
        url, num, created = create_or_update_issue(
            gh, repo, parsed_failures[0], dummy, run_url,
        )
        if extra_comment:
            repo_obj = gh.get_repo(repo)
            repo_obj.get_issue(num).create_comment(extra_comment)
    except Exception as exc:
        logger.warning("Best-effort issue creation failed: %s", exc)


def _run(cmd: list[str], cwd: str | None = None) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}: {result.stderr[:500]}")

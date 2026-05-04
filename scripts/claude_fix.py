"""One-shot Claude Code fix: give it the CI log, get a fix back.

Claude Code is the primary analyzer here. The structured parsers are useful
metadata when they work, but raw job logs are the source of truth.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any

from scripts.agent_runtime import run_agent
from scripts.git_auth import GitAuth, github_https_url
from scripts.issue_tracker import create_or_update_issue
from scripts.log_retriever import LogRetriever
from scripts.models import FailureReport, ParsedFailure
from scripts.publish_guard import check_publish_allowed

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)

_MAX_LOG_CHARS = 45_000
_LOG_TAIL_CHARS = 22_000
_LOG_MARKER_CONTEXT_LINES = 12
_FAILURE_MARKER_RE = re.compile(
    r"(error|fail|failed|failure|assert|crash|signal|sanitizer|valgrind|timeout|panic|exception)",
    re.IGNORECASE,
)


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
    target_token: str | None = None,
) -> dict[str, Any]:
    """Ask Claude Code to analyze a failed job log, edit the repo, and open a PR."""
    result: dict[str, Any] = {"job_name": job_name, "outcome": "error"}

    fetched_log = _fetch_job_log(
        gh=gh,
        target_token=target_token,
        repo_full_name=repo_full_name,
        job_id=job_id,
        job_name=job_name,
    )
    if fetched_log:
        log_excerpt = fetched_log

    log_text = _compact_log_for_prompt(log_excerpt or "")
    tracking_failures = parsed_failures or [_synthesize_failure(job_name, log_text)]
    first_pf = tracking_failures[0]
    failure_summary = _format_failure_summary(first_pf)

    tmpdir = tempfile.mkdtemp(prefix="valkey-fix-")
    try:
        try:
            with GitAuth(fork_token, prefix="claude-fix-git-askpass-") as git_auth:
                git_env = git_auth.env()
                _run(
                    ["git", "clone", "--depth", "50", "--branch", target_branch, github_https_url(fork_repo), tmpdir],
                    env=git_env,
                )
                _run(["git", "config", "user.name", "valkey-ci-agent"], cwd=tmpdir)
                _run(["git", "config", "user.email", "ci-agent@valkey.io"], cwd=tmpdir)
        except Exception as exc:
            logger.error("Clone failed: %s", exc)
            result["error"] = str(exc)
            return result

        prompt = _build_prompt(
            job_name=job_name,
            run_url=run_url,
            base_sha=base_sha,
            failure_summary=failure_summary,
            log_text=log_text,
        )

        logger.info("Calling Claude Code for %s (log=%d chars)...", job_name, len(log_text))
        agent_result = run_agent("fix_generate_patch", prompt, cwd=tmpdir)
        stdout = agent_result.stdout
        stderr = agent_result.stderr
        result["claude_exit_code"] = agent_result.returncode
        logger.info(
            "Claude output for %s (%d chars stdout, %d chars stderr):\n%s",
            job_name,
            len(stdout),
            len(stderr),
            stdout[:3000],
        )

        patch = _capture_worktree_diff(tmpdir)

        if not patch:
            logger.warning("Claude edited no files for %s.", job_name)
            _create_issue_best_effort(
                gh,
                fork_repo,
                tracking_failures,
                job_name,
                run_url,
                _format_no_fix_comment(stdout, stderr, agent_result.returncode),
            )
            result["outcome"] = "no-fix-generated"
            result["error"] = f"claude exited {agent_result.returncode} without editing files"
            return result

        if agent_result.returncode != 0:
            logger.warning(
                "Claude exited %d after editing files for %s; refusing to publish.",
                agent_result.returncode,
                job_name,
            )
            _create_issue_best_effort(
                gh,
                fork_repo,
                tracking_failures,
                job_name,
                run_url,
                _format_no_fix_comment(stdout, stderr, agent_result.returncode),
            )
            result["outcome"] = "claude-failed"
            result["error"] = f"claude exited {agent_result.returncode} after editing files"
            return result

        logger.info("Claude produced %d-line diff for %s.", patch.count("\n"), job_name)

        branch_name = f"agent/fix/{_slugify(job_name)[:44]}-{(base_sha or 'unknown')[:8]}"
        try:
            with GitAuth(fork_token, prefix="claude-fix-push-askpass-") as git_auth:
                git_env = git_auth.env()
                _run(["git", "checkout", "-B", branch_name], cwd=tmpdir)
                _run(["git", "add", "-A"], cwd=tmpdir)
                _run(["git", "commit", "-m", f"[agent-fix] Fix {job_name}"], cwd=tmpdir)
                _run(["git", "push", "--force", "origin", branch_name], cwd=tmpdir, env=git_env)
            logger.info("Pushed fix to %s/%s.", fork_repo, branch_name)
        except Exception as exc:
            logger.error("Push failed for %s: %s", job_name, exc)
            result["outcome"] = "push-failed"
            result["error"] = str(exc)
            return result

        issue_url, issue_number = _create_issue_best_effort(
            gh,
            fork_repo,
            tracking_failures,
            job_name,
            run_url,
            "",
            base_sha=base_sha,
            target_branch=target_branch,
        )
        if issue_url:
            result["issue_url"] = issue_url

        try:
            pr_url = _open_draft_pr(
                gh=gh,
                fork_repo=fork_repo,
                branch_name=branch_name,
                target_branch=target_branch,
                job_name=job_name,
                run_url=run_url,
                base_sha=base_sha,
                first_pf=first_pf,
                issue_number=issue_number,
                stdout=stdout,
            )
            result["pr_url"] = pr_url
            result["outcome"] = "pr-created"
            logger.info("Draft PR created: %s", pr_url)

            if issue_number:
                try:
                    repo_obj = gh.get_repo(fork_repo)
                    repo_obj.get_issue(issue_number).create_comment(f"Draft PR opened: {pr_url}")
                except Exception:
                    pass
        except Exception as exc:
            logger.error("PR creation failed for %s: %s", job_name, exc)
            result["outcome"] = "pr-failed"
            result["error"] = str(exc)

        return result
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _fetch_job_log(
    *,
    gh: Github,
    target_token: str | None,
    repo_full_name: str,
    job_id: int,
    job_name: str,
) -> str:
    if not job_id or not repo_full_name:
        return ""
    try:
        retriever = LogRetriever(gh, token=target_token)
        log_text = retriever.get_job_log(repo_full_name, job_id)
        logger.info("Fetched %d chars of log for job %s.", len(log_text), job_name)
        return log_text
    except Exception as exc:
        logger.warning("Could not fetch log for job %s: %s", job_name, exc)
        return ""


def _format_failure_summary(parsed_failure: ParsedFailure) -> str:
    summary = (
        f"Test: {parsed_failure.test_name or parsed_failure.failure_identifier}\n"
        f"File: {parsed_failure.file_path}\n"
        f"Parser: {parsed_failure.parser_type}\n"
        f"Error: {parsed_failure.error_message}\n"
    )
    if parsed_failure.stack_trace:
        summary += f"Stack trace:\n{parsed_failure.stack_trace[:2000]}\n"
    return summary


def _compact_log_for_prompt(log_text: str, max_chars: int = _MAX_LOG_CHARS) -> str:
    """Keep focused failure context without requiring structured parsing."""
    if len(log_text) <= max_chars:
        return log_text

    lines = log_text.splitlines()
    marker_blocks: list[str] = []
    used_ranges: list[tuple[int, int]] = []
    tail_budget = min(_LOG_TAIL_CHARS, max_chars // 2)
    marker_budget = max(0, max_chars - tail_budget - 200)
    marker_chars = 0

    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if not _FAILURE_MARKER_RE.search(line):
            continue
        start = max(0, index - _LOG_MARKER_CONTEXT_LINES)
        end = min(len(lines), index + _LOG_MARKER_CONTEXT_LINES + 1)
        if any(start <= old_end and end >= old_start for old_start, old_end in used_ranges):
            continue
        block = "\n".join(lines[start:end])
        if marker_chars + len(block) > marker_budget:
            block = block[: max(0, marker_budget - marker_chars)]
        if not block:
            break
        marker_blocks.append(block)
        used_ranges.append((start, end))
        marker_chars += len(block)
        if marker_chars >= marker_budget:
            break

    marker_text = "\n\n[...]\n\n".join(reversed(marker_blocks))
    if marker_text:
        prefix = f"[selected failure-context lines]\n{marker_text}\n\n[tail of job log]\n"
    else:
        prefix = "[tail of job log]\n"
    if len(prefix) >= max_chars:
        return prefix[:max_chars]
    tail_budget = min(tail_budget, max_chars - len(prefix))
    tail_text = log_text[-tail_budget:] if tail_budget else ""
    return prefix + tail_text


def _synthesize_failure(job_name: str, log_text: str) -> ParsedFailure:
    signature = _extract_failure_signature(log_text) or "No structured parser match; inspect the raw job log."
    digest = hashlib.sha1(f"{job_name}\n{signature}".encode("utf-8")).hexdigest()[:12]
    return ParsedFailure(
        failure_identifier=f"job-log:{_slugify(job_name)}:{digest}",
        test_name=job_name,
        file_path="",
        error_message=signature[:2000],
        assertion_details=None,
        line_number=None,
        stack_trace=None,
        parser_type="claude-log",
    )


def _extract_failure_signature(log_text: str) -> str:
    lines = log_text.splitlines()
    for line in reversed(lines):
        if _FAILURE_MARKER_RE.search(line):
            return line.strip()
    return "\n".join(lines[-20:]).strip()


def _build_prompt(
    *,
    job_name: str,
    run_url: str,
    base_sha: str,
    failure_summary: str,
    log_text: str,
) -> str:
    prompt = (
        "You are a Valkey core developer fixing a CI test failure.\n\n"
        f"## CI Job: {job_name}\n"
        f"## Failing run: {run_url}\n"
        f"## Commit: {base_sha}\n\n"
        "## Parser output (optional context)\n"
        "Treat this as a hint only. If it conflicts with the raw log, trust the raw log.\n"
        f"{failure_summary}\n"
    )
    if log_text:
        prompt += (
            f"## CI log ({len(log_text)} chars)\n"
            f"```\n{log_text}\n```\n\n"
        )
    prompt += (
        "## Your task\n"
        "1. Read the CI log to identify the actual failing test or crash.\n"
        "2. Read the relevant source and test files in this checkout.\n"
        "3. Edit the files to fix the root cause of the flaky failure.\n"
        "4. Keep the patch minimal and consistent with Valkey style.\n\n"
        "If the log does not justify a code change, leave the checkout unchanged and explain why. "
        "Do not output a diff; edit files directly using Edit or MultiEdit."
    )
    return prompt


def _format_no_fix_comment(stdout: str, stderr: str, rc: int) -> str:
    sections = [
        "Claude Code did not produce a file edit for this failure.",
        "",
        f"Exit code: `{rc}`",
    ]
    if stdout:
        sections.extend(["", "Claude stdout:", f"```\n{stdout[:3000]}\n```"])
    if stderr:
        sections.extend(["", "Claude stderr:", f"```\n{stderr[:3000]}\n```"])
    return "\n".join(sections)


def _create_issue_best_effort(
    gh: Github,
    repo: str,
    parsed_failures: list[ParsedFailure],
    job_name: str,
    run_url: str,
    extra_comment: str,
    *,
    base_sha: str = "",
    target_branch: str = "",
) -> tuple[str, int]:
    """Create or update a failure issue even when no fix was generated."""
    if not parsed_failures:
        return "", 0
    try:
        dummy = FailureReport(
            workflow_name="Daily",
            job_name=job_name,
            matrix_params={},
            commit_sha=base_sha,
            failure_source="trusted",
            parsed_failures=parsed_failures,
            workflow_file="daily.yml",
            repo_full_name=repo,
            target_branch=target_branch,
        )
        url, num, _created = create_or_update_issue(
            gh, repo, parsed_failures[0], dummy, run_url,
        )
        if extra_comment:
            repo_obj = gh.get_repo(repo)
            repo_obj.get_issue(num).create_comment(extra_comment)
        return url, num
    except Exception as exc:
        logger.warning("Best-effort issue creation failed: %s", exc)
        return "", 0


def _open_draft_pr(
    *,
    gh: Github,
    fork_repo: str,
    branch_name: str,
    target_branch: str,
    job_name: str,
    run_url: str,
    base_sha: str,
    first_pf: ParsedFailure,
    issue_number: int,
    stdout: str,
) -> str:
    check_publish_allowed(target_repo=fork_repo, action="create_pull")
    from scripts.github_client import retry_github_call
    from scripts.pr_manager import upsert_pull_request

    repo_obj = retry_github_call(
        lambda: gh.get_repo(fork_repo),
        retries=2,
        description=f"get repo {fork_repo}",
    )

    test_name = first_pf.test_name or first_pf.failure_identifier
    title = f"[agent-fix] Fix {test_name} in {job_name}"[:256]

    body_lines = []
    if issue_number:
        body_lines.append(f"Fixes #{issue_number}\n")
    body_lines.append(f"## Automated fix for `{job_name}`\n")
    body_lines.append(f"- Failing run: {run_url}")
    body_lines.append(f"- Commit: `{base_sha[:12]}`")
    body_lines.append(f"- Branch: `{branch_name}`\n")
    body_lines.append("### Claude output\n")
    body_lines.append(f"```\n{stdout[:3000]}\n```\n")
    body_lines.append("---")
    body_lines.append("*Generated by valkey-ci-agent using Claude Code.*")

    pr = upsert_pull_request(
        repo_obj,
        head=branch_name,
        base=target_branch,
        title=title,
        body="\n".join(body_lines),
        draft=True,
        labels=("agent-fix",),
    )
    return str(getattr(pr, "html_url", ""))


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


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return slug or "job"


def _run(
    cmd: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}: {result.stderr[:500]}")

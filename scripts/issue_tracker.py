"""Issue tracker — creates and deduplicates failure issues on the fork.

Opens a GitHub issue using the test-failure template for each CI failure
before any fix is attempted. If an open issue already exists for the same
failure identifier, adds a comment instead of creating a duplicate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from scripts.github_client import retry_github_call
from scripts.publish_guard import check_publish_allowed

if TYPE_CHECKING:
    from github import Github

    from scripts.models import FailureReport, ParsedFailure

logger = logging.getLogger(__name__)

# Marker embedded in the issue body so we can find agent-created issues.
_ISSUE_MARKER_PREFIX = "<!-- valkey-ci-agent:failure-issue:"


def _build_issue_title(parsed_failure: ParsedFailure, job_name: str) -> str:
    test = parsed_failure.test_name or parsed_failure.failure_identifier
    return f"[TEST-FAILURE] {test} in {job_name}"


def _build_issue_body(
    parsed_failure: ParsedFailure,
    report: FailureReport,
    run_url: str,
) -> str:
    marker = f"{_ISSUE_MARKER_PREFIX}{parsed_failure.failure_identifier} -->"
    test = parsed_failure.test_name or parsed_failure.failure_identifier
    lines = [
        marker,
        "",
        "**Summary**",
        "",
        f"CI failure detected by valkey-ci-agent in `{report.job_name}` "
        f"(`{report.workflow_file}`).",
        "",
        "**Failing test(s)**",
        "",
        f"- Test name: `{test}`",
        f"- File: `{parsed_failure.file_path}`" if parsed_failure.file_path else "",
        f"- Parser: `{parsed_failure.parser_type}`",
        f"- CI link: {run_url}",
        "",
        "**Error message**",
        "",
        f"```\n{parsed_failure.error_message[:2000]}\n```",
    ]
    if parsed_failure.assertion_details:
        lines.extend([
            "",
            "**Assertion details**",
            "",
            f"```\n{parsed_failure.assertion_details[:1000]}\n```",
        ])
    if parsed_failure.stack_trace:
        lines.extend([
            "",
            "**Stack trace**",
            "",
            f"```\n{parsed_failure.stack_trace[:3000]}\n```",
        ])
    return "\n".join(line for line in lines if line is not None)


def _find_existing_issue(
    repo, failure_identifier: str,
) -> object | None:
    """Search open issues for one matching this failure identifier."""
    marker = f"{_ISSUE_MARKER_PREFIX}{failure_identifier}"
    try:
        issues = retry_github_call(
            lambda: list(repo.get_issues(state="open", labels=["test-failure"])),
            retries=2,
            description=f"list open test-failure issues",
        )
        for issue in issues:
            if marker in (issue.body or ""):
                return issue
    except Exception as exc:
        logger.warning("Issue search failed: %s", exc)
    return None


def create_or_update_issue(
    gh: Github,
    repo_full_name: str,
    parsed_failure: ParsedFailure,
    report: FailureReport,
    run_url: str,
) -> tuple[str, int, bool]:
    """Create a new issue or comment on an existing one.

    Returns ``(issue_url, issue_number, created_new)``.
    """
    check_publish_allowed(
        target_repo=repo_full_name,
        action="create_issue",
        context=f"failure: {parsed_failure.failure_identifier[:60]}",
    )

    repo = retry_github_call(
        lambda: gh.get_repo(repo_full_name),
        retries=2,
        description=f"get repo {repo_full_name}",
    )

    existing = _find_existing_issue(repo, parsed_failure.failure_identifier)
    if existing is not None:
        # Add a comment with the new occurrence
        comment_body = (
            f"**New occurrence** detected in `{report.job_name}` "
            f"(`{report.workflow_file}`).\n\n"
            f"CI run: {run_url}\n"
            f"Commit: `{report.commit_sha[:12]}`"
        )
        retry_github_call(
            lambda: existing.create_comment(comment_body),
            retries=2,
            description=f"comment on issue #{existing.number}",
        )
        logger.info(
            "Updated existing issue #%d for %s.",
            existing.number,
            parsed_failure.failure_identifier,
        )
        return str(existing.html_url), existing.number, False

    # Create new issue
    title = _build_issue_title(parsed_failure, report.job_name)
    body = _build_issue_body(parsed_failure, report, run_url)
    try:
        issue = retry_github_call(
            lambda: repo.create_issue(
                title=title,
                body=body,
                labels=["test-failure"],
            ),
            retries=2,
            description=f"create issue for {parsed_failure.failure_identifier[:40]}",
        )
    except Exception:
        # Label might not exist on the fork — create it and retry.
        try:
            repo.create_label("test-failure", "d93f0b", "CI test failure tracked by valkey-ci-agent")
        except Exception:
            pass  # Label may already exist or we lack permission; proceed anyway.
        issue = retry_github_call(
            lambda: repo.create_issue(
                title=title,
                body=body,
                labels=["test-failure"],
            ),
            retries=2,
            description=f"create issue (retry after label) for {parsed_failure.failure_identifier[:40]}",
        )
    logger.info(
        "Created issue #%d for %s: %s",
        issue.number,
        parsed_failure.failure_identifier,
        issue.html_url,
    )
    return str(issue.html_url), issue.number, True

"""Issue-first CI fix pipeline — the new top-level orchestrator.

Replaces the old analyze → fix → validate → PR flow with:
  1. Parse failures from a CI run
  2. For each failure, open a tracking issue on the fork
  3. Analyze root cause
  4. Fix-validate loop (generate → push → dispatch daily.yml → poll → retry)
  5. Open a PR with issue link + validation evidence

This module is the entry point called by the monitor workflow.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from github import Github

    from scripts.fix_generator import FixGenerator
    from scripts.models import FailureReport, ParsedFailure, RootCauseReport

from scripts.fix_loop import run_fix_loop
from scripts.issue_tracker import create_or_update_issue
from scripts.pr_manager import build_validated_pr_body

logger = logging.getLogger(__name__)


def process_failure_issue_first(
    *,
    report: "FailureReport",
    parsed_failure: "ParsedFailure",
    root_cause: "RootCauseReport",
    fix_generator: "FixGenerator",
    gh: "Github",
    fork_repo: str,
    fork_token: str,
    run_url: str,
    max_fix_attempts: int = 3,
    loop_count: int = 100,
) -> dict[str, Any]:
    """Process one failure through the issue-first pipeline.

    Returns a dict with the outcome:
      - issue_url, issue_number
      - pr_url (if fix succeeded)
      - fix_result (FixResult)
      - outcome: "pr-created" | "fix-failed" | "error"
    """
    result: dict[str, Any] = {
        "job_name": report.job_name,
        "failure_identifier": parsed_failure.failure_identifier,
        "outcome": "error",
    }

    # 1. Open tracking issue
    try:
        issue_url, issue_number, created = create_or_update_issue(
            gh, fork_repo, parsed_failure, report, run_url,
        )
        result["issue_url"] = issue_url
        result["issue_number"] = issue_number
        logger.info(
            "Issue %s #%d for %s.",
            "created" if created else "updated",
            issue_number,
            parsed_failure.failure_identifier[:60],
        )
    except Exception as exc:
        logger.error("Issue creation failed: %s", exc)
        result["error"] = str(exc)
        return result

    # 2. Fix-validate loop
    test_file = parsed_failure.file_path or ""
    try:
        fix_result = run_fix_loop(
            report=report,
            root_cause=root_cause,
            fix_generator=fix_generator,
            fork_repo=fork_repo,
            fork_token=fork_token,
            base_sha=report.commit_sha,
            test_file=test_file,
            job_name=report.job_name,
            loop_count=loop_count,
            max_attempts=max_fix_attempts,
            issue_gh=gh,
            issue_repo=fork_repo,
            issue_number=issue_number,
        )
        result["fix_result"] = fix_result
    except Exception as exc:
        logger.error("Fix loop failed: %s", exc)
        result["error"] = str(exc)
        result["outcome"] = "fix-failed"
        return result

    if not fix_result.succeeded:
        result["outcome"] = "fix-failed"
        logger.warning(
            "All %d fix attempts failed for %s.",
            fix_result.attempts,
            parsed_failure.failure_identifier[:60],
        )
        return result

    # 3. Open PR
    try:
        from scripts.github_client import retry_github_call

        repo_obj = retry_github_call(
            lambda: gh.get_repo(fork_repo),
            retries=2,
            description=f"get repo {fork_repo}",
        )

        pr_body = build_validated_pr_body(
            report, root_cause, run_url,
            issue_number=issue_number,
            issue_url=issue_url,
            validation_run_url=fix_result.validation_run_url,
            attempts=fix_result.attempts,
        )

        branch_name = f"bot/fix/{report.job_name[:40]}/{report.commit_sha[:8]}"
        test_name = parsed_failure.test_name or parsed_failure.failure_identifier
        title = f"[bot-fix] Fix {test_name} in {report.job_name}"

        from scripts.pr_manager import upsert_pull_request
        pr = upsert_pull_request(
            repo_obj,
            head=branch_name,
            base=report.target_branch or "unstable",
            title=title[:256],
            body=pr_body,
            draft=False,
            labels=("bot-fix",),
        )
        pr_url = str(getattr(pr, "html_url", ""))
        result["pr_url"] = pr_url
        result["outcome"] = "pr-created"
        logger.info("PR created: %s", pr_url)

    except Exception as exc:
        logger.error("PR creation failed: %s", exc)
        result["error"] = str(exc)
        result["outcome"] = "pr-creation-failed"

    return result

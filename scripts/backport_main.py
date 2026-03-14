"""Backport Bot — CLI entry point and pipeline orchestrator.

Orchestrates the full backport pipeline: config loading, validation,
cherry-pick, conflict resolution, PR creation, and summary reporting.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from github import Github
from github.GithubException import GithubException

from scripts.backport_config import load_backport_config_from_repo
from scripts.backport_models import (
    BackportConfig,
    BackportPRContext,
    BackportResult,
    ResolutionResult,
)
from scripts.backport_pr_creator import BackportPRCreator
from scripts.backport_utils import build_branch_name
from scripts.bedrock_client import BedrockClient
from scripts.cherry_pick import CherryPickExecutor
from scripts.config import BotConfig, ProjectContext
from scripts.conflict_resolver import ConflictResolver
from scripts.github_client import retry_github_call
from scripts.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


@dataclass
class _BedrockConfigAdapter:
    """Adapts :class:`BackportConfig` to the :class:`BedrockConfig` protocol.

    Provides sensible defaults for fields that ``BackportConfig`` does not
    carry (``max_input_tokens``, ``max_output_tokens``, ``max_retries_bedrock``,
    ``project``).
    """

    _backport_config: BackportConfig

    @property
    def bedrock_model_id(self) -> str:
        return self._backport_config.bedrock_model_id

    @property
    def max_input_tokens(self) -> int:
        return 200_000

    @property
    def max_output_tokens(self) -> int:
        return 4096

    @property
    def max_retries_bedrock(self) -> int:
        return 3

    @property
    def project(self) -> ProjectContext:
        return ProjectContext()


# ------------------------------------------------------------------
# Summary helpers
# ------------------------------------------------------------------


def build_summary(result: BackportResult) -> str:
    """Generate a human-readable summary string for a backport run.

    Contains: commits cherry-picked, conflicting files, files resolved,
    files unresolved, and total tokens used.

    **Validates: Requirements 9.2, 9.4**
    """
    lines = [
        f"Commits cherry-picked: {result.commits_cherry_picked}",
        f"Conflicting files: {result.files_conflicted}",
        f"Files resolved by LLM: {result.files_resolved}",
        f"Files unresolved: {result.files_unresolved}",
        f"Total tokens used: {result.total_tokens_used}",
    ]
    return "\n".join(lines)


def emit_job_summary(text: str) -> None:
    """Write *text* to the GitHub Actions job summary file.

    The path is read from the ``GITHUB_STEP_SUMMARY`` environment variable.
    If the variable is unset or the write fails, the error is logged but
    does not raise.
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        logger.info("GITHUB_STEP_SUMMARY not set; skipping job summary.")
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
        logger.info("Wrote job summary to %s.", summary_path)
    except OSError as exc:
        logger.warning("Failed to write job summary: %s", exc)


# ------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------


def run_backport(
    repo_full_name: str,
    source_pr_number: int,
    target_branch: str,
    config: BackportConfig,
    github_token: str,
    aws_region: str,
) -> BackportResult:
    """Execute the backport pipeline end-to-end.

    Returns a :class:`BackportResult` with outcome details.

    **Validates: Requirements 1.2, 1.3, 1.5, 2.1, 4.6, 4.7, 6.1, 8.1,
    9.1, 9.2, 9.3, 9.4**
    """
    gh = Github(github_token)
    repo = retry_github_call(
        lambda: gh.get_repo(repo_full_name),
        retries=3,
        description=f"get repo {repo_full_name}",
    )

    # ---- Step 1: Validate target branch exists (Req 1.5) ----
    logger.info("Validating target branch %s exists.", target_branch)
    try:
        retry_github_call(
            lambda: repo.get_branch(target_branch),
            retries=3,
            description=f"get branch {target_branch}",
        )
    except GithubException as exc:
        if exc.status == 404:
            msg = f"Target branch `{target_branch}` does not exist."
            logger.warning(msg)
            _post_comment(repo, source_pr_number, f"⚠️ Backport skipped: {msg}")
            return BackportResult(outcome="branch-missing", error_message=msg)
        raise

    # ---- Step 2: Check for duplicate backport PR (Req 6.1) ----
    logger.info("Checking for duplicate backport PR.")
    pr_creator = BackportPRCreator(gh, repo_full_name)
    existing_url = pr_creator.check_duplicate(source_pr_number, target_branch)
    if existing_url:
        msg = (
            f"A backport PR already exists for #{source_pr_number} → "
            f"`{target_branch}`: {existing_url}"
        )
        logger.info(msg)
        _post_comment(repo, source_pr_number, f"ℹ️ Backport skipped: {msg}")
        return BackportResult(outcome="duplicate", backport_pr_url=existing_url)

    # ---- Step 3: Rate limit check (Req 8.1) ----
    logger.info("Checking rate limit.")
    bot_config = BotConfig(max_prs_per_day=config.max_prs_per_day)
    # Pass github_client=None so the rate limiter skips the open-PR-label
    # check (which looks for "bot-fix" labels, not backport labels).
    # State persistence still works via state_github_client.
    rate_limiter = RateLimiter(
        bot_config,
        None,
        "",
        state_github_client=gh,
        state_repo_full_name=repo_full_name,
    )
    rate_limiter.load()
    if not rate_limiter.can_create_pr():
        msg = "Daily backport PR rate limit reached. Please try again later."
        logger.warning(msg)
        _post_comment(repo, source_pr_number, f"⚠️ Backport skipped: {msg}")
        return BackportResult(outcome="rate-limited", error_message=msg)

    # ---- Step 4: Fetch source PR metadata ----
    logger.info("Fetching source PR #%d metadata.", source_pr_number)
    try:
        source_pr = retry_github_call(
            lambda: repo.get_pull(source_pr_number),
            retries=3,
            description=f"get PR #{source_pr_number}",
        )
    except GithubException as exc:
        msg = f"Failed to fetch source PR #{source_pr_number}: {exc}"
        logger.error(msg)
        _post_comment(repo, source_pr_number, f"❌ Backport failed: {msg}")
        return BackportResult(outcome="error", error_message=msg)

    commits = [
        c.sha
        for c in retry_github_call(
            lambda: list(source_pr.get_commits()),
            retries=3,
            description=f"get commits for PR #{source_pr_number}",
        )
    ]
    merge_commit_sha = source_pr.merge_commit_sha

    # Fetch PR diff
    try:
        # PyGithub doesn't have a direct diff method, but we can get
        # the patch/diff from the PR's files
        pr_files = retry_github_call(
            lambda: list(source_pr.get_files()),
            retries=3,
            description=f"get files for PR #{source_pr_number}",
        )
        diff_parts = []
        for f in pr_files:
            if f.patch:
                diff_parts.append(f"--- a/{f.filename}\n+++ b/{f.filename}\n{f.patch}")
        diff_content = "\n".join(diff_parts)
    except Exception:
        diff_content = ""

    pr_context = BackportPRContext(
        source_pr_number=source_pr_number,
        source_pr_title=source_pr.title or "",
        source_pr_body=source_pr.body or "",
        source_pr_url=source_pr.html_url,
        source_pr_diff=diff_content,
        target_branch=target_branch,
        commits=commits,
        repo_full_name=repo_full_name,
    )

    # ---- Step 5: Cherry-pick (Req 2.1) ----
    logger.info("Executing cherry-pick onto %s.", target_branch)
    branch_name = build_branch_name(source_pr_number, target_branch)
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Clone the repo with full history for cherry-pick
        _clone_repo(repo_full_name, github_token, tmp_dir, target_branch)

        # Create the backport branch locally from target branch HEAD
        _run_git(tmp_dir, "checkout", "-b", branch_name)

        executor = CherryPickExecutor(tmp_dir)

        try:
            cherry_result = executor.execute(
                branch_name, merge_commit_sha, commits,
            )
        except Exception as exc:
            msg = f"Cherry-pick failed: {exc}"
            logger.error(msg)
            _post_comment(repo, source_pr_number, f"❌ Backport failed: {msg}")
            return BackportResult(outcome="error", error_message=msg)

        # ---- Step 6: Conflict resolution ----
        resolution_results = None
        total_tokens = 0
        if not cherry_result.success and cherry_result.conflicting_files:
            logger.info(
                "Cherry-pick produced %d conflict(s). Invoking conflict resolver.",
                len(cherry_result.conflicting_files),
            )
            bedrock_adapter = _BedrockConfigAdapter(_backport_config=config)
            bedrock_client = BedrockClient(
                bedrock_adapter,
                client=boto3.client("bedrock-runtime", region_name=aws_region),
            )
            resolver = ConflictResolver(bedrock_client, config)
            resolution_results = resolver.resolve_conflicts(
                cherry_result.conflicting_files,
                pr_context,
                token_budget=config.per_backport_token_budget,
            )
            total_tokens = sum(r.tokens_used for r in resolution_results)

            # Apply resolved files to the working tree and commit
            _apply_resolutions(tmp_dir, resolution_results)

        # Push the backport branch to the remote
        logger.info("Pushing branch %s to origin.", branch_name)
        _run_git(tmp_dir, "push", "origin", branch_name)

    # ---- Step 7: Create backport PR (Req 4.6) ----
    logger.info("Creating backport PR.")
    try:
        backport_pr_url = pr_creator.create_backport_pr(
            pr_context, cherry_result, resolution_results, branch_name,
        )
    except Exception as exc:
        msg = f"Failed to create backport PR: {exc}"
        logger.error(msg)
        _post_comment(repo, source_pr_number, f"❌ Backport failed: {msg}")
        return BackportResult(outcome="error", error_message=msg)

    # ---- Build result ----
    files_resolved = 0
    files_unresolved = 0
    if resolution_results:
        files_resolved = sum(
            1 for r in resolution_results if r.resolved_content is not None
        )
        files_unresolved = sum(
            1 for r in resolution_results if r.resolved_content is None
        )

    outcome = "success" if files_unresolved == 0 else "conflicts-unresolved"
    result = BackportResult(
        outcome=outcome,
        backport_pr_url=backport_pr_url,
        commits_cherry_picked=len(cherry_result.applied_commits),
        files_conflicted=len(cherry_result.conflicting_files),
        files_resolved=files_resolved,
        files_unresolved=files_unresolved,
        total_tokens_used=total_tokens,
    )

    # ---- Step 8: Post summary comment on source PR (Req 9.2) ----
    summary_text = build_summary(result)
    comment_body = (
        f"✅ Backport PR created: {backport_pr_url}\n\n"
        f"### Summary\n```\n{summary_text}\n```"
    )
    _post_comment(repo, source_pr_number, comment_body)

    # ---- Step 9: Record PR creation and save state (Req 8.1) ----
    rate_limiter.record_pr_created()
    rate_limiter.save()

    # ---- Step 10: Emit GitHub Actions job summary (Req 9.4) ----
    job_summary = (
        f"## Backport Result: {result.outcome}\n\n"
        f"- **Source PR:** #{source_pr_number}\n"
        f"- **Target branch:** `{target_branch}`\n"
        f"- **Backport PR:** {backport_pr_url}\n\n"
        f"```\n{summary_text}\n```"
    )
    emit_job_summary(job_summary)

    logger.info("Backport complete: %s", result.outcome)
    return result


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _post_comment(repo: object, pr_number: int, body: str) -> None:
    """Post a comment on a pull request (best-effort)."""
    try:
        pr = retry_github_call(
            lambda: repo.get_pull(pr_number),  # type: ignore[attr-defined]
            retries=3,
            description=f"get PR #{pr_number} for comment",
        )
        retry_github_call(
            lambda: pr.create_issue_comment(body),
            retries=3,
            description=f"post comment on PR #{pr_number}",
        )
        logger.info("Posted comment on PR #%d.", pr_number)
    except Exception as exc:
        logger.warning("Failed to post comment on PR #%d: %s", pr_number, exc)


def _clone_repo(
    repo_full_name: str,
    github_token: str,
    dest_dir: str,
    target_branch: str,
) -> None:
    """Clone the repository with full history into *dest_dir*."""
    import subprocess

    clone_url = f"https://x-access-token:{github_token}@github.com/{repo_full_name}.git"
    logger.info("Cloning %s into %s.", repo_full_name, dest_dir)
    subprocess.run(
        ["git", "clone", "--no-single-branch", "--branch", target_branch, clone_url, "."],
        cwd=dest_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    # Fetch all branches so cherry-pick can reference any commit
    subprocess.run(
        ["git", "fetch", "--all"],
        cwd=dest_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def _run_git(repo_dir: str, *args: str) -> None:
    """Run a git command in *repo_dir*, raising on failure."""
    import subprocess

    cmd = ["git", *args]
    logger.debug("Running: %s (cwd=%s)", " ".join(cmd), repo_dir)
    subprocess.run(cmd, cwd=repo_dir, check=True, capture_output=True, text=True)


def _apply_resolutions(
    repo_dir: str,
    resolution_results: list[ResolutionResult],
) -> None:
    """Write resolved file contents to the working tree and commit.

    For each successfully resolved file, writes the content, stages it
    with ``git add``, then aborts the failed cherry-pick and commits
    the resolved state.
    """
    any_resolved = False
    for result in resolution_results:
        if result.resolved_content is not None:
            file_path = os.path.join(repo_dir, result.path)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as fh:
                fh.write(result.resolved_content)
            _run_git(repo_dir, "add", result.path)
            any_resolved = True

    # Also stage any unresolved files (they keep conflict markers)
    for result in resolution_results:
        if result.resolved_content is None:
            try:
                _run_git(repo_dir, "add", result.path)
            except Exception:
                logger.warning("Could not stage unresolved file %s", result.path)

    if any_resolved or resolution_results:
        # Complete the cherry-pick with resolved content.
        # Set core.editor=true to prevent git from opening an editor
        # in the non-interactive CI environment.
        try:
            _run_git(
                repo_dir, "-c", "user.name=backport-bot",
                "-c", "user.email=backport-bot@users.noreply.github.com",
                "-c", "core.editor=true",
                "cherry-pick", "--continue",
            )
        except Exception:
            # If cherry-pick --continue fails (e.g. no cherry-pick in
            # progress because all files were staged), commit directly.
            logger.warning("cherry-pick --continue failed, committing directly.")
            _run_git(
                repo_dir, "-c", "user.name=backport-bot",
                "-c", "user.email=backport-bot@users.noreply.github.com",
                "commit", "--allow-empty", "-m",
                "Backport with conflict resolutions",
            )


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def main() -> None:
    """CLI entry point for the backport bot."""
    parser = argparse.ArgumentParser(description="Backport Bot Pipeline")
    parser.add_argument(
        "--repo", required=True, help="Repository full name (owner/repo)",
    )
    parser.add_argument(
        "--pr-number", type=int, required=True, help="Source PR number",
    )
    parser.add_argument(
        "--target-branch", required=True, help="Target release branch",
    )
    parser.add_argument(
        "--config",
        default=".github/backport-bot.yml",
        help="Path to backport config YAML in the consumer repo",
    )
    parser.add_argument(
        "--token", required=True, help="GitHub token",
    )
    parser.add_argument(
        "--aws-region", default="us-east-1", help="AWS region for Bedrock",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Load config from consumer repo
    gh = Github(args.token)
    config = load_backport_config_from_repo(gh, args.repo, args.config)

    result = run_backport(
        repo_full_name=args.repo,
        source_pr_number=args.pr_number,
        target_branch=args.target_branch,
        config=config,
        github_token=args.token,
        aws_region=args.aws_region,
    )

    logger.info("Backport outcome: %s", result.outcome)
    if result.outcome == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()

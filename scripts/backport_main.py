"""Backport Agent — CLI entry point and pipeline orchestrator.

Orchestrates the full backport pipeline: config loading, validation,
cherry-pick, conflict resolution, PR creation, and summary reporting.
"""

from __future__ import annotations

import argparse
import logging
import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from github import Auth, Github
from github.GithubException import GithubException

from scripts.publish_guard import check_publish_allowed

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
from scripts.commit_signoff import (
    CommitSigner,
    load_signer_from_env,
    require_dco_signoff_from_env,
)
from scripts.config import BotConfig, ProjectContext
from scripts.conflict_resolver import ConflictResolver
from scripts.event_ledger import EventLedger
from scripts.github_client import retry_github_call
from scripts.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


def _resolve_commit_signer() -> tuple[CommitSigner, bool]:
    """Load commit signer policy from environment variables."""
    signer = load_signer_from_env()
    require_dco = require_dco_signoff_from_env()
    if require_dco and not signer.configured:
        raise ValueError(
            "DCO signoff is required, but CI_BOT_COMMIT_NAME or "
            "CI_BOT_COMMIT_EMAIL is not configured."
        )
    return signer, require_dco


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
        f"- Outcome: `{result.outcome}`",
        f"- Commits cherry-picked: {result.commits_cherry_picked}",
        f"- Conflicting files: {result.files_conflicted}",
        f"- Files resolved by LLM: {result.files_resolved}",
        f"- Files unresolved: {result.files_unresolved}",
        f"- Total tokens used: {result.total_tokens_used}",
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


def _backport_subject(repo_full_name: str, source_pr_number: int, target_branch: str) -> str:
    """Build a stable event subject for one backport attempt."""
    return f"{repo_full_name}#{source_pr_number}->{target_branch}"


def run_backport(
    repo_full_name: str,
    source_pr_number: int,
    target_branch: str,
    config: BackportConfig,
    github_token: str,
    aws_region: str,
    push_repo: str | None = None,
) -> BackportResult:
    """Execute the backport pipeline end-to-end.

    Returns a :class:`BackportResult` with outcome details.

    **Validates: Requirements 1.2, 1.3, 1.5, 2.1, 4.6, 4.7, 6.1, 8.1,
    9.1, 9.2, 9.3, 9.4**
    """
    gh = Github(auth=Auth.Token(github_token))
    subject = _backport_subject(repo_full_name, source_pr_number, target_branch)
    event_ledger = EventLedger(gh, repo_full_name)
    event_ledger.record(
        "workflow.run_seen",
        subject,
        workflow="backport",
        repo=repo_full_name,
        source_pr_number=source_pr_number,
        target_branch=target_branch,
    )
    rate_limiter: RateLimiter | None = None
    try:
        try:
            signer, require_dco_signoff = _resolve_commit_signer()
        except ValueError as exc:
            msg = str(exc)
            logger.error(msg)
            event_ledger.record(
                "backport.preflight_failed",
                subject,
                reason="commit-signer-misconfigured",
                error=msg,
            )
            return BackportResult(outcome="error", error_message=msg)
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
                _post_comment(repo, source_pr_number, f"Backport skipped: {msg}")
                event_ledger.record(
                    "backport.skipped",
                    subject,
                    reason="branch-missing",
                    error=msg,
                )
                return BackportResult(outcome="branch-missing", error_message=msg)
            raise

        # ---- Step 2: Check for duplicate backport PR (Req 6.1) ----
        logger.info("Checking for duplicate backport PR.")
        pr_target_repo = push_repo or repo_full_name
        pr_creator = BackportPRCreator(
            gh,
            pr_target_repo,
            backport_label=config.backport_label,
            llm_conflict_label=config.llm_conflict_label,
        )
        existing_url = pr_creator.check_duplicate(source_pr_number, target_branch)
        if existing_url:
            msg = (
                f"A backport PR already exists for #{source_pr_number} → "
                f"`{target_branch}`: {existing_url}"
            )
            logger.info(msg)
            _post_comment(repo, source_pr_number, f"Backport skipped: {msg}")
            event_ledger.record(
                "backport.skipped",
                subject,
                reason="duplicate",
                backport_pr_url=existing_url,
            )
            return BackportResult(outcome="duplicate", backport_pr_url=existing_url)

        # ---- Step 3: Rate limit check (Req 8.1) ----
        logger.info("Checking rate limit.")
        bot_config = BotConfig(max_prs_per_day=config.max_prs_per_day)
        # Pass github_client=None so the rate limiter skips the open-PR-label
        # check (which looks for "agent-fix" labels, not backport labels).
        # State persistence still works via state_github_client.
        rate_limiter = RateLimiter(
            bot_config,
            None,
            "",
            state_github_client=gh,
            state_repo_full_name=repo_full_name,
        )
        rate_limiter.load()
        if not rate_limiter.reserve_pr_creation():
            msg = "Daily backport PR rate limit reached. Please try again later."
            logger.warning(msg)
            _post_comment(repo, source_pr_number, f"Backport skipped: {msg}")
            event_ledger.record(
                "backport.skipped",
                subject,
                reason="rate-limited",
                error=msg,
            )
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
            _post_comment(repo, source_pr_number, f"Backport failed: {msg}")
            event_ledger.record(
                "backport.failed",
                subject,
                phase="fetch-source-pr",
                error=msg,
            )
            return BackportResult(outcome="error", error_message=msg)

        if not bool(getattr(source_pr, "merged", False)):
            msg = f"Source PR #{source_pr_number} is not merged."
            logger.warning(msg)
            _post_comment(repo, source_pr_number, f"Backport skipped: {msg}")
            event_ledger.record(
                "backport.skipped",
                subject,
                reason="pr-not-merged",
                error=msg,
            )
            return BackportResult(outcome="pr-not-merged", error_message=msg)

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
        except Exception as exc:
            logger.warning("Could not fetch PR diff for #%s: %s", source_pr_number, exc)
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
            git_env = _clone_repo(
                repo_full_name,
                github_token,
                tmp_dir,
                target_branch,
                signer=signer,
            )

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
                _post_comment(repo, source_pr_number, f"Backport failed: {msg}")
                event_ledger.record(
                    "backport.failed",
                    subject,
                    phase="cherry-pick",
                    error=msg,
                )
                return BackportResult(outcome="error", error_message=msg)

            # ---- Step 6: Conflict resolution ----
            resolution_results = None
            total_tokens = 0
            if not cherry_result.success and cherry_result.conflicting_files:
                logger.info(
                    "Cherry-pick produced %d conflict(s). Invoking conflict resolver.",
                    len(cherry_result.conflicting_files),
                )
                event_ledger.record(
                    "backport.conflicts_detected",
                    subject,
                    conflicting_files=len(cherry_result.conflicting_files),
                )
                bedrock_adapter = _BedrockConfigAdapter(_backport_config=config)
                bedrock_client = BedrockClient(
                    bedrock_adapter,
                    client=boto3.client("bedrock-runtime", region_name=aws_region),
                    rate_limiter=rate_limiter,
                )
                resolver = ConflictResolver(
                    bedrock_client, config,
                    github_client=gh,
                    repo_full_name=repo_full_name,
                    head_sha=merge_commit_sha or "",
                )
                resolution_results = resolver.resolve_conflicts(
                    cherry_result.conflicting_files,
                    pr_context,
                    token_budget=config.per_backport_token_budget,
                )
                total_tokens = sum(r.tokens_used for r in resolution_results)

                # Apply resolved files to the working tree and commit
                _apply_resolutions(
                    tmp_dir,
                    resolution_results,
                    signer=signer,
                    require_dco_signoff=require_dco_signoff,
                )

            # Push the backport branch to the remote
            push_target = push_repo or repo_full_name
            if push_repo and push_repo != repo_full_name:
                fork_url = f"https://x-access-token@github.com/{push_repo}.git"
                _run_git(tmp_dir, "remote", "add", "fork", fork_url, env=git_env)
                logger.info("Pushing branch %s to fork %s.", branch_name, push_repo)
                _run_git(tmp_dir, "push", "fork", branch_name, env=git_env)
            else:
                logger.info("Pushing branch %s to origin.", branch_name)
                _run_git(tmp_dir, "push", "origin", branch_name, env=git_env)

        # ---- Step 7: Create backport PR (Req 4.6) ----
        logger.info("Creating backport PR.")
        try:
            backport_pr_url = pr_creator.create_backport_pr(
                pr_context, cherry_result, resolution_results, branch_name,
            )
        except Exception as exc:
            msg = f"Failed to create backport PR: {exc}"
            logger.error(msg)
            _post_comment(repo, source_pr_number, f"Backport failed: {msg}")
            event_ledger.record(
                "backport.failed",
                subject,
                phase="create-pr",
                error=msg,
            )
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
            "## Backport Result\n\n"
            f"Backport PR created: [view PR]({backport_pr_url})\n\n"
            f"### Overview\n{summary_text}"
        )
        _post_comment(repo, source_pr_number, comment_body)

        # ---- Step 9: Record PR creation and save state (Req 8.1) ----
        rate_limiter.record_pr_created()

        event_ledger.record(
            "backport.pr_created",
            subject,
            backport_pr_url=backport_pr_url,
            outcome=outcome,
            commits_cherry_picked=result.commits_cherry_picked,
            files_conflicted=result.files_conflicted,
            files_resolved=result.files_resolved,
            files_unresolved=result.files_unresolved,
            total_tokens_used=result.total_tokens_used,
        )

        # ---- Step 10: Emit GitHub Actions job summary (Req 9.4) ----
        job_summary = (
            f"## Backport Result: {result.outcome}\n\n"
            f"- Source PR: #{source_pr_number}\n"
            f"- Target branch: `{target_branch}`\n"
            f"- Backport PR: [view PR]({backport_pr_url})\n\n"
            f"### Overview\n{summary_text}"
        )
        emit_job_summary(job_summary)

        logger.info("Backport complete: %s", result.outcome)
        return result
    except Exception as exc:
        logger.exception("Backport pipeline failed: %s", exc)
        event_ledger.record(
            "pipeline.failed",
            subject,
            workflow="backport",
            error=str(exc),
        )
        return BackportResult(outcome="error", error_message=str(exc))
    finally:
        if rate_limiter is not None:
            rate_limiter.save()
        event_ledger.save()


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
        check_publish_allowed(
            target_repo=str(getattr(repo, "full_name", "") or ""),
            action="create_issue_comment",
            context=f"backport PR #{pr_number}",
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
    *,
    signer: CommitSigner,
) -> dict[str, str]:
    """Clone the repository with full history into *dest_dir*.

    Uses a git credential helper to supply the token, avoiding
    embedding credentials in the clone URL (which would persist in
    ``.git/config`` and be visible via ``git remote -v``).
    """
    logger.info("Cloning %s into %s.", repo_full_name, dest_dir)

    # Write a small credential-helper script that supplies the token.
    # This keeps the token out of .git/config remote URLs while still
    # allowing subsequent git operations (push, fetch) to authenticate.
    askpass_script = os.path.join(dest_dir, ".git-askpass.sh")
    with open(askpass_script, "w") as f:
        f.write('#!/bin/sh\necho "$GIT_PASSWORD"\n')
    os.chmod(askpass_script, stat.S_IRWXU)

    env = {
        **os.environ,
        "GIT_ASKPASS": askpass_script,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PASSWORD": github_token,
    }

    clone_url = f"https://x-access-token@github.com/{repo_full_name}.git"
    subprocess.run(
        ["git", "clone", "--no-single-branch", "--branch", target_branch, clone_url, "."],
        cwd=dest_dir,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    # Configure git identity for cherry-pick commits
    user_name = signer.name if signer.configured else "valkey-ci-agent"
    user_email = (
        signer.email
        if signer.configured
        else "valkey-ci-agent@users.noreply.github.com"
    )
    subprocess.run(
        ["git", "config", "user.name", user_name],
        cwd=dest_dir, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", user_email],
        cwd=dest_dir, check=True, capture_output=True, text=True,
    )
    # Fetch all branches so cherry-pick can reference any commit
    subprocess.run(
        ["git", "fetch", "--all"],
        cwd=dest_dir,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return env


def _run_git(repo_dir: str, *args: str, env: dict[str, str] | None = None) -> None:
    """Run a git command in *repo_dir*, raising on failure."""
    cmd = ["git", *args]
    logger.debug("Running: %s (cwd=%s)", " ".join(cmd), repo_dir)
    subprocess.run(cmd, cwd=repo_dir, check=True, capture_output=True, text=True, env=env)


def _apply_resolutions(
    repo_dir: str,
    resolution_results: list[ResolutionResult],
    *,
    signer: CommitSigner,
    require_dco_signoff: bool,
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
            continue_args = [
                repo_dir,
                "-c", f"user.name={signer.name or 'backport-agent'}",
                "-c", (
                    f"user.email={signer.email or 'backport-agent@users.noreply.github.com'}"
                ),
                "-c", "core.editor=true",
                "cherry-pick",
                "--continue",
            ]
            _run_git(
                *continue_args,
            )
            if require_dco_signoff:
                _run_git(
                    repo_dir,
                    "-c", f"user.name={signer.name or 'backport-agent'}",
                    "-c", (
                        f"user.email={signer.email or 'backport-agent@users.noreply.github.com'}"
                    ),
                    "commit",
                    "--amend",
                    "--no-edit",
                    "--signoff",
                )
        except Exception:
            # If cherry-pick --continue fails (e.g. no cherry-pick in
            # progress because all files were staged), commit directly.
            logger.warning("cherry-pick --continue failed, committing directly.")
            commit_args = [
                repo_dir,
                "-c", f"user.name={signer.name or 'backport-agent'}",
                "-c", (
                    f"user.email={signer.email or 'backport-agent@users.noreply.github.com'}"
                ),
                "commit",
                "--allow-empty",
            ]
            if require_dco_signoff:
                commit_args.append("--signoff")
            commit_args.extend(["-m", "Backport with conflict resolutions"])
            _run_git(
                *commit_args,
            )


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def main() -> None:
    """CLI entry point for the backport agent."""
    parser = argparse.ArgumentParser(description="Backport Agent Pipeline")
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
        default=".github/backport-agent.yml",
        help="Path to backport config YAML in the consumer repo",
    )
    parser.add_argument(
        "--token",
        default="",
        help=(
            "GitHub token. Prefer BACKPORT_GITHUB_TOKEN or GITHUB_TOKEN in CI "
            "to avoid putting secrets in process arguments."
        ),
    )
    parser.add_argument(
        "--aws-region", default="us-east-1", help="AWS region for Bedrock",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging",
    )
    parser.add_argument(
        "--push-repo",
        default="",
        help="Push the backport branch to this repo instead of --repo (e.g. your fork)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    github_token = (
        args.token
        or os.environ.get("BACKPORT_GITHUB_TOKEN", "")
        or os.environ.get("GITHUB_TOKEN", "")
    )
    if not github_token:
        parser.error(
            "GitHub token is required via --token, BACKPORT_GITHUB_TOKEN, or GITHUB_TOKEN."
        )

    # Load config from consumer repo
    gh = Github(auth=Auth.Token(github_token))
    config = load_backport_config_from_repo(gh, args.repo, args.config)

    result = run_backport(
        repo_full_name=args.repo,
        source_pr_number=args.pr_number,
        target_branch=args.target_branch,
        config=config,
        github_token=github_token,
        aws_region=args.aws_region,
        push_repo=args.push_repo or None,
    )

    logger.info("Backport outcome: %s", result.outcome)
    if result.outcome == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()

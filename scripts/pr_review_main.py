"""PR reviewer pipeline entry point."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.config import Config as BotocoreConfig
from github import Auth, Github

from scripts.bedrock_client import BedrockClient, PromptClient
from scripts.bedrock_retriever import BedrockRetriever
from scripts.code_reviewer import CodeReviewer, ReviewCoverage
from scripts.comment_publisher import CommentPublisher
from scripts.config import ReviewerConfig, load_reviewer_config, load_reviewer_config_text
from scripts.event_ledger import EventLedger
from scripts.models import DiffScope as _DiffScope
from scripts.models import PullRequestContext, ReviewFinding, ReviewState, SummaryResult
from scripts.path_filter import PathFilter
from scripts.permission_gate import PermissionGate
from scripts.pr_context_fetcher import PRContextFetcher
from scripts.pr_event_router import PREventRouter, load_event_from_path
from scripts.pr_summarizer import PRSummarizer
from scripts.rate_limiter import RateLimiter
from scripts.review_chat import ReviewChat
from scripts.review_policy import collect_review_policy_note, render_review_policy_note
from scripts.review_state_store import ReviewStateStore
from scripts.summary import ReviewWorkflowSummary
from scripts.valkey_repo_context import (
    augment_reviewer_config_for_valkey,
    load_valkey_repo_context,
)

logger = logging.getLogger(__name__)


def _review_subject(repo_name: str, pr_number: int | None) -> str:
    """Build a stable event subject for one reviewed pull request."""
    return f"{repo_name}#{pr_number or 0}"


def _load_runtime_reviewer_config(
    gh: Github,
    repo_name: str,
    config_path: str,
    *,
    ref: str | None = None,
) -> ReviewerConfig:
    """Load reviewer config from GitHub first, then fall back to local disk."""
    try:
        if repo_name:
            repo = gh.get_repo(repo_name)
            config_ref = ref or repo.default_branch
            contents = repo.get_contents(config_path, ref=config_ref)
            if isinstance(contents, list):
                raise ValueError("Reviewer config path resolved to a directory.")
            text = contents.decoded_content.decode("utf-8", errors="replace")
            return load_reviewer_config_text(
                text,
                source=f"{repo_name}@{config_ref}:{config_path}",
            )
    except Exception as exc:
        logger.warning(
            "Could not load reviewer config %s from %s%s: %s.",
            config_path,
            repo_name,
            f" at {ref}" if ref else "",
            exc,
        )

    local_path = Path(config_path)
    if local_path.exists():
        return load_reviewer_config(local_path)

    logger.warning(
        "Could not load reviewer config %s locally. Using defaults.",
        config_path,
    )
    return ReviewerConfig()


def _select_review_files(
    context: PullRequestContext,
    config: ReviewerConfig,
) -> list[str]:
    """Apply configured path filters and max-file limits."""
    selected = PathFilter().select(context.files, config.path_filters)
    return [changed_file.path for changed_file in selected[: config.max_files]]


def _render_summary_comment(
    summary: SummaryResult,
    *,
    policy_note: str = "",
) -> str:
    """Render the summary comment body posted to the pull request."""
    sections = ["## PR Summary"]
    if summary.short_summary:
        sections.extend(["", summary.short_summary])
    sections.extend(["", "### Walkthrough", "", summary.walkthrough])
    if summary.file_groups_markdown:
        sections.extend(["", "### File Groups", "", summary.file_groups_markdown])
    if summary.release_notes:
        sections.extend(["", "### Release Notes", "", summary.release_notes])
    if policy_note:
        sections.extend(["", policy_note])
    return "\n".join(sections).strip()


def _filtered_context(
    context: PullRequestContext,
    allowed_paths: set[str],
) -> PullRequestContext:
    """Return a PR context restricted to the selected reviewable files."""
    return replace(
        context,
        files=[
            changed_file
            for changed_file in context.files
            if changed_file.path in allowed_paths
        ],
    )


def _path_is_mentioned(text: str, path: str) -> bool:
    """Return True when the comment text explicitly references a changed file."""
    if not text:
        return False
    lowered = text.lower()
    normalized_path = path.lower()
    basename = PurePosixPath(path).name.lower()
    if normalized_path in lowered or basename in lowered:
        return True
    stem = PurePosixPath(path).stem.lower()
    if len(stem) >= 4:
        return re.search(rf"\b{re.escape(stem)}\b", lowered) is not None
    return False


def _select_chat_paths(
    selected_paths: list[str],
    thread_path: str | None,
    conversation: list[str],
    prompt: str,
) -> set[str]:
    """Choose the most relevant changed files to hydrate for chat mode."""
    if thread_path:
        return {thread_path} if thread_path in set(selected_paths) else set()
    if not selected_paths:
        return set()

    reference_text = "\n".join(
        part for part in [prompt, *conversation] if part
    )
    mentioned_paths = [
        path for path in selected_paths
        if _path_is_mentioned(reference_text, path)
    ]
    if mentioned_paths:
        return set(mentioned_paths[: min(8, len(mentioned_paths))])
    return set(selected_paths[: min(5, len(selected_paths))])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review a pull request with Bedrock.")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--config", default=".github/pr-review-bot.yml")
    parser.add_argument("--pr-number", type=int, default=None)
    parser.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "review", "chat", "skip"],
    )
    parser.add_argument("--token", required=True)
    parser.add_argument("--state-token", default="")
    parser.add_argument("--state-repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--aws-region", default=os.environ.get("AWS_DEFAULT_REGION", ""))
    parser.add_argument(
        "--event-name",
        default=os.environ.get("GITHUB_EVENT_NAME", ""),
    )
    parser.add_argument(
        "--event-path",
        default=os.environ.get("GITHUB_EVENT_PATH", ""),
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    """Run the PR reviewer pipeline."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    gh = Github(auth=Auth.Token(args.token))
    state_gh = Github(auth=Auth.Token(args.state_token)) if args.state_token else gh
    manual_pr_number = args.pr_number
    event = None
    repo_name = args.repo
    event_ledger: EventLedger | None = None
    if manual_pr_number is not None:
        if args.mode not in {"auto", "review"}:
            logger.error("Manual PR review only supports --mode auto or --mode review.")
            return 2
        resolved_mode = "review"
        summary = ReviewWorkflowSummary(mode=resolved_mode)
    else:
        if not args.event_name or not args.event_path:
            logger.error("Both --event-name and --event-path are required.")
            return 2
        event = load_event_from_path(args.event_name, args.event_path)
        repo_name = args.repo or event.repo
        summary = ReviewWorkflowSummary(mode=args.mode if args.mode != "auto" else "router")
        router = PREventRouter()
        resolved_mode = router.classify_event(event)
        if args.mode != "auto" and args.mode != resolved_mode and resolved_mode != "skip":
            resolved_mode = args.mode
        summary.mode = resolved_mode

    if not repo_name:
        summary.add_result("preflight", "failed", "missing-repository")
        summary.write()
        return 2

    preflight_pr_number = manual_pr_number
    if preflight_pr_number is None and event is not None:
        preflight_pr_number = event.pr_number
    review_subject = _review_subject(repo_name, preflight_pr_number)
    event_ledger = EventLedger(
        gh,
        repo_name,
        state_github_client=state_gh,
        state_repo_full_name=args.state_repo or repo_name,
    )
    event_ledger.record(
        "workflow.run_seen",
        review_subject,
        workflow="pr-review",
        repo=repo_name,
        pr_number=preflight_pr_number or 0,
        mode=resolved_mode,
        event_name=args.event_name or "manual",
    )

    config = _load_runtime_reviewer_config(gh, repo_name, args.config)
    if not config.enabled:
        summary.add_result("preflight", "skipped", "disabled")
        event_ledger.record(
            "review.preflight_skipped",
            review_subject,
            reason="disabled",
            mode=resolved_mode,
        )
        event_ledger.save()
        summary.write()
        return 0
    if event is not None:
        gate = PermissionGate(gh, github_retries=config.github_retries)
        allowed, reason = gate.may_process(event, config)
        if not allowed:
            summary.add_result("preflight", "skipped", reason)
            event_ledger.record(
                "review.preflight_skipped",
                review_subject,
                reason=reason,
                mode=resolved_mode,
            )
            event_ledger.save()
            summary.write()
            return 0

    rate_limiter = RateLimiter(
        config=type(
            "ReviewerBudgetConfig",
            (),
            {
                "max_prs_per_day": 10**9,
                "max_open_bot_prs": 10**9,
                "daily_token_budget": config.daily_token_budget,
            },
        )(),
        github_client=gh,
        repo_full_name=repo_name,
        state_github_client=state_gh,
        state_repo_full_name=args.state_repo or repo_name,
    )
    rate_limiter.load()

    bedrock_timeout_seconds = max(60, int(config.bedrock_timeout_ms / 1000))
    bedrock_client_config = BotocoreConfig(
        read_timeout=bedrock_timeout_seconds,
        connect_timeout=60,
    )
    bedrock_runtime = boto3.client(
        "bedrock-runtime",
        region_name=args.aws_region or None,
        config=bedrock_client_config,
    )
    bedrock_client: PromptClient = BedrockClient(
        config=config,
        client=bedrock_runtime,
        rate_limiter=rate_limiter,
    )
    retriever = None
    retrieval_enabled = config.retrieval.enabled and any([
        config.retrieval.code_knowledge_base_id,
        config.retrieval.docs_knowledge_base_id,
    ])
    if retrieval_enabled:
        retriever = BedrockRetriever(
            boto3.client(
                "bedrock-agent-runtime",
                region_name=args.aws_region or None,
                config=bedrock_client_config,
            ),
            metric_recorder=rate_limiter.record_ai_metric,
        )
    fetcher = PRContextFetcher(gh, github_retries=config.github_retries)
    publisher = CommentPublisher(gh, github_retries=config.github_retries)
    state_store = ReviewStateStore(state_gh, args.state_repo or repo_name)

    had_failure = False

    try:
        if manual_pr_number is not None:
            pr_number = manual_pr_number
        else:
            assert event is not None
            pr_number = event.pr_number or 0
        pr_context = fetcher.fetch(repo_name, pr_number)
        valkey_context = load_valkey_repo_context(gh, repo_name, ref=pr_context.base_sha)
        config = augment_reviewer_config_for_valkey(config, pr_context, valkey_context)
        review_subject = _review_subject(repo_name, pr_context.number)
        if config.ignore_keyword and config.ignore_keyword in (pr_context.body or ""):
            summary.add_result("preflight", "skipped", "ignored-by-keyword")
            event_ledger.record(
                "review.preflight_skipped",
                review_subject,
                reason="ignored-by-keyword",
                mode=resolved_mode,
            )
            summary.write()
            return 0

        selected_paths = _select_review_files(pr_context, config)
        selected_path_set = set(selected_paths)
        pr_context = fetcher.hydrate_contents(pr_context, selected_path_set)
        review_context = _filtered_context(pr_context, selected_path_set)
        current_state = state_store.load(repo_name, pr_context.number)
        last_reviewed_head_sha = (
            current_state.last_reviewed_head_sha if current_state else None
        )
        review_completed_for_head = False

        if resolved_mode == "chat":
            assert event is not None
            if event.comment_id is None:
                summary.add_result("chat", "skipped", "missing-comment-id")
                event_ledger.record(
                    "review.chat_skipped",
                    review_subject,
                    reason="missing-comment-id",
                )
                summary.write()
                return 0

            thread = fetcher.fetch_review_thread(
                repo_name,
                pr_context.number,
                event.comment_id,
                review_comment=event.is_review_comment,
            )
            if event.is_review_comment and not thread.reply_to_bot:
                summary.add_result("chat", "skipped", "unsupported-comment-context")
                event_ledger.record(
                    "review.chat_skipped",
                    review_subject,
                    reason="unsupported-comment-context",
                    comment_id=event.comment_id,
                )
                summary.write()
                return 0
            relevant_paths = _select_chat_paths(
                selected_paths,
                thread.path if event.is_review_comment else None,
                thread.conversation,
                event.body or "",
            )
            chat_context = _filtered_context(
                fetcher.hydrate_contents(pr_context, relevant_paths),
                relevant_paths,
            )
            reply = ReviewChat(
                bedrock_client,
                retriever=retriever,
                retrieval_config=config.retrieval,
                github_client=gh,
                ).reply(
                    chat_context,
                    thread,
                    event.body or "",
                    config,
                    requester=event.actor,
                )
            publisher.publish_chat_reply(
                repo_name,
                pr_context.number,
                event.comment_id,
                reply,
                review_comment=event.is_review_comment,
            )
            summary.add_result("chat", "performed", "reply-posted")
            event_ledger.record(
                "review.chat_replied",
                review_subject,
                comment_id=event.comment_id,
                review_comment=event.is_review_comment,
                relevant_paths=sorted(relevant_paths),
            )
            summary.write()
            return 0

        summary_comment_id = current_state.summary_comment_id if current_state else None
        short_summary = ""
        try:
            policy_note = ""
            if config.post_policy_notes:
                policy_note = render_review_policy_note(
                    collect_review_policy_note(pr_context)
                )
            summary_result = PRSummarizer(
                bedrock_client,
                retriever=retriever,
                retrieval_config=config.retrieval,
            ).summarize(
                review_context,
                config,
            )
            short_summary = summary_result.short_summary
            summary_comment_id = publisher.upsert_summary(
                repo_name,
                pr_context.number,
                summary_comment_id,
                _render_summary_comment(summary_result, policy_note=policy_note),
            )
            summary.add_result("summary", "performed", "comment-upserted")
            event_ledger.record(
                "review.summary_posted",
                review_subject,
                summary_comment_id=summary_comment_id or 0,
                release_notes=bool(summary_result.release_notes),
                has_short_summary=bool(summary_result.short_summary.strip()),
            )
        except Exception as exc:
            had_failure = True
            logger.warning("PR summary failed for %s#%d: %s", repo_name, pr_context.number, exc)
            summary.add_result("summary", "failed", str(exc))
            event_ledger.record(
                "review.summary_failed",
                review_subject,
                error=str(exc),
            )

        review_comment_ids: list[int] = (
            list(current_state.review_comment_ids) if current_state else []
        )

        if config.disable_review:
            summary.add_result("review", "skipped", "disabled")
            event_ledger.record(
                "review.skipped",
                review_subject,
                reason="disabled",
            )
        else:
            try:
                diff_scope = fetcher.build_diff_scope(
                    review_context,
                    last_reviewed_head_sha,
                )
                reviewer = CodeReviewer(
                    bedrock_client,
                    retriever=retriever,
                    retrieval_config=config.retrieval,
                    github_client=gh,
                )
                if reviewer.classify_simple_change(diff_scope.files) and not config.review_simple_changes:
                    detail = "simple-change" if diff_scope.files else "no-new-files"
                    summary.add_result("review", "skipped", detail)
                    event_ledger.record(
                        "review.skipped",
                        review_subject,
                        reason=detail,
                        file_count=len(diff_scope.files),
                    )
                    review_completed_for_head = True
                else:
                    # Per-file triage: use light model to skip trivial files
                    triaged_files = reviewer.triage_files(
                        diff_scope.files, review_context, config,
                    )
                    if not triaged_files:
                        summary.add_result("review", "skipped", "all-files-approved-by-triage")
                        event_ledger.record(
                            "review.skipped",
                            review_subject,
                            reason="all-files-approved-by-triage",
                            file_count=len(diff_scope.files),
                        )
                        review_completed_for_head = True
                    else:
                        triaged_scope = _DiffScope(
                            base_sha=diff_scope.base_sha,
                            head_sha=diff_scope.head_sha,
                            files=triaged_files,
                            incremental=diff_scope.incremental,
                        )
                        if config.specialist_mode:
                            from scripts.specialist_reviewer import SpecialistReviewer

                            specialist = SpecialistReviewer(bedrock_client)
                            specialist_result = specialist.review(
                                review_context,
                                config,
                                selected_paths,
                            )
                            findings = [
                                ReviewFinding(
                                    path=sf.path,
                                    line=sf.line,
                                    body=sf.description,
                                    severity=sf.severity,
                                    title=sf.title,
                                )
                                for sf in specialist_result.findings
                            ]
                            # Post the synthesis markdown as the summary comment
                            if specialist_result.markdown_summary:
                                summary_comment_id = publisher.upsert_summary(
                                    repo_name,
                                    pr_context.number,
                                    summary_comment_id,
                                    specialist_result.markdown_summary,
                                )
                        else:
                            findings = reviewer.review(
                                review_context, triaged_scope, config,
                                short_summary=short_summary,
                            )
                        coverage_report: ReviewCoverage | None = None
                        get_coverage = getattr(reviewer, "get_last_review_coverage", None)
                        if callable(get_coverage):
                            candidate = get_coverage()
                            if isinstance(candidate, ReviewCoverage):
                                coverage_report = candidate
                        if findings:
                            published_ids = publisher.publish_review_comments(
                                repo_name,
                                pr_context.number,
                                findings,
                                commit_sha=pr_context.head_sha,
                            )
                            review_comment_ids.extend(
                                comment_id
                                for comment_id in published_ids
                                if comment_id not in review_comment_ids
                            )
                            comments_published = bool(published_ids)
                            summary.add_result(
                                "review",
                                "performed" if comments_published else "failed",
                                (
                                    f"{len(published_ids)} comment(s), "
                                    f"{len(diff_scope.files) - len(triaged_files)} "
                                    "file(s) auto-approved"
                                ),
                            )
                            event_ledger.record(
                                "review.comments_posted" if comments_published else "review.failed",
                                review_subject,
                                comments=len(published_ids),
                                triaged_file_count=len(triaged_files),
                                auto_approved_file_count=len(diff_scope.files) - len(triaged_files),
                                reason=(
                                    "publish-review-comments-returned-no-comment-ids"
                                    if not comments_published
                                    else ""
                                ),
                            )
                            if not comments_published:
                                had_failure = True
                            if coverage_report is not None and not coverage_report.complete:
                                review_id = publisher.publish_review_note(
                                    repo_name,
                                    pr_context.number,
                                    coverage_report.render_review_note(),
                                    commit_sha=pr_context.head_sha,
                                )
                                if review_id and review_id not in review_comment_ids:
                                    review_comment_ids.append(review_id)
                                event_ledger.record(
                                    "review.note_posted",
                                    review_subject,
                                    note_kind="coverage-incomplete",
                                    review_id=review_id or 0,
                                    unaccounted_files=len(coverage_report.unaccounted_files),
                                )
                            review_completed_for_head = (
                                comments_published
                                and (
                                    coverage_report.complete
                                    if coverage_report is not None
                                    else True
                                )
                            )
                        else:
                            if coverage_report is not None and not coverage_report.approvable:
                                review_id = publisher.publish_review_note(
                                    repo_name,
                                    pr_context.number,
                                    coverage_report.render_review_note(),
                                    commit_sha=pr_context.head_sha,
                                )
                                if review_id and review_id not in review_comment_ids:
                                    review_comment_ids.append(review_id)
                                summary.add_result(
                                    "review",
                                    "performed",
                                    "approval withheld (incomplete review coverage)",
                                )
                                event_ledger.record(
                                    "review.note_posted",
                                    review_subject,
                                    note_kind="approval-withheld",
                                    review_id=review_id or 0,
                                    unaccounted_files=len(coverage_report.unaccounted_files),
                                )
                            else:
                                if config.approve_on_no_findings:
                                    publisher.approve_pr(
                                        repo_name,
                                        pr_context.number,
                                        body="LGTM",
                                        commit_sha=pr_context.head_sha,
                                    )
                                    detail = "approved (no issues found)"
                                    event_ledger.record(
                                        "review.approved",
                                        review_subject,
                                        reason="no-issues-found",
                                        reviewed_file_count=len(triaged_files),
                                    )
                                else:
                                    review_id = publisher.publish_review_note(
                                        repo_name,
                                        pr_context.number,
                                        (
                                            "Automated review found no actionable "
                                            "issues in this pass. It is not approving "
                                            "automatically."
                                        ),
                                        commit_sha=pr_context.head_sha,
                                    )
                                    if review_id and review_id not in review_comment_ids:
                                        review_comment_ids.append(review_id)
                                    detail = "no actionable issues (approval disabled)"
                                    event_ledger.record(
                                        "review.note_posted",
                                        review_subject,
                                        note_kind="no-findings-approval-disabled",
                                        review_id=review_id or 0,
                                        reviewed_file_count=len(triaged_files),
                                    )
                                review_completed_for_head = True
                                summary.add_result(
                                    "review",
                                    "performed",
                                    detail,
                                )
            except Exception as exc:
                had_failure = True
                logger.warning("PR review failed for %s#%d: %s", repo_name, pr_context.number, exc)
                summary.add_result("review", "failed", str(exc))
                event_ledger.record(
                    "review.failed",
                    review_subject,
                    error=str(exc),
                )

        state_store.save(
            ReviewState(
                repo=repo_name,
                pr_number=pr_context.number,
                last_reviewed_head_sha=(
                    pr_context.head_sha
                    if review_completed_for_head
                    else last_reviewed_head_sha
                ),
                summary_comment_id=summary_comment_id,
                review_comment_ids=review_comment_ids,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        summary.add_result("state", "saved", None)
        event_ledger.record(
            "review.state_saved",
            review_subject,
            review_completed_for_head=review_completed_for_head,
            last_reviewed_head_sha=(
                pr_context.head_sha
                if review_completed_for_head
                else last_reviewed_head_sha or ""
            ),
            review_comment_count=len(review_comment_ids),
            summary_comment_id=summary_comment_id or 0,
        )
        summary.write()
        return 1 if had_failure else 0
    except Exception as exc:
        logger.exception("PR reviewer pipeline failed: %s", exc)
        summary.add_result("pipeline", "failed", str(exc))
        if event_ledger is not None:
            event_ledger.record(
                "pipeline.failed",
                review_subject,
                workflow="pr-review",
                error=str(exc),
            )
        summary.write()
        return 1
    finally:
        rate_limiter.save()
        if event_ledger is not None:
            event_ledger.save()


def main() -> None:
    """CLI wrapper around ``run``."""
    sys.exit(run())


if __name__ == "__main__":
    main()

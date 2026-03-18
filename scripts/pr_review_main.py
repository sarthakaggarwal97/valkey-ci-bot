"""PR reviewer pipeline entry point."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from github import Auth, Github

from scripts.bedrock_client import BedrockClient, PromptClient
from scripts.bedrock_retriever import BedrockRetriever
from scripts.code_reviewer import CodeReviewer
from scripts.comment_publisher import CommentPublisher
from scripts.config import ReviewerConfig, load_reviewer_config, load_reviewer_config_text
from scripts.models import PullRequestContext, ReviewState, SummaryResult
from scripts.models import DiffScope as _DiffScope
from scripts.path_filter import PathFilter
from scripts.permission_gate import PermissionGate
from scripts.pr_context_fetcher import PRContextFetcher
from scripts.pr_event_router import PREventRouter, load_event_from_path
from scripts.pr_summarizer import PRSummarizer
from scripts.rate_limiter import RateLimiter
from scripts.review_chat import ReviewChat
from scripts.review_state_store import ReviewStateStore
from scripts.summary import ReviewWorkflowSummary

logger = logging.getLogger(__name__)


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


def _render_summary_comment(summary: SummaryResult) -> str:
    """Render the summary comment body posted to the pull request."""
    sections = ["## PR Summary"]
    if summary.short_summary:
        sections.extend(["", summary.short_summary])
    sections.extend(["", "### Walkthrough", "", summary.walkthrough])
    if summary.file_groups_markdown:
        sections.extend(["", "### File Groups", "", summary.file_groups_markdown])
    if summary.release_notes:
        sections.extend(["", "### Release Notes", "", summary.release_notes])
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

    config = _load_runtime_reviewer_config(gh, repo_name, args.config)
    if not config.enabled:
        summary.add_result("preflight", "skipped", "disabled")
        summary.write()
        return 0
    if event is not None:
        gate = PermissionGate(gh, github_retries=config.github_retries)
        allowed, reason = gate.may_process(event, config)
        if not allowed:
            summary.add_result("preflight", "skipped", reason)
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

    bedrock_runtime = boto3.client(
        "bedrock-runtime",
        region_name=args.aws_region or None,
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
            boto3.client("bedrock-agent-runtime", region_name=args.aws_region or None),
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
        if config.ignore_keyword and config.ignore_keyword in (pr_context.body or ""):
            summary.add_result("preflight", "skipped", "ignored-by-keyword")
            summary.write()
            return 0

        selected_paths = _select_review_files(pr_context, config)
        selected_path_set = set(selected_paths)
        pr_context = fetcher.hydrate_contents(pr_context, selected_path_set)
        review_context = _filtered_context(pr_context, selected_path_set)
        current_state = state_store.load(repo_name, pr_context.number)

        if resolved_mode == "chat":
            assert event is not None
            if event.comment_id is None:
                summary.add_result("chat", "skipped", "missing-comment-id")
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
            )
            publisher.publish_chat_reply(
                repo_name,
                pr_context.number,
                event.comment_id,
                reply,
                review_comment=event.is_review_comment,
            )
            summary.add_result("chat", "performed", "reply-posted")
            summary.write()
            return 0

        summary_comment_id = current_state.summary_comment_id if current_state else None
        short_summary = ""
        try:
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
                _render_summary_comment(summary_result),
            )
            summary.add_result("summary", "performed", "comment-upserted")
        except Exception as exc:
            had_failure = True
            logger.warning("PR summary failed for %s#%d: %s", repo_name, pr_context.number, exc)
            summary.add_result("summary", "failed", str(exc))

        review_comment_ids: list[int] = (
            list(current_state.review_comment_ids) if current_state else []
        )

        if config.disable_review:
            summary.add_result("review", "skipped", "disabled")
        else:
            try:
                diff_scope = fetcher.build_diff_scope(
                    review_context,
                    current_state.last_reviewed_head_sha if current_state else None,
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
                else:
                    # Per-file triage: use light model to skip trivial files
                    triaged_files = reviewer.triage_files(
                        diff_scope.files, review_context, config,
                    )
                    if not triaged_files:
                        summary.add_result("review", "skipped", "all-files-approved-by-triage")
                    else:
                        triaged_scope = _DiffScope(
                            base_sha=diff_scope.base_sha,
                            head_sha=diff_scope.head_sha,
                            files=triaged_files,
                            incremental=diff_scope.incremental,
                        )
                        findings = reviewer.review(
                            review_context, triaged_scope, config,
                            short_summary=short_summary,
                        )
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
                            summary.add_result(
                                "review",
                                "performed",
                                f"{len(published_ids)} comment(s), {len(diff_scope.files) - len(triaged_files)} file(s) auto-approved",
                            )
                        else:
                            publisher.approve_pr(
                                repo_name,
                                pr_context.number,
                                body="LGTM",
                                commit_sha=pr_context.head_sha,
                            )
                            summary.add_result(
                                "review",
                                "performed",
                                "approved (no issues found)",
                            )
            except Exception as exc:
                had_failure = True
                logger.warning("PR review failed for %s#%d: %s", repo_name, pr_context.number, exc)
                summary.add_result("review", "failed", str(exc))

        state_store.save(
            ReviewState(
                repo=repo_name,
                pr_number=pr_context.number,
                last_reviewed_head_sha=pr_context.head_sha,
                summary_comment_id=summary_comment_id,
                review_comment_ids=review_comment_ids,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        summary.add_result("state", "saved", None)
        summary.write()
        return 1 if had_failure else 0
    except Exception as exc:
        logger.exception("PR reviewer pipeline failed: %s", exc)
        summary.add_result("pipeline", "failed", str(exc))
        summary.write()
        return 1
    finally:
        rate_limiter.save()


def main() -> None:
    """CLI wrapper around ``run``."""
    sys.exit(run())


if __name__ == "__main__":
    main()

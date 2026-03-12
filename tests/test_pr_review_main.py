"""Tests for the PR reviewer orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.config import RetrievalConfig, ReviewerConfig
from scripts.models import (
    ChangedFile,
    PullRequestContext,
    ReviewState,
    SummaryResult,
)
from scripts.pr_review_main import _filtered_context, _select_review_files, run


def _event_file(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload))
    return path


def _context() -> PullRequestContext:
    return PullRequestContext(
        repo="owner/repo",
        number=11,
        title="Improve failover timing",
        body="Details",
        base_sha="base123",
        head_sha="head456",
        author="alice",
        files=[
            ChangedFile(
                path="src/failover.c",
                status="modified",
                additions=5,
                deletions=1,
                patch="@@ -1 +1 @@\n-old\n+new",
                contents="int failover(void) { return 1; }",
                is_binary=False,
            )
        ],
    )


def test_select_review_files_applies_path_filters() -> None:
    context = PullRequestContext(
        repo="owner/repo",
        number=11,
        title="Improve failover timing",
        body="Details",
        base_sha="base123",
        head_sha="head456",
        author="alice",
        files=[
            ChangedFile(
                path="src/failover.c",
                status="modified",
                additions=5,
                deletions=1,
                patch="patch",
                contents=None,
                is_binary=False,
            ),
            ChangedFile(
                path="docs/readme.md",
                status="modified",
                additions=2,
                deletions=0,
                patch="patch",
                contents=None,
                is_binary=False,
            ),
        ],
    )

    selected = _select_review_files(context, ReviewerConfig(path_filters=["src/**"]))

    assert selected == ["src/failover.c"]


def test_filtered_context_restricts_files() -> None:
    filtered = _filtered_context(_context(), {"src/failover.c"})

    assert [changed_file.path for changed_file in filtered.files] == ["src/failover.c"]


@patch("scripts.pr_review_main.boto3.client")
@patch("scripts.pr_review_main.Github")
@patch("scripts.pr_review_main.RateLimiter")
@patch("scripts.pr_review_main.ReviewStateStore")
@patch("scripts.pr_review_main.CommentPublisher")
@patch("scripts.pr_review_main.PRContextFetcher")
def test_run_review_mode_posts_summary_and_review(
    mock_fetcher_cls,
    mock_publisher_cls,
    mock_state_store_cls,
    mock_rate_limiter_cls,
    mock_github_cls,
    _mock_boto_client,
    tmp_path,
) -> None:
    payload = {
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "alice"},
        "pull_request": {"number": 11, "body": "Details"},
    }
    event_path = _event_file(tmp_path, payload)

    fetcher = mock_fetcher_cls.return_value
    fetcher.fetch.return_value = _context()
    fetcher.hydrate_contents.side_effect = lambda context, _paths: context
    fetcher.build_diff_scope.return_value = MagicMock(files=_context().files)

    publisher = mock_publisher_cls.return_value
    publisher.upsert_summary.return_value = 99
    publisher.publish_review_comments.return_value = [1001]

    state_store = mock_state_store_cls.return_value
    state_store.load.return_value = ReviewState(
        repo="owner/repo",
        pr_number=11,
        last_reviewed_head_sha="oldsha",
        summary_comment_id=55,
        review_comment_ids=[],
        updated_at="2026-03-12T00:00:00+00:00",
    )

    mock_rate_limiter_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.save.return_value = None
    mock_github_cls.return_value = MagicMock()

    with patch(
        "scripts.pr_review_main._load_runtime_reviewer_config",
        return_value=ReviewerConfig(),
    ), patch(
        "scripts.pr_review_main.PRSummarizer"
    ) as mock_summarizer_cls, patch(
        "scripts.pr_review_main.CodeReviewer"
    ) as mock_reviewer_cls:
        mock_summarizer_cls.return_value.summarize.return_value = SummaryResult(
            walkthrough="Summary",
            file_groups_markdown="- Core",
            release_notes="Release note",
        )
        mock_reviewer = mock_reviewer_cls.return_value
        mock_reviewer.classify_simple_change.return_value = False
        mock_reviewer.review.return_value = [
            MagicMock(path="src/failover.c", line=12, body="Risk", severity="high")
        ]

        exit_code = run(
            [
                "--repo",
                "owner/repo",
                "--mode",
                "review",
                "--token",
                "token",
                "--event-name",
                "pull_request_target",
                "--event-path",
                str(event_path),
            ]
        )

    assert exit_code == 0
    publisher.upsert_summary.assert_called_once()
    publisher.publish_review_comments.assert_called_once()
    state_store.save.assert_called_once()


@patch("scripts.pr_review_main.boto3.client")
@patch("scripts.pr_review_main.Github")
@patch("scripts.pr_review_main.RateLimiter")
@patch("scripts.pr_review_main.ReviewStateStore")
@patch("scripts.pr_review_main.CommentPublisher")
@patch("scripts.pr_review_main.PRContextFetcher")
def test_run_review_mode_wires_retriever_when_enabled(
    mock_fetcher_cls,
    mock_publisher_cls,
    mock_state_store_cls,
    mock_rate_limiter_cls,
    mock_github_cls,
    mock_boto_client,
    tmp_path,
) -> None:
    payload = {
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "alice"},
        "pull_request": {"number": 11, "body": "Details"},
    }
    event_path = _event_file(tmp_path, payload)

    fetcher = mock_fetcher_cls.return_value
    fetcher.fetch.return_value = _context()
    fetcher.hydrate_contents.side_effect = lambda context, _paths: context
    fetcher.build_diff_scope.return_value = MagicMock(files=_context().files)

    mock_publisher_cls.return_value.upsert_summary.return_value = 99
    mock_publisher_cls.return_value.publish_review_comments.return_value = []
    mock_state_store_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.save.return_value = None
    mock_github_cls.return_value = MagicMock()
    mock_boto_client.side_effect = [MagicMock(), MagicMock()]

    config = ReviewerConfig()
    config.retrieval = RetrievalConfig(enabled=True, code_knowledge_base_id="CODEKB")

    with patch(
        "scripts.pr_review_main._load_runtime_reviewer_config",
        return_value=config,
    ), patch(
        "scripts.pr_review_main.PRSummarizer"
    ) as mock_summarizer_cls, patch(
        "scripts.pr_review_main.CodeReviewer"
    ) as mock_reviewer_cls:
        mock_summarizer_cls.return_value.summarize.return_value = SummaryResult(
            walkthrough="Summary",
            file_groups_markdown="- Core",
            release_notes="Release note",
        )
        mock_reviewer_cls.return_value.classify_simple_change.return_value = False
        mock_reviewer_cls.return_value.review.return_value = []

        exit_code = run(
            [
                "--repo",
                "owner/repo",
                "--mode",
                "review",
                "--token",
                "token",
                "--event-name",
                "pull_request_target",
                "--event-path",
                str(event_path),
                "--aws-region",
                "us-east-1",
            ]
        )

    assert exit_code == 0
    mock_boto_client.assert_any_call("bedrock-runtime", region_name="us-east-1")
    mock_boto_client.assert_any_call("bedrock-agent-runtime", region_name="us-east-1")
    assert mock_summarizer_cls.call_args.kwargs["retriever"] is not None
    assert mock_reviewer_cls.call_args.kwargs["retriever"] is not None


@patch("scripts.pr_review_main.boto3.client")
@patch("scripts.pr_review_main.Github")
@patch("scripts.pr_review_main.RateLimiter")
@patch("scripts.pr_review_main.ReviewStateStore")
@patch("scripts.pr_review_main.CommentPublisher")
@patch("scripts.pr_review_main.PRContextFetcher")
def test_run_review_mode_skips_retriever_client_without_kb_ids(
    mock_fetcher_cls,
    mock_publisher_cls,
    mock_state_store_cls,
    mock_rate_limiter_cls,
    mock_github_cls,
    mock_boto_client,
    tmp_path,
) -> None:
    payload = {
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "alice"},
        "pull_request": {"number": 11, "body": "Details"},
    }
    event_path = _event_file(tmp_path, payload)

    fetcher = mock_fetcher_cls.return_value
    fetcher.fetch.return_value = _context()
    fetcher.hydrate_contents.side_effect = lambda context, _paths: context
    fetcher.build_diff_scope.return_value = MagicMock(files=_context().files)

    mock_publisher_cls.return_value.upsert_summary.return_value = 99
    mock_publisher_cls.return_value.publish_review_comments.return_value = []
    mock_state_store_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.save.return_value = None
    mock_github_cls.return_value = MagicMock()
    mock_boto_client.return_value = MagicMock()

    config = ReviewerConfig()
    config.retrieval.enabled = True

    with patch(
        "scripts.pr_review_main._load_runtime_reviewer_config",
        return_value=config,
    ), patch(
        "scripts.pr_review_main.PRSummarizer"
    ) as mock_summarizer_cls, patch(
        "scripts.pr_review_main.CodeReviewer"
    ) as mock_reviewer_cls:
        mock_summarizer_cls.return_value.summarize.return_value = SummaryResult(
            walkthrough="Summary",
            file_groups_markdown="- Core",
            release_notes="Release note",
        )
        mock_reviewer_cls.return_value.classify_simple_change.return_value = False
        mock_reviewer_cls.return_value.review.return_value = []

        exit_code = run(
            [
                "--repo",
                "owner/repo",
                "--mode",
                "review",
                "--token",
                "token",
                "--event-name",
                "pull_request_target",
                "--event-path",
                str(event_path),
                "--aws-region",
                "us-east-1",
            ]
        )

    assert exit_code == 0
    mock_boto_client.assert_called_once_with("bedrock-runtime", region_name="us-east-1")


@patch("scripts.pr_review_main.boto3.client")
@patch("scripts.pr_review_main.Github")
@patch("scripts.pr_review_main.RateLimiter")
@patch("scripts.pr_review_main.ReviewStateStore")
@patch("scripts.pr_review_main.CommentPublisher")
@patch("scripts.pr_review_main.PRContextFetcher")
def test_run_review_mode_returns_nonzero_when_review_generation_is_unparseable(
    mock_fetcher_cls,
    mock_publisher_cls,
    mock_state_store_cls,
    mock_rate_limiter_cls,
    mock_github_cls,
    _mock_boto_client,
    tmp_path,
) -> None:
    payload = {
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "alice"},
        "pull_request": {"number": 11, "body": "Details"},
    }
    event_path = _event_file(tmp_path, payload)

    fetcher = mock_fetcher_cls.return_value
    fetcher.fetch.return_value = _context()
    fetcher.hydrate_contents.side_effect = lambda context, _paths: context
    fetcher.build_diff_scope.return_value = MagicMock(files=_context().files)

    mock_publisher_cls.return_value.upsert_summary.return_value = 99
    mock_state_store_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.save.return_value = None
    mock_github_cls.return_value = MagicMock()

    with patch(
        "scripts.pr_review_main._load_runtime_reviewer_config",
        return_value=ReviewerConfig(),
    ), patch(
        "scripts.pr_review_main.PRSummarizer"
    ) as mock_summarizer_cls, patch(
        "scripts.pr_review_main.CodeReviewer"
    ) as mock_reviewer_cls:
        mock_summarizer_cls.return_value.summarize.return_value = SummaryResult(
            walkthrough="Summary",
            file_groups_markdown="- Core",
            release_notes="Release note",
        )
        mock_reviewer = mock_reviewer_cls.return_value
        mock_reviewer.classify_simple_change.return_value = False
        mock_reviewer.review.side_effect = ValueError("Unparseable review response")

        exit_code = run(
            [
                "--repo",
                "owner/repo",
                "--mode",
                "review",
                "--token",
                "token",
                "--event-name",
                "pull_request_target",
                "--event-path",
                str(event_path),
            ]
        )

    assert exit_code == 1
    mock_publisher_cls.return_value.publish_review_comments.assert_not_called()


@patch("scripts.pr_review_main.boto3.client")
@patch("scripts.pr_review_main.Github")
@patch("scripts.pr_review_main.RateLimiter")
@patch("scripts.pr_review_main.ReviewStateStore")
@patch("scripts.pr_review_main.CommentPublisher")
@patch("scripts.pr_review_main.PRContextFetcher")
def test_run_chat_mode_replies_to_review_comment(
    mock_fetcher_cls,
    mock_publisher_cls,
    mock_state_store_cls,
    mock_rate_limiter_cls,
    mock_github_cls,
    _mock_boto_client,
    tmp_path,
) -> None:
    payload = {
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "alice"},
        "pull_request": {"number": 11},
        "comment": {
            "id": 77,
            "body": "Can you suggest a test?",
            "path": "src/failover.c",
            "line": 12,
            "in_reply_to_id": 55,
        },
    }
    event_path = _event_file(tmp_path, payload)

    fetcher = mock_fetcher_cls.return_value
    fetcher.fetch.return_value = _context()
    fetcher.hydrate_contents.side_effect = lambda context, _paths: context
    fetcher.fetch_review_thread.return_value = MagicMock(
        comment_id=77,
        path="src/failover.c",
        line=12,
        conversation=["Can you suggest a test?"],
        reply_to_bot=True,
    )

    mock_publisher_cls.return_value.publish_chat_reply.return_value = 88
    mock_state_store_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.save.return_value = None
    mock_github_cls.return_value = MagicMock()

    with patch(
        "scripts.pr_review_main._load_runtime_reviewer_config",
        return_value=ReviewerConfig(),
    ), patch(
        "scripts.pr_review_main.ReviewChat"
    ) as mock_chat_cls:
        mock_chat_cls.return_value.reply.return_value = "Add a focused timeout test."

        exit_code = run(
            [
                "--repo",
                "owner/repo",
                "--mode",
                "chat",
                "--token",
                "token",
                "--event-name",
                "pull_request_review_comment",
                "--event-path",
                str(event_path),
            ]
        )

    assert exit_code == 0
    mock_publisher_cls.return_value.publish_chat_reply.assert_called_once()


@patch("scripts.pr_review_main.boto3.client")
@patch("scripts.pr_review_main.Github")
@patch("scripts.pr_review_main.RateLimiter")
@patch("scripts.pr_review_main.ReviewStateStore")
@patch("scripts.pr_review_main.CommentPublisher")
@patch("scripts.pr_review_main.PRContextFetcher")
def test_run_chat_mode_skips_non_bot_review_thread(
    mock_fetcher_cls,
    mock_publisher_cls,
    mock_state_store_cls,
    mock_rate_limiter_cls,
    mock_github_cls,
    _mock_boto_client,
    tmp_path,
) -> None:
    payload = {
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "alice"},
        "pull_request": {"number": 11},
        "comment": {
            "id": 77,
            "body": "Can you suggest a test?",
            "path": "src/failover.c",
            "line": 12,
            "in_reply_to_id": 55,
        },
    }
    event_path = _event_file(tmp_path, payload)

    fetcher = mock_fetcher_cls.return_value
    fetcher.fetch.return_value = _context()
    fetcher.hydrate_contents.side_effect = lambda context, _paths: context
    fetcher.fetch_review_thread.return_value = MagicMock(
        comment_id=77,
        path="src/failover.c",
        line=12,
        conversation=["Can you suggest a test?"],
        reply_to_bot=False,
    )

    mock_state_store_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.save.return_value = None
    mock_github_cls.return_value = MagicMock()

    with patch(
        "scripts.pr_review_main._load_runtime_reviewer_config",
        return_value=ReviewerConfig(),
    ), patch(
        "scripts.pr_review_main.ReviewChat"
    ) as mock_chat_cls:
        exit_code = run(
            [
                "--repo",
                "owner/repo",
                "--mode",
                "chat",
                "--token",
                "token",
                "--event-name",
                "pull_request_review_comment",
                "--event-path",
                str(event_path),
            ]
        )

    assert exit_code == 0
    mock_chat_cls.return_value.reply.assert_not_called()
    mock_publisher_cls.return_value.publish_chat_reply.assert_not_called()


@patch("scripts.pr_review_main.boto3.client")
@patch("scripts.pr_review_main.Github")
@patch("scripts.pr_review_main.RateLimiter")
@patch("scripts.pr_review_main.ReviewStateStore")
@patch("scripts.pr_review_main.CommentPublisher")
@patch("scripts.pr_review_main.PRContextFetcher")
def test_run_chat_mode_does_not_use_unrelated_file_context_for_filtered_thread(
    mock_fetcher_cls,
    mock_publisher_cls,
    mock_state_store_cls,
    mock_rate_limiter_cls,
    mock_github_cls,
    _mock_boto_client,
    tmp_path,
) -> None:
    payload = {
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "alice"},
        "pull_request": {"number": 11},
        "comment": {
            "id": 77,
            "body": "Can you suggest a test?",
            "path": "docs/readme.md",
            "line": 12,
            "in_reply_to_id": 55,
        },
    }
    event_path = _event_file(tmp_path, payload)

    fetcher = mock_fetcher_cls.return_value
    fetcher.fetch.return_value = _context()
    fetcher.hydrate_contents.side_effect = lambda context, _paths: context
    fetcher.fetch_review_thread.return_value = MagicMock(
        comment_id=77,
        path="docs/readme.md",
        line=12,
        conversation=["Can you suggest a test?"],
        reply_to_bot=True,
    )

    mock_publisher_cls.return_value.publish_chat_reply.return_value = 88
    mock_state_store_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.load.return_value = None
    mock_rate_limiter_cls.return_value.save.return_value = None
    mock_github_cls.return_value = MagicMock()

    with patch(
        "scripts.pr_review_main._load_runtime_reviewer_config",
        return_value=ReviewerConfig(path_filters=["src/**"]),
    ), patch(
        "scripts.pr_review_main.ReviewChat"
    ) as mock_chat_cls:
        mock_chat_cls.return_value.reply.return_value = "Answer"

        exit_code = run(
            [
                "--repo",
                "owner/repo",
                "--mode",
                "chat",
                "--token",
                "token",
                "--event-name",
                "pull_request_review_comment",
                "--event-path",
                str(event_path),
            ]
        )

    assert exit_code == 0
    assert fetcher.hydrate_contents.call_args_list[-1].args[1] == set()
    mock_publisher_cls.return_value.publish_chat_reply.assert_called_once()

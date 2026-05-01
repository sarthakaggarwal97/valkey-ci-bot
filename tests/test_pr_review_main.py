"""Tests for the PR reviewer orchestrator."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scripts.claude_reviewer import ReviewGenerationError
from scripts.config import ReviewerConfig
from scripts.models import (
    ChangedFile,
    DiffScope,
    PullRequestContext,
    ReviewFinding,
    ReviewState,
    SummaryResult,
)
from scripts.pr_review_main import (
    _filtered_context,
    _load_runtime_reviewer_config,
    _render_summary_comment,
    _select_chat_paths,
    _select_review_files,
    run,
)


@pytest.fixture(autouse=True)
def _mock_event_ledger():
    with patch("scripts.pr_review_main.EventLedger") as mock_cls:
        yield mock_cls.return_value


def _event_file(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
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
        base_ref="unstable",
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


def _multi_file_context() -> PullRequestContext:
    context = _context()
    context.files.append(
        ChangedFile(
            path="tests/failover_timeout.tcl",
            status="modified",
            additions=3,
            deletions=0,
            patch="@@ -1 +1 @@\n-old\n+new",
            contents="test failover timeout",
            is_binary=False,
        )
    )
    return context


def _diff_scope(context: PullRequestContext, incremental: bool = False) -> DiffScope:
    return DiffScope(
        base_sha=context.base_sha,
        head_sha=context.head_sha,
        files=context.files,
        incremental=incremental,
    )


@contextmanager
def _patched_run_dependencies(
    *,
    config: ReviewerConfig | None = None,
    clone_error: Exception | None = None,
):
    config = config or ReviewerConfig()
    target_gh = MagicMock(name="target_gh")
    state_gh = MagicMock(name="state_gh")
    with patch("scripts.pr_review_main.PRContextFetcher") as mock_fetcher_cls, \
        patch("scripts.pr_review_main.CommentPublisher") as mock_publisher_cls, \
        patch("scripts.pr_review_main.ReviewStateStore") as mock_state_store_cls, \
        patch("scripts.pr_review_main.RateLimiter") as mock_rate_limiter_cls, \
        patch("scripts.pr_review_main.Github") as mock_github_cls, \
        patch("scripts.pr_review_main._clone_pr_checkout") as mock_clone_checkout, \
        patch("scripts.pr_review_main._load_runtime_reviewer_config", return_value=config), \
        patch("scripts.pr_review_main.load_valkey_repo_context", return_value=MagicMock()), \
        patch("scripts.pr_review_main.augment_reviewer_config_for_valkey", side_effect=lambda cfg, *_args: cfg), \
        patch("scripts.pr_review_main.claude_summarize_pr", return_value="This PR improves failover timing.") as mock_summarize, \
        patch("scripts.pr_review_main.claude_review_pr", return_value=[]) as mock_review, \
        patch("scripts.pr_review_main.claude_reply_to_comment", return_value="Add a focused timeout test.") as mock_reply:
        mock_github_cls.side_effect = [target_gh, state_gh]
        if clone_error is not None:
            mock_clone_checkout.side_effect = clone_error

        fetcher = mock_fetcher_cls.return_value
        fetcher.fetch.return_value = _context()
        fetcher.hydrate_contents.side_effect = lambda context, _paths: context
        fetcher.build_diff_scope.side_effect = lambda context, _sha: _diff_scope(context)

        publisher = mock_publisher_cls.return_value
        publisher.upsert_summary.return_value = 99
        publisher.publish_review_comments.return_value = [1001]
        publisher.publish_review_note.return_value = 1002
        publisher.publish_chat_reply.return_value = 88

        state_store = mock_state_store_cls.return_value
        state_store.load.return_value = None

        mock_rate_limiter_cls.return_value.load.return_value = None
        mock_rate_limiter_cls.return_value.save.return_value = None

        yield SimpleNamespace(
            target_gh=target_gh,
            state_gh=state_gh,
            fetcher=fetcher,
            publisher=publisher,
            state_store=state_store,
            rate_limiter_cls=mock_rate_limiter_cls,
            state_store_cls=mock_state_store_cls,
            clone_checkout=mock_clone_checkout,
            summarize=mock_summarize,
            review=mock_review,
            reply=mock_reply,
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


def test_select_chat_paths_prefers_explicit_file_mentions() -> None:
    paths = ["src/failover.c", "tests/failover_timeout.tcl"]

    selected = _select_chat_paths(
        paths,
        None,
        ["Can you explain tests/failover_timeout.tcl?"],
        "/reviewbot what changed in tests/failover_timeout.tcl?",
    )

    assert selected == {"tests/failover_timeout.tcl"}


def test_render_summary_comment_uses_short_summary_when_present() -> None:
    rendered = _render_summary_comment(
        SummaryResult(
            walkthrough="Longer walkthrough",
            file_groups_markdown="- Core",
            release_notes="Release note",
            short_summary="Short summary first.",
        ),
        policy_note="### Maintainer Checklist\n\nNo signals.",
    )

    assert "Short summary first." in rendered
    assert "### Walkthrough" in rendered
    assert "Longer walkthrough" in rendered
    assert "### Maintainer Checklist" in rendered


def test_select_chat_paths_falls_back_to_first_five_when_no_file_is_mentioned() -> None:
    paths = [f"src/file{i}.c" for i in range(7)]

    selected = _select_chat_paths(
        paths,
        None,
        ["Can you suggest tests?"],
        "/reviewbot can you suggest tests?",
    )

    assert selected == set(paths[:5])


def test_load_runtime_reviewer_config_prefers_repository_file() -> None:
    gh = MagicMock()
    repo = gh.get_repo.return_value
    repo.default_branch = "main"
    repo.get_contents.return_value = MagicMock(decoded_content=b"enabled: true\n")

    config = _load_runtime_reviewer_config(
        gh,
        "owner/repo",
        ".github/pr-review-bot.yml",
    )

    assert config.enabled is True
    repo.get_contents.assert_called_once_with(".github/pr-review-bot.yml", ref="main")


def test_load_runtime_reviewer_config_falls_back_to_local_file(tmp_path) -> None:
    config_path = tmp_path / "pr-review.yml"
    config_path.write_text("enabled: false\n", encoding="utf-8")
    gh = MagicMock()
    gh.get_repo.side_effect = RuntimeError("missing target config")

    config = _load_runtime_reviewer_config(
        gh,
        "owner/repo",
        str(config_path),
    )

    assert config.enabled is False


def test_run_review_mode_posts_summary_and_review(_mock_event_ledger, tmp_path) -> None:
    payload = {
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "alice"},
        "pull_request": {"number": 11, "body": "Details"},
    }
    event_path = _event_file(tmp_path, payload)

    with _patched_run_dependencies() as deps:
        deps.state_store.load.return_value = ReviewState(
            repo="owner/repo",
            pr_number=11,
            last_reviewed_head_sha="oldsha",
            summary_comment_id=55,
            review_comment_ids=[],
            updated_at="2026-03-12T00:00:00+00:00",
        )
        deps.review.return_value = [
            ReviewFinding(
                path="src/failover.c",
                line=1,
                body="Risk",
                severity="high",
            )
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
    deps.publisher.upsert_summary.assert_called_once()
    deps.publisher.publish_review_comments.assert_called_once()
    deps.summarize.assert_called_once()
    deps.review.assert_called_once()
    assert deps.summarize.call_args.kwargs["config"] is not None
    assert deps.review.call_args.kwargs["config"] is not None
    saved_state = deps.state_store.save.call_args.args[0]
    assert saved_state.last_reviewed_head_sha == "head456"
    _mock_event_ledger.record.assert_any_call(
        "review.comments_posted",
        "owner/repo#11",
        comments=1,
        file_count=1,
        reason="",
    )


def test_run_review_mode_preserves_state_when_review_generation_fails(tmp_path) -> None:
    payload = {
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "alice"},
        "pull_request": {"number": 11, "body": "Details"},
    }
    event_path = _event_file(tmp_path, payload)

    with _patched_run_dependencies() as deps:
        deps.state_store.load.return_value = ReviewState(
            repo="owner/repo",
            pr_number=11,
            last_reviewed_head_sha="oldsha",
            summary_comment_id=55,
            review_comment_ids=[],
            updated_at="2026-03-12T00:00:00+00:00",
        )
        deps.review.side_effect = ReviewGenerationError("unparseable review response")

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
    deps.publisher.publish_review_comments.assert_not_called()
    deps.publisher.publish_review_note.assert_called_once()
    saved_state = deps.state_store.save.call_args.args[0]
    assert saved_state.last_reviewed_head_sha == "oldsha"


def test_run_review_mode_fails_closed_when_checkout_is_unavailable(tmp_path) -> None:
    payload = {
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "alice"},
        "pull_request": {"number": 11, "body": "Details"},
    }
    event_path = _event_file(tmp_path, payload)

    with _patched_run_dependencies(clone_error=RuntimeError("clone failed")) as deps:
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
    deps.summarize.assert_not_called()
    deps.review.assert_not_called()
    deps.publisher.upsert_summary.assert_not_called()
    deps.publisher.publish_review_comments.assert_not_called()
    deps.publisher.publish_review_note.assert_called_once()
    saved_state = deps.state_store.save.call_args.args[0]
    assert saved_state.last_reviewed_head_sha is None


def test_run_manual_review_mode_uses_bot_repo_state() -> None:
    context = _context()
    context.repo = "fork-owner/valkey"

    with _patched_run_dependencies() as deps:
        deps.fetcher.fetch.return_value = context

        exit_code = run(
            [
                "--repo",
                "fork-owner/valkey",
                "--pr-number",
                "17",
                "--mode",
                "review",
                "--token",
                "target-token",
                "--state-token",
                "state-token",
                "--state-repo",
                "sarthakaggarwal97/valkey-ci-agent",
            ]
        )

    assert exit_code == 0
    deps.fetcher.fetch.assert_called_once_with("fork-owner/valkey", 17)
    deps.state_store_cls.assert_called_once_with(
        deps.state_gh,
        "sarthakaggarwal97/valkey-ci-agent",
    )
    rate_kwargs = deps.rate_limiter_cls.call_args.kwargs
    assert rate_kwargs["github_client"] is deps.target_gh
    assert rate_kwargs["state_github_client"] is deps.state_gh
    assert rate_kwargs["state_repo_full_name"] == "sarthakaggarwal97/valkey-ci-agent"
    deps.publisher.upsert_summary.assert_called_once()


def test_run_chat_mode_replies_to_review_comment_with_relevant_context(tmp_path) -> None:
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
    config = ReviewerConfig(chat_collaborator_only=False)

    with _patched_run_dependencies(config=config) as deps:
        deps.fetcher.fetch_review_thread.return_value = MagicMock(
            comment_id=77,
            path="src/failover.c",
            line=12,
            conversation=["Can you suggest a test?"],
            reply_to_bot=True,
        )

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
    deps.publisher.publish_chat_reply.assert_called_once()
    chat_context = deps.reply.call_args.args[0]
    assert [changed_file.path for changed_file in chat_context.files] == ["src/failover.c"]


def test_run_chat_mode_skips_non_bot_review_thread(tmp_path) -> None:
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
    config = ReviewerConfig(chat_collaborator_only=False)

    with _patched_run_dependencies(config=config) as deps:
        deps.fetcher.fetch_review_thread.return_value = MagicMock(
            comment_id=77,
            path="src/failover.c",
            line=12,
            conversation=["Can you suggest a test?"],
            reply_to_bot=False,
        )

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
    deps.reply.assert_not_called()
    deps.publisher.publish_chat_reply.assert_not_called()


def test_run_chat_mode_does_not_use_unrelated_file_context_for_filtered_thread(tmp_path) -> None:
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
    config = ReviewerConfig(path_filters=["src/**"], chat_collaborator_only=False)

    with _patched_run_dependencies(config=config) as deps:
        deps.fetcher.fetch_review_thread.return_value = MagicMock(
            comment_id=77,
            path="docs/readme.md",
            line=12,
            conversation=["Can you suggest a test?"],
            reply_to_bot=True,
        )

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
    chat_context = deps.reply.call_args.args[0]
    assert chat_context.files == []
    deps.publisher.publish_chat_reply.assert_called_once()


def test_run_issue_comment_chat_mode_prefers_mentioned_file_context(tmp_path) -> None:
    payload = {
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "alice"},
        "issue": {"number": 11, "pull_request": {}},
        "comment": {
            "id": 77,
            "body": "/reviewbot what changed in tests/failover_timeout.tcl?",
        },
    }
    event_path = _event_file(tmp_path, payload)
    config = ReviewerConfig(chat_collaborator_only=False)

    with _patched_run_dependencies(config=config) as deps:
        deps.fetcher.fetch.return_value = _multi_file_context()
        deps.fetcher.fetch_review_thread.return_value = MagicMock(
            comment_id=77,
            path=None,
            line=None,
            conversation=["/reviewbot what changed in tests/failover_timeout.tcl?"],
            reply_to_bot=True,
        )

        exit_code = run(
            [
                "--repo",
                "owner/repo",
                "--mode",
                "chat",
                "--token",
                "token",
                "--event-name",
                "issue_comment",
                "--event-path",
                str(event_path),
            ]
        )

    assert exit_code == 0
    chat_context = deps.reply.call_args.args[0]
    assert [changed_file.path for changed_file in chat_context.files] == [
        "tests/failover_timeout.tcl"
    ]
    deps.publisher.publish_chat_reply.assert_called_once()

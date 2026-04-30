"""Tests for PR reviewer comment publishing helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts.comment_publisher import SUMMARY_MARKER, CommentPublisher
from scripts.models import ReviewFinding


def test_upsert_summary_updates_existing_marker_comment() -> None:
    existing = MagicMock()
    existing.id = 42
    existing.body = f"{SUMMARY_MARKER}\nold body"
    existing.user.login = "github-actions[bot]"

    pr = MagicMock()
    pr.get_issue_comments.return_value = [existing]

    repo = MagicMock()
    repo.get_pull.return_value = pr

    gh = MagicMock()
    gh.get_repo.return_value = repo
    gh.get_user.return_value.login = "github-actions[bot]"

    comment_id = CommentPublisher(gh).upsert_summary("owner/repo", 1, None, "new body")

    assert comment_id == 42
    existing.edit.assert_called_once()
    pr.create_issue_comment.assert_not_called()


def test_upsert_summary_ignores_marker_comment_not_authored_by_bot() -> None:
    existing = MagicMock()
    existing.id = 42
    existing.body = f"{SUMMARY_MARKER}\nspoofed"
    existing.user.login = "alice"

    created = MagicMock()
    created.id = 77

    pr = MagicMock()
    pr.get_issue_comments.return_value = [existing]
    pr.create_issue_comment.return_value = created

    repo = MagicMock()
    repo.get_pull.return_value = pr

    gh = MagicMock()
    gh.get_repo.return_value = repo
    gh.get_user.return_value.login = "github-actions[bot]"

    comment_id = CommentPublisher(gh).upsert_summary("owner/repo", 1, None, "new body")

    assert comment_id == 77
    existing.edit.assert_not_called()
    pr.create_issue_comment.assert_called_once()


def test_publish_chat_reply_uses_review_comment_reply_for_review_threads() -> None:
    review_reply = MagicMock()
    review_reply.id = 8

    pr = MagicMock()
    pr.create_review_comment_reply.return_value = review_reply

    repo = MagicMock()
    repo.get_pull.return_value = pr

    gh = MagicMock()
    gh.get_repo.return_value = repo

    reply_id = CommentPublisher(gh).publish_chat_reply(
        "owner/repo",
        1,
        77,
        "reply",
        review_comment=True,
    )

    assert reply_id == 8
    pr.create_review_comment_reply.assert_called_once_with(77, "reply")


def test_publish_review_comments_includes_review_summary_body() -> None:
    pr = MagicMock()
    pr.base.repo._requester.requestJsonAndCheck.return_value = ({}, {"id": 11})

    repo = MagicMock()
    repo.get_pull.return_value = pr

    gh = MagicMock()
    gh.get_repo.return_value = repo

    findings = [
        ReviewFinding(
            path="src/failover.c",
            line=12,
            body="**Top issue**\n\nConfidence: `high`",
            severity="high",
            title="Top issue",
            confidence="high",
        )
    ]

    CommentPublisher(gh).publish_review_comments("owner/repo", 1, findings, commit_sha="head456")

    payload = pr.base.repo._requester.requestJsonAndCheck.call_args.kwargs["input"]
    assert "Automated review found 1 issue" in payload["body"]
    assert payload["comments"][0]["body"] == findings[0].body


def test_publish_review_note_posts_comment_review() -> None:
    pr = MagicMock()
    pr.base.repo._requester.requestJsonAndCheck.return_value = ({}, {"id": 17})

    repo = MagicMock()
    repo.get_pull.return_value = pr

    gh = MagicMock()
    gh.get_repo.return_value = repo

    review_id = CommentPublisher(gh).publish_review_note(
        "owner/repo",
        1,
        "coverage note",
        commit_sha="head456",
    )

    assert review_id == 17
    payload = pr.base.repo._requester.requestJsonAndCheck.call_args.kwargs["input"]
    assert payload["body"] == "coverage note"
    assert payload["event"] == "COMMENT"
    assert payload["commit_id"] == "head456"

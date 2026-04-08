"""Tests for PR reviewer permission gating."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts.config import ReviewerConfig
from scripts.models import GithubEvent
from scripts.permission_gate import PermissionGate


def test_may_process_blocks_non_collaborator_when_required() -> None:
    repo = MagicMock()
    repo.get_collaborator_permission.return_value = "none"
    gh = MagicMock()
    gh.get_repo.return_value = repo

    allowed, reason = PermissionGate(gh).may_process(
        GithubEvent(
            event_name="pull_request_target",
            repo="owner/repo",
            actor="alice",
            pr_number=1,
            comment_id=None,
            body="",
        ),
        ReviewerConfig(collaborator_only=True),
    )

    assert allowed is False
    assert reason == "non-collaborator"


def test_may_process_allows_supported_review_event() -> None:
    allowed, reason = PermissionGate(MagicMock()).may_process(
        GithubEvent(
            event_name="pull_request_target",
            repo="owner/repo",
            actor="alice",
            pr_number=1,
            comment_id=None,
            body="",
        ),
        ReviewerConfig(),
    )

    assert allowed is True
    assert reason is None


def test_may_process_blocks_chat_from_non_collaborator_by_default() -> None:
    repo = MagicMock()
    repo.get_collaborator_permission.return_value = "read"
    gh = MagicMock()
    gh.get_repo.return_value = repo

    allowed, reason = PermissionGate(gh).may_process(
        GithubEvent(
            event_name="issue_comment",
            repo="owner/repo",
            actor="alice",
            pr_number=1,
            comment_id=99,
            body="/reviewbot explain this",
        ),
        ReviewerConfig(),
    )

    assert allowed is False
    assert reason == "non-collaborator"


def test_may_process_allows_chat_when_chat_collaborator_gate_disabled() -> None:
    allowed, reason = PermissionGate(MagicMock()).may_process(
        GithubEvent(
            event_name="issue_comment",
            repo="owner/repo",
            actor="alice",
            pr_number=1,
            comment_id=99,
            body="/reviewbot explain this",
        ),
        ReviewerConfig(chat_collaborator_only=False),
    )

    assert allowed is True
    assert reason is None


def test_actor_is_collaborator_rejects_read_only_access() -> None:
    repo = MagicMock()
    repo.get_collaborator_permission.return_value = "read"
    gh = MagicMock()
    gh.get_repo.return_value = repo

    assert PermissionGate(gh).actor_is_collaborator("owner/repo", "alice") is False

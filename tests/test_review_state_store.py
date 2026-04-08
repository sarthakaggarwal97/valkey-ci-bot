"""Tests for PR reviewer state persistence."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from github.GithubException import GithubException

from scripts.models import ReviewState
from scripts.review_state_store import ReviewStateStore


def test_review_state_round_trip() -> None:
    store = ReviewStateStore()
    state = ReviewState(
        repo="owner/repo",
        pr_number=12,
        last_reviewed_head_sha="abc123",
        summary_comment_id=9,
        review_comment_ids=[1, 2, 3],
        updated_at="2026-03-12T00:00:00+00:00",
    )

    store.from_dict({"owner/repo#12": {
        "repo": state.repo,
        "pr_number": state.pr_number,
        "last_reviewed_head_sha": state.last_reviewed_head_sha,
        "summary_comment_id": state.summary_comment_id,
        "review_comment_ids": state.review_comment_ids,
        "updated_at": state.updated_at,
    }})

    restored = store.load("owner/repo", 12)

    assert restored == state


def test_save_creates_bot_data_branch_when_missing() -> None:
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_git_ref.side_effect = [
        GithubException(404, {"message": "missing bot-data"}),
        MagicMock(object=MagicMock(sha="base-sha")),
    ]
    repo.get_contents.side_effect = [
        GithubException(404, {"message": "missing review-state"}),
        GithubException(404, {"message": "missing review-state"}),
    ]
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = ReviewStateStore(gh, "owner/repo")

    store.save(
        ReviewState(
            repo="owner/repo",
            pr_number=5,
            last_reviewed_head_sha="abc123",
            summary_comment_id=1,
            review_comment_ids=[],
            updated_at="2026-03-12T00:00:00+00:00",
        )
    )

    repo.create_git_ref.assert_called_once_with(
        ref="refs/heads/bot-data",
        sha="base-sha",
    )


def test_save_does_not_fallback_to_create_on_non_404_lookup_error() -> None:
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_git_ref.return_value = MagicMock()
    repo.get_contents.side_effect = GithubException(500, {"message": "boom"})
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = ReviewStateStore(gh, "owner/repo")

    with pytest.raises(RuntimeError, match="failed to load review state store"):
        store.save(
            ReviewState(
                repo="owner/repo",
                pr_number=5,
                last_reviewed_head_sha="abc123",
                summary_comment_id=1,
                review_comment_ids=[],
                updated_at="2026-03-12T00:00:00+00:00",
            )
        )

    repo.create_file.assert_not_called()


def test_load_raises_on_non_missing_remote_error() -> None:
    repo = MagicMock()
    repo.get_contents.side_effect = GithubException(500, {"message": "boom"})
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = ReviewStateStore(gh, "owner/repo")

    with pytest.raises(RuntimeError, match="failed to load review state store"):
        store.load("owner/repo", 5)


def test_save_retries_on_write_conflict_and_merges_remote_updates() -> None:
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_git_ref.return_value = MagicMock()

    initial_payload = {
        "owner/repo#1": {
            "repo": "owner/repo",
            "pr_number": 1,
            "last_reviewed_head_sha": "base",
            "summary_comment_id": 11,
            "review_comment_ids": [101],
            "updated_at": "2026-03-12T00:00:00+00:00",
        },
    }
    concurrent_payload = {
        **initial_payload,
        "owner/repo#9": {
            "repo": "owner/repo",
            "pr_number": 9,
            "last_reviewed_head_sha": "head",
            "summary_comment_id": 19,
            "review_comment_ids": [901],
            "updated_at": "2026-03-12T00:01:00+00:00",
        },
    }
    initial_contents = MagicMock(
        decoded_content=json.dumps(initial_payload).encode(),
        sha="sha-1",
    )
    concurrent_contents = MagicMock(
        decoded_content=json.dumps(concurrent_payload).encode(),
        sha="sha-2",
    )
    repo.get_contents.side_effect = [
        initial_contents,
        initial_contents,
        concurrent_contents,
    ]
    repo.update_file.side_effect = [
        GithubException(409, {"message": "sha conflict"}),
        None,
    ]

    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = ReviewStateStore(gh, "owner/repo")

    store.save(
        ReviewState(
            repo="owner/repo",
            pr_number=5,
            last_reviewed_head_sha="abc123",
            summary_comment_id=5,
            review_comment_ids=[501],
            updated_at="2026-03-12T00:02:00+00:00",
        )
    )

    assert repo.update_file.call_count == 2
    merged_payload = json.loads(repo.update_file.call_args_list[-1].args[2])
    assert set(merged_payload) == {
        "owner/repo#1",
        "owner/repo#5",
        "owner/repo#9",
    }
    assert repo.update_file.call_args_list[-1].args[3] == "sha-2"

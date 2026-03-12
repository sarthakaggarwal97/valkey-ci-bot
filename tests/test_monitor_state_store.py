"""Tests for centralized monitor state persistence."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from github.GithubException import GithubException

from scripts.monitor_state_store import MonitorStateStore


def test_monitor_state_store_round_trip() -> None:
    store = MonitorStateStore()
    store.mark_seen(
        "valkey-io/valkey:daily.yml:schedule",
        last_seen_run_id=12345,
        target_repo="valkey-io/valkey",
        workflow_file="daily.yml",
        event="schedule",
    )

    payload = store.to_dict()
    restored = MonitorStateStore()
    restored.from_dict(payload)

    assert restored.get_last_seen_run_id("valkey-io/valkey:daily.yml:schedule") == 12345


def test_monitor_state_store_defaults_to_zero() -> None:
    store = MonitorStateStore()
    assert store.get_last_seen_run_id("missing") == 0


def test_monitor_state_store_load_raises_on_unexpected_error() -> None:
    repo = MagicMock()
    repo.get_contents.side_effect = GithubException(500, {"message": "boom"})
    github_client = MagicMock()
    github_client.get_repo.return_value = repo

    store = MonitorStateStore(github_client, "owner/repo")

    with pytest.raises(RuntimeError, match="failed to load monitor state"):
        store.load()


def test_monitor_state_store_save_raises_on_unexpected_error() -> None:
    repo = MagicMock()
    repo.get_git_ref.side_effect = GithubException(500, {"message": "boom"})
    github_client = MagicMock()
    github_client.get_repo.return_value = repo

    store = MonitorStateStore(github_client, "owner/repo")
    store.mark_seen(
        "valkey-io/valkey:daily.yml:schedule",
        last_seen_run_id=12345,
        target_repo="valkey-io/valkey",
        workflow_file="daily.yml",
        event="schedule",
    )

    with pytest.raises(RuntimeError, match="failed to save monitor state"):
        store.save()

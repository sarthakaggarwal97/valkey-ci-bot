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


def test_monitor_state_store_supports_multi_event_keys() -> None:
    store = MonitorStateStore()
    store.mark_seen(
        "valkey-io/valkey:ci.yml:pull_request,push",
        last_seen_run_id=23456,
        target_repo="valkey-io/valkey",
        workflow_file="ci.yml",
        event="pull_request,push",
    )

    payload = store.to_dict()
    restored = MonitorStateStore()
    restored.from_dict(payload)

    assert restored.get_last_seen_run_id("valkey-io/valkey:ci.yml:pull_request,push") == 23456


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


def test_monitor_state_store_save_retries_and_merges_on_conflict() -> None:
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_git_ref.return_value = MagicMock()

    first_remote = MagicMock()
    first_remote.sha = "sha-1"
    first_remote.decoded_content = (
        b'{"existing":{"last_seen_run_id":11,"target_repo":"valkey-io/valkey",'
        b'"workflow_file":"ci.yml","event":"pull_request,push","updated_at":"t1"}}'
    )
    second_remote = MagicMock()
    second_remote.sha = "sha-2"
    second_remote.decoded_content = (
        b'{"existing":{"last_seen_run_id":12,"target_repo":"valkey-io/valkey",'
        b'"workflow_file":"ci.yml","event":"pull_request,push","updated_at":"t2"},'
        b'"weekly":{"last_seen_run_id":7,"target_repo":"valkey-io/valkey",'
        b'"workflow_file":"weekly.yml","event":"schedule","updated_at":"t3"}}'
    )
    repo.get_contents.side_effect = [first_remote, second_remote]
    repo.update_file.side_effect = [
        GithubException(409, {"message": "sha mismatch"}),
        None,
    ]

    github_client = MagicMock()
    github_client.get_repo.return_value = repo

    store = MonitorStateStore(github_client, "owner/repo")
    store.mark_seen(
        "daily",
        last_seen_run_id=12345,
        target_repo="valkey-io/valkey",
        workflow_file="daily.yml",
        event="schedule",
    )

    store.save()

    assert repo.update_file.call_count == 2
    final_call = repo.update_file.call_args_list[-1]
    assert final_call.args[3] == "sha-2"
    payload = final_call.args[2]
    assert '"existing"' in payload
    assert '"weekly"' in payload
    assert '"daily"' in payload
    assert store.get_last_seen_run_id("existing") == 12
    assert store.get_last_seen_run_id("weekly") == 7
    assert store.get_last_seen_run_id("daily") == 12345

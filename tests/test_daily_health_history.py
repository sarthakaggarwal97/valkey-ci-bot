"""Tests for durable daily health history snapshots."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import scripts.daily_health_history as daily_health_history


class _FakeContents:
    def __init__(self, text: str, sha: str = "sha-1") -> None:
        self.decoded_content = text.encode("utf-8")
        self.sha = sha


class _FakeGitRef:
    def __init__(self, sha: str = "base-sha") -> None:
        self.object = type("ObjectRef", (), {"sha": sha})()


class _FakeRemoteRepo:
    def __init__(self) -> None:
        self.default_branch = "main"
        self.contents: dict[str, _FakeContents] = {}
        self.created_files: list[tuple[str, str, str, str]] = []
        self.updated_files: list[tuple[str, str, str, str, str]] = []
        self.created_refs: list[tuple[str, str]] = []
        self.history_branch_exists = True

    def get_git_ref(self, ref: str) -> _FakeGitRef:
        if ref == "heads/bot-data" and not self.history_branch_exists:
            raise FileNotFoundError("missing branch")
        return _FakeGitRef()

    def create_git_ref(self, ref: str, sha: str) -> None:
        self.created_refs.append((ref, sha))
        self.history_branch_exists = True

    def get_contents(self, path: str, ref: str) -> _FakeContents:
        if path not in self.contents:
            raise FileNotFoundError(path)
        return self.contents[path]

    def create_file(self, path: str, message: str, content: str, branch: str) -> None:
        self.created_files.append((path, message, content, branch))
        self.contents[path] = _FakeContents(content, sha=f"created-{len(self.created_files)}")

    def update_file(
        self,
        path: str,
        message: str,
        content: str,
        sha: str,
        branch: str,
    ) -> None:
        self.updated_files.append((path, message, content, sha, branch))
        self.contents[path] = _FakeContents(content, sha=f"updated-{len(self.updated_files)}")


class _FakeRemoteClient:
    def __init__(self, repo: _FakeRemoteRepo) -> None:
        self.repo = repo

    def get_repo(self, name: str) -> _FakeRemoteRepo:
        assert name == "owner/repo"
        return self.repo


def _run(
    *,
    workflow: str = "daily.yml",
    date: str = "2026-04-15",
    run_id: int = 77,
    status: str = "success",
) -> dict[str, object]:
    return {
        "workflow": workflow,
        "date": date,
        "run_id": run_id,
        "status": status,
        "commit_sha": "abcdef1",
        "full_sha": "abcdef1234567890",
        "run_url": f"https://example.com/runs/{run_id}",
        "total_jobs": 4,
        "failed_jobs": 0 if status == "success" else 1,
        "failed_job_names": [],
        "unique_failures": 0 if status == "success" else 1,
        "failure_names": [] if status == "success" else ["timeout"],
        "failure_jobs": {},
    }


def test_snapshot_path_and_history_root_require_values() -> None:
    assert daily_health_history.history_root() == "dashboard-history/daily-health"
    assert (
        daily_health_history.snapshot_relative_path("daily.yml", "2026-04-15")
        == "dashboard-history/daily-health/daily.yml/2026-04-15.json"
    )
    with pytest.raises(ValueError):
        daily_health_history.snapshot_relative_path("", "2026-04-15")
    with pytest.raises(ValueError):
        daily_health_history.snapshot_relative_path("daily.yml", "")


def test_merge_runs_prefers_latest_data_by_workflow_date_and_run_id() -> None:
    fallback = [
        _run(workflow="daily.yml", date="2026-04-15", run_id=1, status="success"),
        {"run_id": 9, "status": "success"},
    ]
    preferred = [
        _run(workflow="daily.yml", date="2026-04-15", run_id=2, status="failure"),
        {"run_id": 9, "status": "failure"},
    ]

    merged = daily_health_history.merge_runs(preferred, fallback)

    by_key = {
        (str(item.get("workflow", "")), str(item.get("date", "")), str(item.get("run_id", ""))): item
        for item in merged
    }
    assert by_key[("daily.yml", "2026-04-15", "2")]["status"] == "failure"
    assert by_key[("", "", "9")]["status"] == "failure"
    assert len(merged) == 2


def test_load_history_runs_normalizes_payloads_and_skips_invalid_files(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    history_dir = tmp_path / "history"
    (history_dir / "daily.yml").mkdir(parents=True)
    (history_dir / "weekly.yml").mkdir(parents=True)

    wrapped_payload = {
        "schema_version": 1,
        "repo": "valkey-io/valkey",
        "workflow": "daily.yml",
        "date": "2026-04-15",
        "captured_at": "2026-04-15T10:00:00+00:00",
        "run": {
            "run_id": 101,
            "status": "failure",
            "failure_names": ["timeout"],
            "failure_jobs": {},
        },
    }
    raw_payload = {
        "workflow": "weekly.yml",
        "date": "2026-04-14",
        "run_id": 202,
        "status": "success",
        "failure_names": [],
        "failure_jobs": {},
    }
    invalid_payload = "{not-json"

    (history_dir / "daily.yml" / "2026-04-15.json").write_text(
        json.dumps(wrapped_payload),
        encoding="utf-8",
    )
    (history_dir / "weekly.yml" / "2026-04-14.json").write_text(
        json.dumps(raw_payload),
        encoding="utf-8",
    )
    (history_dir / "weekly.yml" / "2026-04-13.json").write_text(
        invalid_payload,
        encoding="utf-8",
    )

    caplog.set_level(logging.WARNING)
    loaded = daily_health_history.load_history_runs(
        history_dir,
        workflows=["daily.yml", "weekly.yml"],
        expected_dates=["2026-04-13", "2026-04-14", "2026-04-15"],
    )

    assert {(item["workflow"], item["date"]) for item in loaded} == {
        ("daily.yml", "2026-04-15"),
        ("weekly.yml", "2026-04-14"),
    }
    daily_run = next(item for item in loaded if item["workflow"] == "daily.yml")
    assert daily_run["repo"] == "valkey-io/valkey"
    assert daily_run["captured_at"] == "2026-04-15T10:00:00+00:00"
    assert "Skipping unreadable history snapshot" in caplog.text


def test_run_from_snapshot_payload_fills_metadata_and_rejects_invalid_payloads() -> None:
    payload = {
        "repo": "valkey-io/valkey",
        "workflow": "daily.yml",
        "date": "2026-04-15",
        "captured_at": "2026-04-15T10:00:00+00:00",
        "run": {"run_id": 99, "status": "success"},
    }

    run = daily_health_history._run_from_snapshot_payload(payload)

    assert run == {
        "run_id": 99,
        "status": "success",
        "workflow": "daily.yml",
        "date": "2026-04-15",
        "repo": "valkey-io/valkey",
        "captured_at": "2026-04-15T10:00:00+00:00",
    }
    assert daily_health_history._run_from_snapshot_payload(None) is None
    assert daily_health_history._run_from_snapshot_payload({"run": []}) is None
    assert daily_health_history._run_from_snapshot_payload({"run": {"status": "success"}}) is None


def test_save_runs_mirrors_locally_and_warns_on_partial_remote_config(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = daily_health_history.DailyHealthHistoryStore(
        repo_full_name="owner/repo",
        mirror_dir=tmp_path,
    )

    caplog.set_level(logging.WARNING)
    summary = store.save_runs(
        [
            _run(),
            {"date": "2026-04-16", "run_id": 88},
        ],
        repo_full_name="valkey-io/valkey",
    )

    assert summary == {"saved": 0, "skipped": 0, "mirrored": 1}
    mirrored = tmp_path / "daily.yml" / "2026-04-15.json"
    assert mirrored.exists()
    payload = json.loads(mirrored.read_text(encoding="utf-8"))
    assert payload["repo"] == "valkey-io/valkey"
    assert "missing a GitHub client or repo name" in caplog.text


def test_save_runs_creates_history_branch_and_remote_snapshot(tmp_path: Path) -> None:
    repo = _FakeRemoteRepo()
    repo.history_branch_exists = False
    client = _FakeRemoteClient(repo)
    store = daily_health_history.DailyHealthHistoryStore(
        client,
        "owner/repo",
        mirror_dir=tmp_path,
    )

    summary = store.save_runs([_run(status="failure")], repo_full_name="valkey-io/valkey")

    assert summary == {"saved": 1, "skipped": 0, "mirrored": 1}
    assert repo.created_refs == [("refs/heads/bot-data", "base-sha")]
    assert len(repo.created_files) == 1


def test_save_remote_snapshot_creates_skips_and_updates() -> None:
    repo = _FakeRemoteRepo()
    store = daily_health_history.DailyHealthHistoryStore()
    path = daily_health_history.snapshot_relative_path("daily.yml", "2026-04-15")

    created = store._save_remote_snapshot(repo, "daily.yml", "2026-04-15", "one\n")
    skipped = store._save_remote_snapshot(repo, "daily.yml", "2026-04-15", "one\n")
    updated = store._save_remote_snapshot(repo, "daily.yml", "2026-04-15", "two\n")

    assert created == "saved"
    assert skipped == "skipped"
    assert updated == "saved"
    assert path in repo.contents
    assert len(repo.created_files) == 1
    assert len(repo.updated_files) == 1


def test_save_remote_snapshot_retries_on_conflict() -> None:
    class _Conflict(Exception):
        def __init__(self) -> None:
            super().__init__("conflict")
            self.status = 409

    class _ConflictRepo(_FakeRemoteRepo):
        def __init__(self) -> None:
            super().__init__()
            self.contents[daily_health_history.snapshot_relative_path("daily.yml", "2026-04-15")] = _FakeContents(
                "old\n",
                sha="old-sha",
            )
            self.attempts = 0

        def update_file(
            self,
            path: str,
            message: str,
            content: str,
            sha: str,
            branch: str,
        ) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise _Conflict()
            super().update_file(path, message, content, sha, branch)

    repo = _ConflictRepo()
    store = daily_health_history.DailyHealthHistoryStore()

    outcome = store._save_remote_snapshot(repo, "daily.yml", "2026-04-15", "new\n")

    assert outcome == "saved"
    assert repo.attempts == 2


def test_save_remote_snapshot_rejects_directory_results() -> None:
    class _DirectoryRepo(_FakeRemoteRepo):
        def get_contents(self, path: str, ref: str) -> list[str]:
            return ["not-a-file"]

    repo = _DirectoryRepo()
    store = daily_health_history.DailyHealthHistoryStore()

    with pytest.raises(ValueError, match="directory"):
        store._save_remote_snapshot(repo, "daily.yml", "2026-04-15", "content\n")

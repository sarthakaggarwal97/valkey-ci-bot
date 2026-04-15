"""Tests for the daily health history backfill CLI."""

from __future__ import annotations

import json

import scripts.backfill_daily_health_history as backfill_daily_health_history


def test_main_requires_tokens(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    exit_code = backfill_daily_health_history.main(
        ["--state-repo", "owner/repo"]
    )

    assert exit_code == 1


def test_main_backfills_multiple_workflows_and_prints_summary(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    github_auths: list[object] = []
    fetch_calls: list[tuple[object, str, str, str, int]] = []
    store_calls: list[tuple[list[dict[str, object]], str]] = []

    class _FakeGithub:
        def __init__(self, auth: object) -> None:
            self.auth = auth
            github_auths.append(auth)

    class _FakeAuth:
        @staticmethod
        def Token(token: str) -> str:
            return f"auth:{token}"

    class _FakeStore:
        def __init__(self, github_client, repo_full_name: str, *, mirror_dir=None) -> None:
            self.github_client = github_client
            self.repo_full_name = repo_full_name
            self.mirror_dir = mirror_dir

        def save_runs(
            self,
            runs: list[dict[str, object]],
            *,
            repo_full_name: str = "",
        ) -> dict[str, int]:
            store_calls.append((runs, repo_full_name))
            return {
                "saved": len(runs),
                "skipped": 1,
                "mirrored": len(runs),
            }

    def _fake_fetch(gh, repo: str, workflow: str, branch: str, days: int) -> list[dict[str, object]]:
        fetch_calls.append((gh.auth, repo, workflow, branch, days))
        return [
            {
                "workflow": workflow,
                "date": "2026-04-15",
                "run_id": 100 if workflow == "daily.yml" else 200,
            }
        ]

    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    monkeypatch.setattr(backfill_daily_health_history, "Github", _FakeGithub)
    monkeypatch.setattr(backfill_daily_health_history, "Auth", _FakeAuth)
    monkeypatch.setattr(backfill_daily_health_history, "DailyHealthHistoryStore", _FakeStore)
    monkeypatch.setattr(backfill_daily_health_history, "fetch_daily_runs", _fake_fetch)

    exit_code = backfill_daily_health_history.main(
        [
            "--repo",
            "valkey-io/valkey",
            "--workflow",
            "daily.yml",
            "weekly.yml",
            "--branch",
            "unstable",
            "--days",
            "7",
            "--state-repo",
            "owner/repo",
            "--mirror-dir",
            str(tmp_path / "history"),
        ]
    )

    assert exit_code == 0
    assert github_auths == ["auth:env-token", "auth:env-token"]
    assert fetch_calls == [
        ("auth:env-token", "valkey-io/valkey", "daily.yml", "unstable", 7),
        ("auth:env-token", "valkey-io/valkey", "weekly.yml", "unstable", 7),
    ]
    assert [call[1] for call in store_calls] == ["valkey-io/valkey", "valkey-io/valkey"]

    summary = json.loads(capsys.readouterr().out)
    assert summary["repo"] == "valkey-io/valkey"
    assert summary["state_repo"] == "owner/repo"
    assert summary["saved"] == 2
    assert summary["skipped"] == 2
    assert summary["mirrored"] == 2
    assert summary["workflows"] == [
        {"workflow": "daily.yml", "runs_found": 1, "saved": 1, "skipped": 1, "mirrored": 1},
        {"workflow": "weekly.yml", "runs_found": 1, "saved": 1, "skipped": 1, "mirrored": 1},
    ]

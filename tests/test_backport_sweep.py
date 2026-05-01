from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from scripts import backport_sweep
from scripts.backport_sweep import ProjectBackportCandidate


def test_git_auth_env_keeps_askpass_outside_clone_destination(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    env = backport_sweep._git_auth_env(str(repo_dir), "token")
    askpass = Path(env["GIT_ASKPASS"])
    try:
        assert askpass.exists()
        assert askpass.parent != repo_dir
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_PASSWORD"] == "token"
    finally:
        askpass.unlink(missing_ok=True)


def test_apply_candidate_aborts_empty_cherry_pick(monkeypatch, tmp_path):
    candidate = ProjectBackportCandidate(
        source_pr_number=10,
        source_pr_title="Already applied",
        source_pr_url="https://github.com/valkey-io/valkey/pull/10",
        target_branch="8.1",
        merge_commit_sha="abc123",
    )
    git_calls: list[tuple[str, ...]] = []
    subprocess_calls: list[list[str]] = []

    def fake_run_git(_repo_dir, *args, **_kwargs):
        git_calls.append(args)

    def fake_subprocess_run(cmd, **_kwargs):
        subprocess_calls.append(cmd)
        if cmd[:2] == ["git", "cherry-pick"]:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="The previous cherry-pick is now empty",
            )
        if cmd[:4] == ["git", "diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "cherry-pick", "--abort"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(backport_sweep, "_run_git", fake_run_git)
    monkeypatch.setattr(backport_sweep.subprocess, "run", fake_subprocess_run)

    result = backport_sweep._apply_candidate(
        str(tmp_path),
        candidate,
        MagicMock(),
        MagicMock(),
        "valkey-io/valkey",
        {},
    )

    assert result.outcome == "skipped-existing"
    assert result.detail == "already applied or empty cherry-pick"
    assert ("fetch", "origin", "abc123") in git_calls
    assert ["git", "cherry-pick", "--abort"] in subprocess_calls


def test_run_test_commands_returns_failure_output(tmp_path):
    ok, output = backport_sweep._run_test_commands(
        str(tmp_path),
        ["printf stdout; printf stderr >&2; exit 3"],
    )

    assert ok is False
    assert "stdout" in output
    assert "stderr" in output

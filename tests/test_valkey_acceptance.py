"""Tests for the Valkey acceptance harness helpers."""

from __future__ import annotations

from pathlib import Path

from scripts.commit_signoff import CommitSigner
from scripts.valkey_acceptance import (
    CICase,
    BackportCase,
    _has_signed_off_by,
    _load_manifest,
    _needs_core_team,
    _needs_docs,
    _render_backport_command,
    _render_ci_command,
    _security_sensitive,
)


def test_has_signed_off_by_detects_dco_trailer() -> None:
    assert _has_signed_off_by("fix: test\n\nSigned-off-by: Val Key <valkey@example.com>")
    assert not _has_signed_off_by("fix: test")


def test_needs_core_team_matches_valkey_critical_paths() -> None:
    assert _needs_core_team(["src/cluster_slot_stats.c"]) is True
    assert _needs_core_team(["src/replication.c"]) is True
    assert _needs_core_team(["GOVERNANCE.md"]) is True
    assert _needs_core_team(["src/server.c"]) is False


def test_needs_docs_matches_command_and_config_paths() -> None:
    assert _needs_docs(["src/commands/get.json"]) is True
    assert _needs_docs(["valkey.conf"]) is True
    assert _needs_docs(["src/server.c"]) is False


def test_security_sensitive_detects_keywords() -> None:
    assert _security_sensitive("Fix security issue", "", ["src/server.c"]) is True
    assert _security_sensitive("Bug fix", "Addresses CVE-2026-1234", []) is True
    assert _security_sensitive("Bug fix", "", ["src/server.c"]) is False


def test_load_manifest_parses_review_ci_and_backport_cases(tmp_path: Path) -> None:
    manifest_path = tmp_path / "acceptance.yml"
    manifest_path.write_text(
        """
target_repo: valkey-io/valkey
execution_repo: your-user/valkey
review_cases:
  - name: dco-check
    pr_number: 123
    expectations:
      missing_dco: false
ci_cases:
  - name: daily-replay
    workflow_run_id: 456
backport_cases:
  - name: release-8-1
    source_pr_number: 789
    target_branch: "8.1"
""",
        encoding="utf-8",
    )

    manifest = _load_manifest(manifest_path)

    assert manifest.target_repo == "valkey-io/valkey"
    assert manifest.execution_repo == "your-user/valkey"
    assert manifest.review_cases[0].pr_number == 123
    assert manifest.ci_cases[0].workflow_run_id == 456
    assert manifest.backport_cases[0].target_branch == "8.1"


def test_render_ci_command_includes_queue_only_and_identity() -> None:
    command = _render_ci_command(
        CICase(name="daily", workflow_run_id=123),
        "your-user/valkey",
        CommitSigner(name="Val Key", email="valkey@example.com"),
    )

    assert "--queue-only" in command
    assert "CI_BOT_COMMIT_NAME='Val Key'" in command
    assert "--run-id 123" in command


def test_render_backport_command_includes_branch_and_identity() -> None:
    command = _render_backport_command(
        BackportCase(name="bp", source_pr_number=123, target_branch="8.1"),
        "your-user/valkey",
        CommitSigner(name="Val Key", email="valkey@example.com"),
    )

    assert "--pr-number 123" in command
    assert "--target-branch 8.1" in command
    assert "CI_BOT_COMMIT_EMAIL='valkey@example.com'" in command

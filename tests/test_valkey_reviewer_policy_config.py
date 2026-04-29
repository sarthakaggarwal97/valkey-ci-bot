"""Regression checks for the checked-in Valkey reviewer guidance."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_valkey_reviewer_config_mentions_current_upstream_policy() -> None:
    config_text = (
        Path(__file__).resolve().parents[1] / ".github" / "pr-review-bot.yml"
    ).read_text(encoding="utf-8")

    lowered = config_text.lower()
    assert "signed-off-by" in lowered
    assert "security@lists.valkey.io" in lowered
    assert "@core-team" in config_text
    assert "needs-doc-pr" in lowered
    assert "governance.md" in lowered


def test_valkey_reviewer_config_keeps_human_control_defaults() -> None:
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github" / "pr-review-bot.yml").read_text(
            encoding="utf-8"
        )
    )["reviewer"]

    assert config["approve_on_no_findings"] is False
    assert config["model_file_triage"] is False
    assert config["post_policy_notes"] is True
    assert config["chat_collaborator_only"] is True
    assert config["max_review_comments"] <= 25

"""Regression checks for the checked-in Valkey reviewer guidance."""

from __future__ import annotations

from pathlib import Path


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

"""Regression checks for Valkey org adoption defaults."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_yaml(path: str) -> dict:
    return yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))


def _assert_bounded_fix_config(config: dict) -> None:
    limits = config["limits"]
    fix_generation = config["fix_generation"]

    assert 1 <= limits["max_failures_per_run"] <= 5
    assert 1 <= limits["max_prs_per_day"] <= 3
    assert 1 <= limits["max_open_bot_prs"] <= 3
    assert 1 <= limits["queued_pr_max_attempts"] <= 5
    assert 1 <= limits["max_patch_files"] <= 10
    assert limits["daily_token_budget"] > 0
    assert fix_generation["confidence_threshold"] in {"medium", "high"}
    assert fix_generation["max_retries"] <= 2
    assert fix_generation["max_validation_retries"] <= 1


def test_valkey_daily_bot_uses_bounded_production_limits() -> None:
    _assert_bounded_fix_config(_load_yaml(".github/valkey-daily-bot.yml"))


def test_ci_failure_bot_fallback_uses_bounded_production_limits() -> None:
    _assert_bounded_fix_config(_load_yaml(".github/ci-failure-bot.yml"))

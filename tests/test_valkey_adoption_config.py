"""Regression checks for Valkey org adoption defaults."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_yaml(path: str) -> dict:
    return yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))


def _assert_ai_first_fix_config(config: dict) -> None:
    limits = config["limits"]
    fix_generation = config["fix_generation"]
    bedrock = config["bedrock"]
    retrieval = config["retrieval"]

    assert bedrock["model_id"] == "us.anthropic.claude-opus-4-7"
    assert bedrock["max_input_tokens"] >= 900000
    assert bedrock["max_output_tokens"] >= 65536
    assert bedrock["thinking_budget"] == 128000
    assert retrieval["max_results_per_knowledge_base"] >= 8
    assert retrieval["max_total_chars"] >= 30000
    assert limits["max_failures_per_run"] == 0
    assert limits["max_prs_per_day"] == 0
    assert limits["max_open_bot_prs"] == 0
    assert limits["queued_pr_max_attempts"] == 0
    assert limits["max_patch_files"] >= 30
    assert limits["daily_token_budget"] == 0
    assert fix_generation["confidence_threshold"] in {"medium", "high"}
    assert fix_generation["max_retries"] >= 10
    assert fix_generation["max_validation_retries"] >= 5


def test_valkey_daily_bot_uses_bounded_production_limits() -> None:
    _assert_ai_first_fix_config(_load_yaml(".github/valkey-daily-bot.yml"))


def test_ci_failure_bot_fallback_uses_bounded_production_limits() -> None:
    _assert_ai_first_fix_config(_load_yaml(".github/ci-failure-bot.yml"))

# Feature: backport-agent, Property 12: Config loading round trip
"""Property tests for backport configuration loading.

Property 12: For any valid BackportConfig values serialized to a YAML-compatible
dict, load_backport_config should produce a BackportConfig with matching field
values. When given an empty or None input, it should return a BackportConfig with
all default values.

**Validates: Requirements 7.4, 7.5**
"""

from __future__ import annotations

from dataclasses import asdict

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.backport_config import load_backport_config
from scripts.backport_models import BackportConfig

# --- Strategies ---

safe_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Z"),
        min_codepoint=32,
        max_codepoint=126,
    ),
    min_size=1,
    max_size=50,
)

positive_int = st.integers(min_value=1, max_value=10_000_000)

backport_config_strategy = st.fixed_dictionaries({
    "bedrock_model_id": safe_text,
    "max_conflict_retries": positive_int,
    "max_conflicting_files": positive_int,
    "max_prs_per_day": positive_int,
    "per_backport_token_budget": positive_int,
    "backport_label": safe_text,
    "llm_conflict_label": safe_text,
})


# --- Property Tests ---


@settings(max_examples=100, deadline=None)
@given(config_data=backport_config_strategy)
def test_config_round_trip(config_data: dict) -> None:
    """Property 12 (part 1): Loading a valid config dict produces a
    BackportConfig whose fields match the input values."""
    cfg = load_backport_config(config_data)

    assert cfg.bedrock_model_id == config_data["bedrock_model_id"]
    assert cfg.max_conflict_retries == config_data["max_conflict_retries"]
    assert cfg.max_conflicting_files == config_data["max_conflicting_files"]
    assert cfg.max_prs_per_day == config_data["max_prs_per_day"]
    assert cfg.per_backport_token_budget == config_data["per_backport_token_budget"]
    assert cfg.backport_label == config_data["backport_label"]
    assert cfg.llm_conflict_label == config_data["llm_conflict_label"]


@settings(max_examples=100, deadline=None)
@given(config_data=backport_config_strategy)
def test_config_round_trip_via_asdict(config_data: dict) -> None:
    """Property 12 (part 2): Serializing a BackportConfig via asdict and
    reloading it produces an identical config."""
    original = load_backport_config(config_data)
    serialized = asdict(original)
    reloaded = load_backport_config(serialized)

    assert reloaded == original


def test_none_input_returns_defaults() -> None:
    """Property 12 (part 3): None input returns BackportConfig with all
    default values."""
    cfg = load_backport_config(None)
    defaults = BackportConfig()

    assert cfg == defaults


def test_empty_dict_returns_defaults() -> None:
    """Property 12 (part 4): Empty dict input returns BackportConfig with all
    default values."""
    cfg = load_backport_config({})
    defaults = BackportConfig()

    assert cfg == defaults

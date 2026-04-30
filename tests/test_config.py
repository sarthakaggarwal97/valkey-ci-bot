# Feature: valkey-ci-agent, Property 17: Configuration round-trip
"""Property tests for configuration loading.

Property 17: For any valid YAML configuration containing all supported fields,
loading the config should produce a BotConfig with all fields matching the YAML
values. For any missing config file, all fields should have their default values.

Validates: Requirements 8.2, 8.3
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from scripts.config import BotConfig, ProjectContext, ValidationProfile, load_config

# --- Strategies ---

# Constrain strings to printable ASCII to avoid YAML encoding edge cases
safe_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z"), min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=30,
)

safe_word = st.from_regex(r"[a-zA-Z][a-zA-Z0-9_-]{0,19}", fullmatch=True)

positive_int = st.integers(min_value=1, max_value=10_000_000)

confidence_levels = st.sampled_from(["high", "medium", "low"])

workflow_file = st.from_regex(r"[a-z][a-z0-9_-]{0,15}\.yml", fullmatch=True)

project_context_strategy = st.fixed_dictionaries({
    "language": safe_word,
    "build_system": safe_word,
    "test_frameworks": st.lists(safe_word, min_size=0, max_size=5),
    "description": safe_text,
    "source_dirs": st.lists(safe_word.map(lambda s: s + "/"), min_size=0, max_size=3),
    "test_dirs": st.lists(safe_word.map(lambda s: s + "/"), min_size=0, max_size=3),
    "test_to_source_patterns": st.lists(
        st.fixed_dictionaries({
            "test_path": safe_word.map(lambda s: f"tests/{s}.tcl"),
            "source_path": safe_word.map(lambda s: f"src/{s}.c"),
        }),
        min_size=0,
        max_size=3,
    ),
})

validation_profile_strategy = st.fixed_dictionaries({
    "job_name_pattern": safe_word.map(lambda s: f"^{s}$"),
    "matrix_params": st.dictionaries(safe_word, safe_word, min_size=0, max_size=3),
    "env": st.dictionaries(safe_word, safe_word, min_size=0, max_size=3),
    "install_commands": st.lists(safe_text, min_size=0, max_size=3),
    "build_commands": st.lists(safe_text, min_size=0, max_size=3),
    "test_commands": st.lists(safe_text, min_size=0, max_size=3),
})

full_config_strategy = st.fixed_dictionaries({
    "bedrock": st.fixed_dictionaries({
        "model_id": safe_word,
        "max_input_tokens": positive_int,
        "max_output_tokens": positive_int,
    }),
    "limits": st.fixed_dictionaries({
        "max_patch_files": positive_int,
        "max_prs_per_day": positive_int,
        "max_failures_per_run": positive_int,
        "max_open_bot_prs": positive_int,
        "queued_pr_max_attempts": positive_int,
        "daily_token_budget": positive_int,
    }),
    "fix_generation": st.fixed_dictionaries({
        "confidence_threshold": confidence_levels,
        "max_retries": st.integers(min_value=0, max_value=10),
        "max_validation_retries": st.integers(min_value=0, max_value=10),
    }),
    "flaky_campaign": st.fixed_dictionaries({
        "enabled": st.booleans(),
        "max_attempts_per_run": st.integers(min_value=1, max_value=10),
        "validation_passes": st.integers(min_value=1, max_value=10),
        "max_failed_hypotheses": st.integers(min_value=1, max_value=100),
    }),
    "validation": st.fixed_dictionaries({
        "require_profile": st.booleans(),
        "soak_workflows": st.lists(workflow_file, min_size=0, max_size=3),
        "soak_passes": st.integers(min_value=1, max_value=200),
    }),
    "monitored_workflows": st.lists(workflow_file, min_size=1, max_size=6),
    "retrieval": st.fixed_dictionaries({
        "enabled": st.booleans(),
        "code_knowledge_base_id": safe_word,
        "docs_knowledge_base_id": safe_word,
        "max_results_per_knowledge_base": positive_int,
        "max_chars_per_result": positive_int,
        "max_total_chars": positive_int,
    }),
    "project": project_context_strategy,
    "validation_profiles": st.lists(validation_profile_strategy, min_size=0, max_size=4),
})


# --- Property Tests ---

@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(config_data=full_config_strategy)
def test_config_round_trip_all_fields(config_data: dict) -> None:
    """Property 17 (part 1): Loading a valid YAML config with all supported
    fields produces a BotConfig whose fields match the YAML values."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        yaml.safe_dump(config_data, f)
        tmp_path = f.name

    try:
        cfg = load_config(tmp_path)

        # Bedrock section
        assert cfg.bedrock_model_id == config_data["bedrock"]["model_id"]
        assert cfg.max_input_tokens == config_data["bedrock"]["max_input_tokens"]
        assert cfg.max_output_tokens == config_data["bedrock"]["max_output_tokens"]

        # Limits section
        assert cfg.max_patch_files == config_data["limits"]["max_patch_files"]
        assert cfg.max_prs_per_day == config_data["limits"]["max_prs_per_day"]
        assert cfg.max_failures_per_run == config_data["limits"]["max_failures_per_run"]
        assert cfg.max_open_bot_prs == config_data["limits"]["max_open_bot_prs"]
        assert cfg.queued_pr_max_attempts == config_data["limits"]["queued_pr_max_attempts"]
        assert cfg.daily_token_budget == config_data["limits"]["daily_token_budget"]

        # Fix generation section
        assert cfg.confidence_threshold == config_data["fix_generation"]["confidence_threshold"]
        assert cfg.max_retries_fix == config_data["fix_generation"]["max_retries"]
        assert cfg.max_retries_validation == config_data["fix_generation"]["max_validation_retries"]

        # Flaky campaign section
        assert cfg.flaky_campaign_enabled == config_data["flaky_campaign"]["enabled"]
        assert cfg.flaky_max_attempts_per_run == config_data["flaky_campaign"]["max_attempts_per_run"]
        assert cfg.flaky_validation_passes == config_data["flaky_campaign"]["validation_passes"]
        assert cfg.flaky_max_failed_hypotheses == config_data["flaky_campaign"]["max_failed_hypotheses"]

        # Validation section
        assert cfg.require_validation_profile == config_data["validation"]["require_profile"]
        assert cfg.soak_validation_workflows == config_data["validation"]["soak_workflows"]
        assert cfg.soak_validation_passes == config_data["validation"]["soak_passes"]

        # Monitored workflows
        assert cfg.monitored_workflows == config_data["monitored_workflows"]

        # Retrieval section
        assert cfg.retrieval.enabled == config_data["retrieval"]["enabled"]
        assert cfg.retrieval.code_knowledge_base_id == config_data["retrieval"]["code_knowledge_base_id"]
        assert cfg.retrieval.docs_knowledge_base_id == config_data["retrieval"]["docs_knowledge_base_id"]
        assert cfg.retrieval.max_results_per_knowledge_base == config_data["retrieval"]["max_results_per_knowledge_base"]
        assert cfg.retrieval.max_chars_per_result == config_data["retrieval"]["max_chars_per_result"]
        assert cfg.retrieval.max_total_chars == config_data["retrieval"]["max_total_chars"]

        # Project context
        proj = config_data["project"]
        assert cfg.project.language == proj["language"]
        assert cfg.project.build_system == proj["build_system"]
        assert cfg.project.test_frameworks == proj["test_frameworks"]
        assert cfg.project.description == proj["description"]
        assert cfg.project.source_dirs == proj["source_dirs"]
        assert cfg.project.test_dirs == proj["test_dirs"]
        assert cfg.project.test_to_source_patterns == proj["test_to_source_patterns"]

        # Validation profiles
        assert len(cfg.validation_profiles) == len(config_data["validation_profiles"])
        for loaded, original in zip(cfg.validation_profiles, config_data["validation_profiles"]):
            assert loaded.job_name_pattern == original["job_name_pattern"]
            assert loaded.matrix_params == original["matrix_params"]
            assert loaded.env == original["env"]
            assert loaded.install_commands == original["install_commands"]
            assert loaded.build_commands == original["build_commands"]
            assert loaded.test_commands == original["test_commands"]
    finally:
        os.unlink(tmp_path)


def test_missing_config_returns_defaults() -> None:
    """Property 17 (part 2): A missing config file returns BotConfig with all
    default values."""
    cfg = load_config("/nonexistent/path/ci-failure-bot.yml")
    defaults = BotConfig()

    assert cfg.bedrock_model_id == defaults.bedrock_model_id
    assert cfg.max_input_tokens == defaults.max_input_tokens
    assert cfg.max_output_tokens == defaults.max_output_tokens
    assert cfg.max_patch_files == defaults.max_patch_files
    assert cfg.confidence_threshold == defaults.confidence_threshold
    assert cfg.monitored_workflows == defaults.monitored_workflows
    assert cfg.max_retries_fix == defaults.max_retries_fix
    assert cfg.max_retries_validation == defaults.max_retries_validation
    assert cfg.max_retries_bedrock == defaults.max_retries_bedrock
    assert cfg.max_prs_per_day == defaults.max_prs_per_day
    assert cfg.max_failures_per_run == defaults.max_failures_per_run
    assert cfg.max_open_bot_prs == defaults.max_open_bot_prs
    assert cfg.queued_pr_max_attempts == defaults.queued_pr_max_attempts
    assert cfg.daily_token_budget == defaults.daily_token_budget
    assert cfg.flaky_campaign_enabled == defaults.flaky_campaign_enabled
    assert cfg.flaky_max_attempts_per_run == defaults.flaky_max_attempts_per_run
    assert cfg.flaky_validation_passes == defaults.flaky_validation_passes
    assert cfg.flaky_max_failed_hypotheses == defaults.flaky_max_failed_hypotheses
    assert cfg.require_validation_profile == defaults.require_validation_profile
    assert cfg.soak_validation_workflows == defaults.soak_validation_workflows
    assert cfg.soak_validation_passes == defaults.soak_validation_passes
    assert cfg.retrieval == defaults.retrieval

    # Project defaults
    default_proj = ProjectContext()
    assert cfg.project.language == default_proj.language
    assert cfg.project.build_system == default_proj.build_system
    assert cfg.project.test_frameworks == default_proj.test_frameworks
    assert cfg.project.description == default_proj.description
    assert cfg.project.source_dirs == default_proj.source_dirs
    assert cfg.project.test_dirs == default_proj.test_dirs
    assert cfg.project.test_to_source_patterns == default_proj.test_to_source_patterns

    # No validation profiles by default
    assert cfg.validation_profiles == []


# Feature: valkey-ci-agent, Property 18: Invalid config falls back to defaults
# Validates: Requirements 8.4


# --- Strategies for Property 18 ---

# Generate strings that are invalid YAML (unbalanced braces, bad indentation, etc.)
invalid_yaml_content = st.sampled_from([
    ":\n  :\n  - :\n  bad",
    "{{{{not yaml}}}}",
    "bedrock:\n  model_id: [unterminated",
    "- - - -\n  : :\n  :\n",
    "key: *undefined_anchor",
    "tabs:\n\t\tbad:\n\t\t\tindent",
    "%YAML 1.1\n---\n!!python/object:os.system ['echo']",
    "unquoted: value: with: too: many: colons:\n  nested: [",
])

# Generate config dicts that have a mix of valid recognized fields and unrecognized fields
unrecognized_field_name = st.from_regex(r"zzz_unknown_[a-z]{1,10}", fullmatch=True)

unrecognized_fields_strategy = st.dictionaries(
    unrecognized_field_name,
    st.one_of(safe_text, st.integers(min_value=0, max_value=1000), st.booleans()),
    min_size=1,
    max_size=5,
)

# Strategy for a partial valid config (subset of recognized fields) mixed with unrecognized ones
partial_bedrock = st.fixed_dictionaries({}, optional={
    "model_id": safe_word,
    "max_input_tokens": positive_int,
    "max_output_tokens": positive_int,
})

partial_limits = st.fixed_dictionaries({}, optional={
    "max_patch_files": positive_int,
    "max_prs_per_day": positive_int,
    "max_failures_per_run": positive_int,
    "max_open_bot_prs": positive_int,
    "daily_token_budget": positive_int,
})

partial_fix_generation = st.fixed_dictionaries({}, optional={
    "confidence_threshold": confidence_levels,
    "max_retries": st.integers(min_value=0, max_value=10),
    "max_validation_retries": st.integers(min_value=0, max_value=10),
})

partial_flaky_campaign = st.fixed_dictionaries({}, optional={
    "enabled": st.booleans(),
    "max_attempts_per_run": st.integers(min_value=1, max_value=10),
    "validation_passes": st.integers(min_value=1, max_value=10),
    "max_failed_hypotheses": st.integers(min_value=1, max_value=100),
})


# --- Property 18 Tests ---


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(invalid_content=invalid_yaml_content)
def test_invalid_yaml_returns_full_defaults(invalid_content: str) -> None:
    """Property 18 (part 1): Invalid YAML content returns BotConfig with all
    default values.

    **Validates: Requirements 8.4**
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(invalid_content)
        tmp_path = f.name

    try:
        cfg = load_config(tmp_path)
        defaults = BotConfig()

        assert cfg.bedrock_model_id == defaults.bedrock_model_id
        assert cfg.max_input_tokens == defaults.max_input_tokens
        assert cfg.max_output_tokens == defaults.max_output_tokens
        assert cfg.max_patch_files == defaults.max_patch_files
        assert cfg.confidence_threshold == defaults.confidence_threshold
        assert cfg.monitored_workflows == defaults.monitored_workflows
        assert cfg.max_retries_fix == defaults.max_retries_fix
        assert cfg.max_retries_validation == defaults.max_retries_validation
        assert cfg.max_retries_bedrock == defaults.max_retries_bedrock
        assert cfg.max_prs_per_day == defaults.max_prs_per_day
        assert cfg.max_failures_per_run == defaults.max_failures_per_run
        assert cfg.max_open_bot_prs == defaults.max_open_bot_prs
        assert cfg.daily_token_budget == defaults.daily_token_budget
        assert cfg.flaky_campaign_enabled == defaults.flaky_campaign_enabled
        assert cfg.flaky_max_attempts_per_run == defaults.flaky_max_attempts_per_run
        assert cfg.flaky_validation_passes == defaults.flaky_validation_passes
        assert cfg.flaky_max_failed_hypotheses == defaults.flaky_max_failed_hypotheses
        assert cfg.soak_validation_workflows == defaults.soak_validation_workflows
        assert cfg.soak_validation_passes == defaults.soak_validation_passes
        assert cfg.retrieval == defaults.retrieval
        assert cfg.project.language == ProjectContext().language
        assert cfg.project.build_system == ProjectContext().build_system
        assert cfg.validation_profiles == []
    finally:
        os.unlink(tmp_path)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(extra_fields=unrecognized_fields_strategy)
def test_unrecognized_fields_ignored_with_defaults(extra_fields: dict) -> None:
    """Property 18 (part 2): Config with only unrecognized top-level fields
    returns defaults for all recognized fields.

    **Validates: Requirements 8.4**
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        yaml.safe_dump(extra_fields, f)
        tmp_path = f.name

    try:
        cfg = load_config(tmp_path)
        defaults = BotConfig()

        assert cfg.bedrock_model_id == defaults.bedrock_model_id
        assert cfg.max_input_tokens == defaults.max_input_tokens
        assert cfg.max_output_tokens == defaults.max_output_tokens
        assert cfg.max_patch_files == defaults.max_patch_files
        assert cfg.confidence_threshold == defaults.confidence_threshold
        assert cfg.monitored_workflows == defaults.monitored_workflows
        assert cfg.max_retries_fix == defaults.max_retries_fix
        assert cfg.max_retries_validation == defaults.max_retries_validation
        assert cfg.max_prs_per_day == defaults.max_prs_per_day
        assert cfg.max_failures_per_run == defaults.max_failures_per_run
        assert cfg.max_open_bot_prs == defaults.max_open_bot_prs
        assert cfg.daily_token_budget == defaults.daily_token_budget
        assert cfg.flaky_campaign_enabled == defaults.flaky_campaign_enabled
        assert cfg.flaky_max_attempts_per_run == defaults.flaky_max_attempts_per_run
        assert cfg.flaky_validation_passes == defaults.flaky_validation_passes
        assert cfg.flaky_max_failed_hypotheses == defaults.flaky_max_failed_hypotheses
        assert cfg.soak_validation_workflows == defaults.soak_validation_workflows
        assert cfg.soak_validation_passes == defaults.soak_validation_passes
        assert cfg.retrieval == defaults.retrieval
        assert cfg.project.language == ProjectContext().language
        assert cfg.validation_profiles == []
    finally:
        os.unlink(tmp_path)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    bedrock_data=partial_bedrock,
    limits_data=partial_limits,
    fix_gen_data=partial_fix_generation,
    flaky_data=partial_flaky_campaign,
    extra_fields=unrecognized_fields_strategy,
)
def test_valid_fields_preserved_with_unrecognized_ignored(
    bedrock_data: dict,
    limits_data: dict,
    fix_gen_data: dict,
    flaky_data: dict,
    extra_fields: dict,
) -> None:
    """Property 18 (part 3): Valid recognized fields are preserved while
    unrecognized fields are ignored and missing fields get defaults.

    **Validates: Requirements 8.4**
    """
    config_data: dict = {**extra_fields}
    if bedrock_data:
        config_data["bedrock"] = bedrock_data
    if limits_data:
        config_data["limits"] = limits_data
    if fix_gen_data:
        config_data["fix_generation"] = fix_gen_data
    if flaky_data:
        config_data["flaky_campaign"] = flaky_data

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        yaml.safe_dump(config_data, f)
        tmp_path = f.name

    try:
        cfg = load_config(tmp_path)
        defaults = BotConfig()

        # Valid bedrock fields should be preserved, missing ones get defaults
        assert cfg.bedrock_model_id == bedrock_data.get("model_id", defaults.bedrock_model_id)
        assert cfg.max_input_tokens == bedrock_data.get("max_input_tokens", defaults.max_input_tokens)
        assert cfg.max_output_tokens == bedrock_data.get("max_output_tokens", defaults.max_output_tokens)

        # Valid limits fields should be preserved
        assert cfg.max_patch_files == limits_data.get("max_patch_files", defaults.max_patch_files)
        assert cfg.max_prs_per_day == limits_data.get("max_prs_per_day", defaults.max_prs_per_day)
        assert cfg.max_failures_per_run == limits_data.get("max_failures_per_run", defaults.max_failures_per_run)
        assert cfg.max_open_bot_prs == limits_data.get("max_open_bot_prs", defaults.max_open_bot_prs)
        assert cfg.daily_token_budget == limits_data.get("daily_token_budget", defaults.daily_token_budget)

        # Valid fix_generation fields should be preserved
        assert cfg.confidence_threshold == fix_gen_data.get("confidence_threshold", defaults.confidence_threshold)
        assert cfg.max_retries_fix == fix_gen_data.get("max_retries", defaults.max_retries_fix)
        assert cfg.max_retries_validation == fix_gen_data.get("max_validation_retries", defaults.max_retries_validation)
        assert cfg.flaky_campaign_enabled == flaky_data.get("enabled", defaults.flaky_campaign_enabled)
        assert cfg.flaky_max_attempts_per_run == flaky_data.get("max_attempts_per_run", defaults.flaky_max_attempts_per_run)
        assert cfg.flaky_validation_passes == flaky_data.get("validation_passes", defaults.flaky_validation_passes)
        assert cfg.flaky_max_failed_hypotheses == flaky_data.get("max_failed_hypotheses", defaults.flaky_max_failed_hypotheses)

        # Unrecognized top-level fields should not affect anything
        assert cfg.monitored_workflows == defaults.monitored_workflows
        assert cfg.project.language == ProjectContext().language
        assert cfg.validation_profiles == []
    finally:
        os.unlink(tmp_path)


def test_invalid_scalar_types_fall_back_to_defaults() -> None:
    config_data = {
        "bedrock": {
            "model_id": ["not-a-string"],
            "max_input_tokens": "1000",
        },
        "limits": {
            "max_patch_files": "10",
        },
        "fix_generation": {
            "confidence_threshold": ["high"],
        },
        "monitored_workflows": "ci.yml",
        "retrieval": {
            "enabled": "true",
            "code_knowledge_base_id": ["bad"],
            "max_results_per_knowledge_base": "3",
        },
        "project": {
            "test_frameworks": "gtest",
            "source_dirs": "src/",
            "test_to_source_patterns": {"test_path": "tests/a.tcl"},
        },
        "validation_profiles": [
            {
                "job_name_pattern": 123,
                "matrix_params": ["bad"],
                "env": "oops",
                "install_commands": "pip install",
                "build_commands": ["make"],
                "test_commands": ["ctest"],
            }
        ],
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        yaml.safe_dump(config_data, f)
        tmp_path = f.name

    try:
        cfg = load_config(tmp_path)
        defaults = BotConfig()

        assert cfg.bedrock_model_id == defaults.bedrock_model_id
        assert cfg.max_input_tokens == defaults.max_input_tokens
        assert cfg.max_patch_files == defaults.max_patch_files
        assert cfg.confidence_threshold == defaults.confidence_threshold
        assert cfg.monitored_workflows == defaults.monitored_workflows
        assert cfg.retrieval == defaults.retrieval
        assert cfg.project.test_frameworks == defaults.project.test_frameworks
        assert cfg.project.source_dirs == defaults.project.source_dirs
        assert cfg.project.test_to_source_patterns == defaults.project.test_to_source_patterns
        assert cfg.validation_profiles[0].job_name_pattern == ""
        assert cfg.validation_profiles[0].matrix_params == {}
        assert cfg.validation_profiles[0].env == {}
        assert cfg.validation_profiles[0].install_commands == []
        assert cfg.validation_profiles[0].build_commands == ["make"]
        assert cfg.validation_profiles[0].test_commands == ["ctest"]
    finally:
        os.unlink(tmp_path)

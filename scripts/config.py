"""Configuration loader for the CI Failure Bot."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


@dataclass
class ProjectContext:
    """Project-specific context injected into LLM prompts."""
    language: str = "C"
    build_system: str = "CMake"
    test_frameworks: list[str] = field(default_factory=lambda: ["gtest", "tcl"])
    description: str = ""
    source_dirs: list[str] = field(default_factory=lambda: ["src/"])
    test_dirs: list[str] = field(default_factory=lambda: ["tests/"])
    test_to_source_patterns: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ValidationProfile:
    """Maps a CI job shape to concrete build and test commands."""
    job_name_pattern: str = ""
    matrix_params: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    install_commands: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)


@dataclass
class BotConfig:
    """Top-level bot configuration with sensible defaults."""
    bedrock_model_id: str = "anthropic.claude-opus-4-6-v1"
    max_input_tokens: int = 100_000
    max_output_tokens: int = 4096
    max_patch_files: int = 10
    confidence_threshold: str = "medium"
    monitored_workflows: list[str] = field(default_factory=lambda: [
        "ci.yml", "daily.yml", "weekly.yml", "external.yml"
    ])
    max_retries_fix: int = 2
    max_retries_validation: int = 1
    max_retries_bedrock: int = 3
    max_prs_per_day: int = 5
    max_failures_per_run: int = 10
    max_open_bot_prs: int = 3
    daily_token_budget: int = 1_000_000
    project: ProjectContext = field(default_factory=ProjectContext)
    validation_profiles: list[ValidationProfile] = field(default_factory=list)


def _merge_project(data: dict) -> ProjectContext:
    """Build a ProjectContext from a raw dict, using defaults for missing keys."""
    defaults = ProjectContext()
    return ProjectContext(
        language=data.get("language", defaults.language),
        build_system=data.get("build_system", defaults.build_system),
        test_frameworks=data.get("test_frameworks", defaults.test_frameworks),
        description=data.get("description", defaults.description),
        source_dirs=data.get("source_dirs", defaults.source_dirs),
        test_dirs=data.get("test_dirs", defaults.test_dirs),
        test_to_source_patterns=data.get("test_to_source_patterns", defaults.test_to_source_patterns),
    )


def _merge_validation_profiles(raw_list: list[dict]) -> list[ValidationProfile]:
    """Build ValidationProfile list from raw dicts."""
    profiles: list[ValidationProfile] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        profiles.append(ValidationProfile(
            job_name_pattern=item.get("job_name_pattern", ""),
            matrix_params=item.get("matrix_params", {}),
            env=item.get("env", {}),
            install_commands=item.get("install_commands", []),
            build_commands=item.get("build_commands", []),
            test_commands=item.get("test_commands", []),
        ))
    return profiles


def load_config_data(raw: Any, *, source: str = "<memory>") -> BotConfig:
    """Build a BotConfig from pre-loaded YAML data."""
    if not isinstance(raw, dict):
        logger.warning("Config source %s is not a YAML mapping. Using defaults.", source)
        return BotConfig()

    defaults = BotConfig()

    # Flatten nested sections from the YAML schema into BotConfig fields
    bedrock = raw.get("bedrock", {}) if isinstance(raw.get("bedrock"), dict) else {}
    limits = raw.get("limits", {}) if isinstance(raw.get("limits"), dict) else {}
    fix_gen = raw.get("fix_generation", {}) if isinstance(raw.get("fix_generation"), dict) else {}

    return BotConfig(
        bedrock_model_id=bedrock.get("model_id", defaults.bedrock_model_id),
        max_input_tokens=bedrock.get("max_input_tokens", defaults.max_input_tokens),
        max_output_tokens=bedrock.get("max_output_tokens", defaults.max_output_tokens),
        max_patch_files=limits.get("max_patch_files", defaults.max_patch_files),
        confidence_threshold=fix_gen.get("confidence_threshold", defaults.confidence_threshold),
        monitored_workflows=raw.get("monitored_workflows", defaults.monitored_workflows),
        max_retries_fix=fix_gen.get("max_retries", defaults.max_retries_fix),
        max_retries_validation=fix_gen.get("max_validation_retries", defaults.max_retries_validation),
        max_retries_bedrock=defaults.max_retries_bedrock,
        max_prs_per_day=limits.get("max_prs_per_day", defaults.max_prs_per_day),
        max_failures_per_run=limits.get("max_failures_per_run", defaults.max_failures_per_run),
        max_open_bot_prs=limits.get("max_open_bot_prs", defaults.max_open_bot_prs),
        daily_token_budget=limits.get("daily_token_budget", defaults.daily_token_budget),
        project=_merge_project(raw.get("project", {})) if isinstance(raw.get("project"), dict) else defaults.project,
        validation_profiles=_merge_validation_profiles(raw.get("validation_profiles", [])) if isinstance(raw.get("validation_profiles"), list) else defaults.validation_profiles,
    )


def load_config_text(text: str, *, source: str = "<memory>") -> BotConfig:
    """Load bot configuration from YAML text."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning("Invalid YAML in %s: %s. Using defaults.", source, exc)
        return BotConfig()

    return load_config_data(raw, source=source)


def load_config(path: str | Path) -> BotConfig:
    """Load bot configuration from a YAML file.

    Returns default config if the file is missing or contains invalid YAML.
    Valid fields are merged; invalid/unrecognized fields are ignored with a warning.
    """
    config_path = Path(path)
    if not config_path.exists():
        logger.info("Config file %s not found, using defaults.", config_path)
        return BotConfig()

    try:
        raw = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as exc:
        logger.warning("Invalid YAML in %s: %s. Using defaults.", config_path, exc)
        return BotConfig()
    return load_config_data(raw, source=str(config_path))

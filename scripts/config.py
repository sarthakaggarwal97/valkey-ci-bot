"""Configuration loader for the CI Failure Agent."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

__all__ = [
    "ProjectContext",
    "ValidationProfile",
    "RetrievalConfig",
    "BotConfig",
    "ReviewerModels",
    "ReviewerConfig",
    "load_config",
    "load_config_text",
    "load_config_data",
    "load_reviewer_config",
    "load_reviewer_config_text",
    "load_reviewer_config_data",
]

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
class RetrievalConfig:
    """Optional Bedrock Knowledge Base retrieval settings."""

    enabled: bool = False
    code_knowledge_base_id: str = ""
    docs_knowledge_base_id: str = ""
    max_results_per_knowledge_base: int = 3
    max_chars_per_result: int = 1200
    max_total_chars: int = 5000


@dataclass
class BotConfig:
    """Top-level agent configuration with sensible defaults."""
    bedrock_model_id: str = "us.anthropic.claude-opus-4-6-v1"
    max_input_tokens: int = 100_000
    max_output_tokens: int = 4096
    max_patch_files: int = 30
    max_patch_files_override: int | None = None
    confidence_threshold: str = "medium"
    monitored_workflows: list[str] = field(default_factory=lambda: [
        "ci.yml", "daily.yml", "weekly.yml", "external.yml"
    ])
    max_retries_fix: int = 10
    max_retries_validation: int = 5
    max_retries_bedrock: int = 10
    max_prs_per_day: int = 0
    max_failures_per_run: int = 0
    max_open_bot_prs: int = 0
    queued_pr_max_attempts: int = 0
    daily_token_budget: int = 0
    min_failure_streak_before_queue: int = 1
    max_history_entries_per_test: int = 50
    flaky_campaign_enabled: bool = True
    flaky_max_attempts_per_run: int = 10
    flaky_validation_passes: int = 3
    flaky_max_failed_hypotheses: int = 0
    require_validation_profile: bool = True
    soak_validation_workflows: list[str] = field(default_factory=list)
    soak_validation_passes: int = 1
    thinking_budget: int = 32_000
    project: ProjectContext = field(default_factory=ProjectContext)
    validation_profiles: list[ValidationProfile] = field(default_factory=list)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)

    def __post_init__(self) -> None:
        """Clamp numeric fields to valid ranges."""
        self.max_prs_per_day = max(0, self.max_prs_per_day)
        self.max_open_bot_prs = max(0, self.max_open_bot_prs)
        self.max_failures_per_run = max(0, self.max_failures_per_run)
        self.max_retries_bedrock = max(0, self.max_retries_bedrock)
        self.max_retries_fix = max(0, self.max_retries_fix)
        self.max_retries_validation = max(0, self.max_retries_validation)
        self.daily_token_budget = max(0, self.daily_token_budget)
        self.thinking_budget = max(1024, min(self.thinking_budget, 128_000))
        self.max_input_tokens = max(1, self.max_input_tokens)
        self.max_output_tokens = max(1, self.max_output_tokens)
        self.flaky_validation_passes = max(1, self.flaky_validation_passes)
        if self.confidence_threshold not in ("high", "medium", "low"):
            self.confidence_threshold = "medium"


@dataclass
class ReviewerModels:
    """Model configuration for PR reviewer light/heavy tasks."""

    light_model_id: str = "us.anthropic.claude-sonnet-4-v1"
    heavy_model_id: str = "us.anthropic.claude-opus-4-6-v1"
    temperature: float = 0.0
    thinking_budget: int = 32_000


@dataclass
class ReviewerConfig:
    """Top-level PR reviewer configuration with sensible defaults."""

    enabled: bool = True
    collaborator_only: bool = False
    chat_collaborator_only: bool = True
    disable_review: bool = False
    disable_release_notes: bool = False
    review_simple_changes: bool = True
    review_comment_lgtm: bool = False
    approve_on_no_findings: bool = False
    model_file_triage: bool = False
    post_policy_notes: bool = True
    ignore_keyword: str = "/reviewbot: ignore"
    max_files: int = 150
    max_review_comments: int = 25
    path_filters: list[str] = field(default_factory=list)
    daily_token_budget: int = 1_000_000_000
    bedrock_retries: int = 5
    github_retries: int = 5
    bedrock_timeout_ms: int = 300_000
    bedrock_concurrency_limit: int = 2
    github_concurrency_limit: int = 6
    max_input_tokens: int = 190_000
    max_output_tokens: int = 8192
    custom_instructions: str = ""
    specialist_mode: bool = False
    project: ProjectContext = field(default_factory=ProjectContext)
    models: ReviewerModels = field(default_factory=ReviewerModels)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)

    def __post_init__(self) -> None:
        """Clamp numeric fields to valid ranges."""
        self.max_files = max(1, self.max_files)
        self.max_review_comments = max(1, self.max_review_comments)
        self.bedrock_retries = max(0, self.bedrock_retries)
        self.github_retries = max(0, self.github_retries)
        self.daily_token_budget = max(0, self.daily_token_budget)
        self.max_input_tokens = max(1, self.max_input_tokens)
        self.max_output_tokens = max(1, self.max_output_tokens)

    @property
    def bedrock_model_id(self) -> str:
        """Default reviewer Bedrock model when no per-call override is given."""
        return self.models.heavy_model_id

    @property
    def max_retries_bedrock(self) -> int:
        """Alias used by the shared Bedrock client."""
        return self.bedrock_retries


def _merge_project(data: dict) -> ProjectContext:
    """Build a ProjectContext from a raw dict, using defaults for missing keys."""
    defaults = ProjectContext()
    return ProjectContext(
        language=_coerce_str(
            data.get("language"),
            defaults.language,
        ),
        build_system=_coerce_str(
            data.get("build_system"),
            defaults.build_system,
        ),
        test_frameworks=_coerce_str_list(
            data.get("test_frameworks"),
            defaults.test_frameworks,
        ),
        description=_coerce_str(
            data.get("description"),
            defaults.description,
        ),
        source_dirs=_coerce_str_list(
            data.get("source_dirs"),
            defaults.source_dirs,
        ),
        test_dirs=_coerce_str_list(
            data.get("test_dirs"),
            defaults.test_dirs,
        ),
        test_to_source_patterns=_coerce_pattern_list(
            data.get("test_to_source_patterns"),
            defaults.test_to_source_patterns,
        ),
    )


def _merge_validation_profiles(raw_list: list[dict]) -> list[ValidationProfile]:
    """Build ValidationProfile list from raw dicts."""
    profiles: list[ValidationProfile] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        profiles.append(ValidationProfile(
            job_name_pattern=_coerce_str(item.get("job_name_pattern"), ""),
            matrix_params=_coerce_str_dict(item.get("matrix_params"), {}),
            env=_coerce_str_dict(item.get("env"), {}),
            install_commands=_coerce_str_list(item.get("install_commands"), []),
            build_commands=_coerce_str_list(item.get("build_commands"), []),
            test_commands=_coerce_str_list(item.get("test_commands"), []),
        ))
    return profiles


def _merge_reviewer_models(data: dict) -> ReviewerModels:
    """Build ReviewerModels from a raw dict."""
    defaults = ReviewerModels()
    return ReviewerModels(
        light_model_id=_coerce_str(
            data.get("light_model_id"),
            defaults.light_model_id,
        ),
        heavy_model_id=_coerce_str(
            data.get("heavy_model_id"),
            defaults.heavy_model_id,
        ),
        temperature=_coerce_float(
            data.get("temperature"),
            defaults.temperature,
        ),
        thinking_budget=_coerce_int(
            data.get("thinking_budget"),
            defaults.thinking_budget,
        ),
    )


def _merge_retrieval(data: dict) -> RetrievalConfig:
    """Build RetrievalConfig from a raw dict."""
    defaults = RetrievalConfig()
    return RetrievalConfig(
        enabled=_coerce_bool(data.get("enabled"), defaults.enabled),
        code_knowledge_base_id=_coerce_str(
            data.get("code_knowledge_base_id"),
            defaults.code_knowledge_base_id,
        ),
        docs_knowledge_base_id=_coerce_str(
            data.get("docs_knowledge_base_id"),
            defaults.docs_knowledge_base_id,
        ),
        max_results_per_knowledge_base=_coerce_int(
            data.get("max_results_per_knowledge_base"),
            defaults.max_results_per_knowledge_base,
        ),
        max_chars_per_result=_coerce_int(
            data.get("max_chars_per_result"),
            defaults.max_chars_per_result,
        ),
        max_total_chars=_coerce_int(
            data.get("max_total_chars"),
            defaults.max_total_chars,
        ),
    )


def _coerce_str(value: Any, default: str) -> str:
    """Return a string value or the provided default."""
    return value if isinstance(value, str) else default


def _coerce_str_list(value: Any, default: list[str]) -> list[str]:
    """Return a list of strings or the provided default."""
    if not isinstance(value, list):
        return list(default)
    if not all(isinstance(item, str) for item in value):
        return list(default)
    return list(value)


def _coerce_str_dict(value: Any, default: dict[str, str]) -> dict[str, str]:
    """Return a string-to-string mapping or the provided default."""
    if not isinstance(value, dict):
        return dict(default)
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        return dict(default)
    return dict(value)


def _coerce_pattern_list(
    value: Any,
    default: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return a test-to-source pattern list or the provided default."""
    if not isinstance(value, list):
        return list(default)

    patterns: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            return list(default)
        test_path = item.get("test_path")
        source_path = item.get("source_path")
        if not isinstance(test_path, str) or not isinstance(source_path, str):
            return list(default)
        patterns.append({"test_path": test_path, "source_path": source_path})
    return patterns


def _coerce_int(value: Any, default: int) -> int:
    """Return an integer value or the provided default."""
    if isinstance(value, bool):
        return default
    return value if isinstance(value, int) else default


def _coerce_float(value: Any, default: float) -> float:
    """Return a float value or the provided default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _coerce_bool(value: Any, default: bool) -> bool:
    """Return a boolean value or the provided default."""
    return value if isinstance(value, bool) else default


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
    retrieval = raw.get("retrieval", {}) if isinstance(raw.get("retrieval"), dict) else {}
    flaky_campaign = (
        raw.get("flaky_campaign", {})
        if isinstance(raw.get("flaky_campaign"), dict)
        else {}
    )
    validation = raw.get("validation", {}) if isinstance(raw.get("validation"), dict) else {}

    return BotConfig(
        bedrock_model_id=_coerce_str(
            bedrock.get("model_id"),
            defaults.bedrock_model_id,
        ),
        max_input_tokens=_coerce_int(
            bedrock.get("max_input_tokens"),
            defaults.max_input_tokens,
        ),
        max_output_tokens=_coerce_int(
            bedrock.get("max_output_tokens"),
            defaults.max_output_tokens,
        ),
        max_patch_files=_coerce_int(
            limits.get("max_patch_files"),
            defaults.max_patch_files,
        ),
        max_patch_files_override=_coerce_int(
            limits.get("max_patch_files_override"),
            defaults.max_patch_files_override or 0,
        ) if limits.get("max_patch_files_override") is not None else defaults.max_patch_files_override,
        confidence_threshold=_coerce_str(
            fix_gen.get("confidence_threshold"),
            defaults.confidence_threshold,
        ),
        monitored_workflows=_coerce_str_list(
            raw.get("monitored_workflows"),
            defaults.monitored_workflows,
        ),
        max_retries_fix=_coerce_int(
            fix_gen.get("max_retries"),
            defaults.max_retries_fix,
        ),
        max_retries_validation=_coerce_int(
            fix_gen.get("max_validation_retries"),
            defaults.max_retries_validation,
        ),
        max_retries_bedrock=_coerce_int(
            bedrock.get("max_retries"),
            defaults.max_retries_bedrock,
        ),
        max_prs_per_day=_coerce_int(
            limits.get("max_prs_per_day"),
            defaults.max_prs_per_day,
        ),
        max_failures_per_run=_coerce_int(
            limits.get("max_failures_per_run"),
            defaults.max_failures_per_run,
        ),
        max_open_bot_prs=_coerce_int(
            limits.get("max_open_bot_prs"),
            defaults.max_open_bot_prs,
        ),
        queued_pr_max_attempts=_coerce_int(
            limits.get("queued_pr_max_attempts"),
            defaults.queued_pr_max_attempts,
        ),
        daily_token_budget=_coerce_int(
            limits.get("daily_token_budget"),
            defaults.daily_token_budget,
        ),
        min_failure_streak_before_queue=_coerce_int(
            limits.get("min_failure_streak_before_queue"),
            defaults.min_failure_streak_before_queue,
        ),
        max_history_entries_per_test=_coerce_int(
            limits.get("max_history_entries_per_test"),
            defaults.max_history_entries_per_test,
        ),
        flaky_campaign_enabled=_coerce_bool(
            flaky_campaign.get("enabled"),
            defaults.flaky_campaign_enabled,
        ),
        flaky_max_attempts_per_run=_coerce_int(
            flaky_campaign.get("max_attempts_per_run"),
            defaults.flaky_max_attempts_per_run,
        ),
        flaky_validation_passes=_coerce_int(
            flaky_campaign.get("validation_passes"),
            defaults.flaky_validation_passes,
        ),
        flaky_max_failed_hypotheses=_coerce_int(
            flaky_campaign.get("max_failed_hypotheses"),
            defaults.flaky_max_failed_hypotheses,
        ),
        require_validation_profile=_coerce_bool(
            validation.get("require_profile"),
            defaults.require_validation_profile,
        ),
        soak_validation_workflows=_coerce_str_list(
            validation.get("soak_workflows"),
            defaults.soak_validation_workflows,
        ),
        soak_validation_passes=_coerce_int(
            validation.get("soak_passes"),
            defaults.soak_validation_passes,
        ),
        thinking_budget=_coerce_int(
            bedrock.get("thinking_budget"),
            defaults.thinking_budget,
        ),
        project=_merge_project(raw.get("project", {})) if isinstance(raw.get("project"), dict) else defaults.project,
        validation_profiles=_merge_validation_profiles(raw.get("validation_profiles", [])) if isinstance(raw.get("validation_profiles"), list) else defaults.validation_profiles,
        retrieval=_merge_retrieval(retrieval),
    )


def load_config_text(text: str, *, source: str = "<memory>") -> BotConfig:
    """Load agent configuration from YAML text."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning("Invalid YAML in %s: %s. Using defaults.", source, exc)
        return BotConfig()

    return load_config_data(raw, source=source)


def load_config(path: str | Path) -> BotConfig:
    """Load agent configuration from a YAML file.

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


def load_reviewer_config_data(raw: Any, *, source: str = "<memory>") -> ReviewerConfig:
    """Build a ReviewerConfig from pre-loaded YAML data."""
    if not isinstance(raw, dict):
        logger.warning(
            "Reviewer config source %s is not a YAML mapping. Using defaults.",
            source,
        )
        return ReviewerConfig()

    root = raw.get("reviewer") if isinstance(raw.get("reviewer"), dict) else raw
    if not isinstance(root, dict):
        logger.warning(
            "Reviewer config source %s has invalid 'reviewer' section. Using defaults.",
            source,
        )
        return ReviewerConfig()

    defaults = ReviewerConfig()
    models = root.get("models", {}) if isinstance(root.get("models"), dict) else {}
    project = root.get("project", {}) if isinstance(root.get("project"), dict) else {}
    retrieval = root.get("retrieval", {}) if isinstance(root.get("retrieval"), dict) else {}

    return ReviewerConfig(
        enabled=_coerce_bool(root.get("enabled"), defaults.enabled),
        collaborator_only=_coerce_bool(
            root.get("collaborator_only", defaults.collaborator_only)
            if "collaborator_only" in root else defaults.collaborator_only,
            defaults.collaborator_only,
        ),
        chat_collaborator_only=_coerce_bool(
            root.get("chat_collaborator_only", defaults.chat_collaborator_only)
            if "chat_collaborator_only" in root else defaults.chat_collaborator_only,
            defaults.chat_collaborator_only,
        ),
        disable_review=_coerce_bool(
            root.get("disable_review"), defaults.disable_review
        ),
        disable_release_notes=_coerce_bool(
            root.get("disable_release_notes", defaults.disable_release_notes)
            if "disable_release_notes" in root else defaults.disable_release_notes,
            defaults.disable_release_notes,
        ),
        review_simple_changes=_coerce_bool(
            root.get("review_simple_changes", defaults.review_simple_changes)
            if "review_simple_changes" in root else defaults.review_simple_changes,
            defaults.review_simple_changes,
        ),
        review_comment_lgtm=_coerce_bool(
            root.get("review_comment_lgtm", defaults.review_comment_lgtm)
            if "review_comment_lgtm" in root else defaults.review_comment_lgtm,
            defaults.review_comment_lgtm,
        ),
        approve_on_no_findings=_coerce_bool(
            root.get("approve_on_no_findings", defaults.approve_on_no_findings)
            if "approve_on_no_findings" in root else defaults.approve_on_no_findings,
            defaults.approve_on_no_findings,
        ),
        model_file_triage=_coerce_bool(
            root.get("model_file_triage", defaults.model_file_triage)
            if "model_file_triage" in root else defaults.model_file_triage,
            defaults.model_file_triage,
        ),
        post_policy_notes=_coerce_bool(
            root.get("post_policy_notes", defaults.post_policy_notes)
            if "post_policy_notes" in root else defaults.post_policy_notes,
            defaults.post_policy_notes,
        ),
        ignore_keyword=_coerce_str(
            root.get("ignore_keyword"),
            defaults.ignore_keyword,
        ),
        max_files=_coerce_int(root.get("max_files"), defaults.max_files),
        max_review_comments=_coerce_int(
            root.get("max_review_comments", defaults.max_review_comments)
            if "max_review_comments" in root else defaults.max_review_comments,
            defaults.max_review_comments,
        ),
        path_filters=_coerce_str_list(
            root.get("path_filters"),
            defaults.path_filters,
        ),
        daily_token_budget=_coerce_int(
            root.get("daily_token_budget", defaults.daily_token_budget)
            if "daily_token_budget" in root else defaults.daily_token_budget,
            defaults.daily_token_budget,
        ),
        bedrock_retries=_coerce_int(
            root.get("bedrock_retries"), defaults.bedrock_retries
        ),
        github_retries=_coerce_int(
            root.get("github_retries"), defaults.github_retries
        ),
        bedrock_timeout_ms=_coerce_int(
            root.get("bedrock_timeout_ms", defaults.bedrock_timeout_ms)
            if "bedrock_timeout_ms" in root else defaults.bedrock_timeout_ms,
            defaults.bedrock_timeout_ms,
        ),
        bedrock_concurrency_limit=_coerce_int(
            root.get(
                "bedrock_concurrency_limit", defaults.bedrock_concurrency_limit
            ) if "bedrock_concurrency_limit" in root else defaults.bedrock_concurrency_limit,
            defaults.bedrock_concurrency_limit,
        ),
        github_concurrency_limit=_coerce_int(
            root.get("github_concurrency_limit", defaults.github_concurrency_limit)
            if "github_concurrency_limit" in root else defaults.github_concurrency_limit,
            defaults.github_concurrency_limit,
        ),
        max_input_tokens=_coerce_int(
            root.get("max_input_tokens", defaults.max_input_tokens)
            if "max_input_tokens" in root else defaults.max_input_tokens,
            defaults.max_input_tokens,
        ),
        max_output_tokens=_coerce_int(
            root.get("max_output_tokens", defaults.max_output_tokens)
            if "max_output_tokens" in root else defaults.max_output_tokens,
            defaults.max_output_tokens,
        ),
        custom_instructions=_coerce_str(
            root.get("custom_instructions"),
            defaults.custom_instructions,
        ),
        specialist_mode=_coerce_bool(
            root.get("specialist_mode"),
            defaults.specialist_mode,
        ),
        project=_merge_project(project) if project else defaults.project,
        models=_merge_reviewer_models(models),
        retrieval=_merge_retrieval(retrieval),
    )


def load_reviewer_config_text(text: str, *, source: str = "<memory>") -> ReviewerConfig:
    """Load PR reviewer configuration from YAML text."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning(
            "Invalid YAML in reviewer config %s: %s. Using defaults.",
            source,
            exc,
        )
        return ReviewerConfig()

    return load_reviewer_config_data(raw, source=source)


def load_reviewer_config(path: str | Path) -> ReviewerConfig:
    """Load PR reviewer configuration from a YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        logger.info("Reviewer config file %s not found, using defaults.", config_path)
        return ReviewerConfig()

    try:
        raw = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as exc:
        logger.warning(
            "Invalid YAML in reviewer config %s: %s. Using defaults.",
            config_path,
            exc,
        )
        return ReviewerConfig()
    return load_reviewer_config_data(raw, source=str(config_path))

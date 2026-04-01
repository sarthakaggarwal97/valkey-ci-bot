"""Configuration loader for the Backport Agent."""

from __future__ import annotations

import logging
from typing import Any

import yaml  # type: ignore[import-untyped]
from github import Github
from github.GithubException import GithubException

from scripts.backport_models import BackportConfig
from scripts.config import load_repo_file_text
from scripts.github_client import retry_github_call

logger = logging.getLogger(__name__)


def _coerce_str(value: Any, default: str) -> str:
    """Return a string value or the provided default."""
    return value if isinstance(value, str) else default


def _coerce_int(value: Any, default: int) -> int:
    """Return an integer value or the provided default."""
    if isinstance(value, bool):
        return default
    return value if isinstance(value, int) else default


def load_backport_config(raw: Any) -> BackportConfig:
    """Parse a YAML dict into BackportConfig with defaults.

    If *raw* is not a dict (including ``None``), all defaults are used.
    Individual fields that are missing or have the wrong type fall back to
    their default values.
    """
    if not isinstance(raw, dict):
        logger.warning("Backport config is not a YAML mapping. Using defaults.")
        return BackportConfig()

    defaults = BackportConfig()

    return BackportConfig(
        bedrock_model_id=_coerce_str(
            raw.get("bedrock_model_id"),
            defaults.bedrock_model_id,
        ),
        max_conflict_retries=_coerce_int(
            raw.get("max_conflict_retries"),
            defaults.max_conflict_retries,
        ),
        max_conflicting_files=_coerce_int(
            raw.get("max_conflicting_files"),
            defaults.max_conflicting_files,
        ),
        max_prs_per_day=_coerce_int(
            raw.get("max_prs_per_day"),
            defaults.max_prs_per_day,
        ),
        per_backport_token_budget=_coerce_int(
            raw.get("per_backport_token_budget"),
            defaults.per_backport_token_budget,
        ),
        backport_label=_coerce_str(
            raw.get("backport_label"),
            defaults.backport_label,
        ),
        llm_conflict_label=_coerce_str(
            raw.get("llm_conflict_label"),
            defaults.llm_conflict_label,
        ),
    )


def load_backport_config_from_repo(
    github_client: Github,
    repo_full_name: str,
    config_path: str,
) -> BackportConfig:
    """Load backport config from a consumer repo, falling back to defaults.

    Fetches ``config_path`` from the repository via the GitHub API, parses
    the YAML content, and returns a :class:`BackportConfig`.  When the file
    does not exist (HTTP 404) or cannot be parsed, sensible defaults are
    returned instead.
    """
    try:
        raw_text, resolved_ref = retry_github_call(
            lambda: load_repo_file_text(
                github_client,
                repo_full_name,
                config_path,
            ),
            retries=3,
            description=f"fetch backport config from {repo_full_name}:{config_path}",
        )
    except GithubException as exc:
        if exc.status == 404:
            logger.info(
                "Backport config file %s not found in %s. Using defaults.",
                config_path,
                repo_full_name,
            )
            return BackportConfig()
        raise

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        logger.warning(
            "Invalid YAML in backport config %s@%s:%s: %s. Using defaults.",
            repo_full_name,
            resolved_ref,
            config_path,
            exc,
        )
        return BackportConfig()

    return load_backport_config(raw)

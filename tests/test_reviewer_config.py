"""Tests for PR reviewer configuration loading."""

from __future__ import annotations

from scripts.config import (
    ReviewerConfig,
    load_reviewer_config_data,
    load_reviewer_config_text,
)


def test_load_reviewer_config_defaults() -> None:
    config = load_reviewer_config_data({}, source="test")

    assert isinstance(config, ReviewerConfig)
    assert config.enabled is True
    assert config.collaborator_only is False
    assert config.chat_collaborator_only is True
    assert config.ignore_keyword == "/reviewbot: ignore"
    assert config.daily_token_budget == 0
    assert config.approve_on_no_findings is False
    assert config.post_policy_notes is True


def test_load_reviewer_config_nested_section() -> None:
    config = load_reviewer_config_data(
        {
            "reviewer": {
                "collaborator_only": True,
                "chat_collaborator_only": False,
                "approve_on_no_findings": True,
                "post_policy_notes": False,
                "path_filters": ["src/**", "!src/generated/**"],
            }
        },
        source="test",
    )

    assert config.collaborator_only is True
    assert config.chat_collaborator_only is False
    assert config.approve_on_no_findings is True
    assert config.post_policy_notes is False
    assert config.path_filters == ["src/**", "!src/generated/**"]


def test_load_reviewer_config_invalid_yaml_uses_defaults() -> None:
    config = load_reviewer_config_text("reviewer: [", source="broken")

    assert isinstance(config, ReviewerConfig)
    assert config.enabled is True
    assert config.disable_review is False

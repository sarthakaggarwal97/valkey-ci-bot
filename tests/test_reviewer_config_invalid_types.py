"""Regression tests for reviewer config invalid field types."""

from __future__ import annotations

from scripts.config import ReviewerConfig, load_reviewer_config_data


def test_reviewer_config_invalid_types_fall_back_to_defaults() -> None:
    config = load_reviewer_config_data(
        {
            "reviewer": {
                "enabled": "false",
                "collaborator_only": "true",
                "max_files": "150",
                "max_review_comments": "20",
                "path_filters": "src/**",
                "daily_token_budget": "1000000",
                "bedrock_retries": "5",
                "github_retries": "5",
                "agent": {
                    "enabled": "true",
                    "agent_id": ["bad"],
                    "agent_alias_id": 123,
                },
                "models": {
                    "light_model_id": ["bad"],
                    "temperature": "0.2",
                },
                "project": {
                    "test_frameworks": "gtest",
                },
            }
        },
        source="test",
    )

    defaults = ReviewerConfig()
    assert config.enabled == defaults.enabled
    assert config.collaborator_only == defaults.collaborator_only
    assert config.max_files == defaults.max_files
    assert config.max_review_comments == defaults.max_review_comments
    assert config.path_filters == defaults.path_filters
    assert config.daily_token_budget == defaults.daily_token_budget
    assert config.bedrock_retries == defaults.bedrock_retries
    assert config.github_retries == defaults.github_retries
    assert config.agent == defaults.agent
    assert config.models.light_model_id == defaults.models.light_model_id
    assert config.models.temperature == defaults.models.temperature
    assert config.project.test_frameworks == defaults.project.test_frameworks

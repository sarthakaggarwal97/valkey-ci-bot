"""Regression tests for reviewer config invalid field types."""

from __future__ import annotations

from scripts.config import ReviewerConfig, load_reviewer_config_data


def test_reviewer_config_invalid_types_fall_back_to_defaults() -> None:
    config = load_reviewer_config_data(
        {
            "reviewer": {
                "enabled": "false",
                "collaborator_only": "true",
                "chat_collaborator_only": "false",
                "approve_on_no_findings": "true",
                "post_policy_notes": "false",
                "max_files": "150",
                "path_filters": "src/**",
                "daily_token_budget": "1000000",
                "github_retries": "5",
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
    assert config.chat_collaborator_only == defaults.chat_collaborator_only
    assert config.approve_on_no_findings == defaults.approve_on_no_findings
    assert config.post_policy_notes == defaults.post_policy_notes
    assert config.max_files == defaults.max_files
    assert config.path_filters == defaults.path_filters
    assert config.daily_token_budget == defaults.daily_token_budget
    assert config.github_retries == defaults.github_retries
    assert config.project.test_frameworks == defaults.project.test_frameworks

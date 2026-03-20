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
    assert config.ignore_keyword == "/reviewbot: ignore"
    assert config.daily_token_budget == 1_000_000_000
    assert config.models.light_model_id
    assert config.models.heavy_model_id


def test_load_reviewer_config_nested_section() -> None:
    config = load_reviewer_config_data(
        {
            "reviewer": {
                "collaborator_only": True,
                "disable_release_notes": True,
                "max_review_comments": 7,
                "path_filters": ["src/**", "!src/generated/**"],
                "retrieval": {
                    "enabled": True,
                    "code_knowledge_base_id": "CODEKB",
                    "docs_knowledge_base_id": "DOCSKB",
                    "max_results_per_knowledge_base": 2,
                },
                "models": {
                    "light_model_id": "light-model",
                    "heavy_model_id": "heavy-model",
                    "temperature": 0.15,
                },
            }
        },
        source="test",
    )

    assert config.collaborator_only is True
    assert config.disable_release_notes is True
    assert config.max_review_comments == 7
    assert config.path_filters == ["src/**", "!src/generated/**"]
    assert config.retrieval.enabled is True
    assert config.retrieval.code_knowledge_base_id == "CODEKB"
    assert config.retrieval.docs_knowledge_base_id == "DOCSKB"
    assert config.retrieval.max_results_per_knowledge_base == 2
    assert config.models.light_model_id == "light-model"
    assert config.models.heavy_model_id == "heavy-model"
    assert config.models.temperature == 0.15


def test_load_reviewer_config_invalid_yaml_uses_defaults() -> None:
    config = load_reviewer_config_text("reviewer: [", source="broken")

    assert isinstance(config, ReviewerConfig)
    assert config.enabled is True
    assert config.disable_review is False

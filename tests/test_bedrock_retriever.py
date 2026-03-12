"""Tests for Bedrock Knowledge Base retrieval helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import NoCredentialsError

from scripts.bedrock_retriever import BedrockRetriever
from scripts.config import RetrievalConfig


def test_render_for_prompt_includes_code_and_docs_results() -> None:
    client = MagicMock()
    client.retrieve.side_effect = [
        {
            "retrievalResults": [
                {
                    "content": {"text": "sentinel.c snippet"},
                    "metadata": {"path": "src/sentinel.c"},
                    "score": 0.7,
                }
            ]
        },
        {
            "retrievalResults": [
                {
                    "content": {"text": "sentinel docs snippet"},
                    "location": {
                        "type": "WEB",
                        "webLocation": {"url": "https://valkey.io/topics/sentinel"},
                    },
                    "score": 0.6,
                }
            ]
        },
    ]
    retriever = BedrockRetriever(client)
    config = RetrievalConfig(
        enabled=True,
        code_knowledge_base_id="CODEKB",
        docs_knowledge_base_id="DOCSKB",
        max_results_per_knowledge_base=2,
        max_chars_per_result=100,
        max_total_chars=1000,
    )

    rendered = retriever.render_for_prompt("failover timeout", config)

    assert "CODE KB | src/sentinel.c" in rendered
    assert "DOCS KB | https://valkey.io/topics/sentinel" in rendered
    assert "sentinel.c snippet" in rendered
    assert "sentinel docs snippet" in rendered


def test_retrieve_uses_cache_for_repeat_query() -> None:
    client = MagicMock()
    client.retrieve.return_value = {
        "retrievalResults": [
            {
                "content": {"text": "replication context"},
                "metadata": {"path": "src/replication.c"},
                "score": 0.5,
            }
        ]
    }
    retriever = BedrockRetriever(client)
    config = RetrievalConfig(
        enabled=True,
        code_knowledge_base_id="CODEKB",
    )

    first = retriever.retrieve("replication", config)
    second = retriever.retrieve("replication", config)

    assert first == second
    client.retrieve.assert_called_once()


def test_disabled_retrieval_returns_empty_output() -> None:
    retriever = BedrockRetriever(MagicMock())
    config = RetrievalConfig(enabled=False, code_knowledge_base_id="CODEKB")

    assert retriever.retrieve("query", config) == []
    assert retriever.render_for_prompt("query", config) == ""


def test_retrieve_handles_botocore_errors_gracefully() -> None:
    client = MagicMock()
    client.retrieve.side_effect = NoCredentialsError()
    retriever = BedrockRetriever(client)
    config = RetrievalConfig(enabled=True, code_knowledge_base_id="CODEKB")

    assert retriever.retrieve("query", config) == []
    assert retriever.render_for_prompt("query", config) == ""

"""Tests for the Bedrock Agent runtime client wrapper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from scripts.bedrock_agent_client import BedrockAgentClient
from scripts.bedrock_client import BedrockError
from scripts.config import ReviewerAgent, ReviewerConfig


def _make_client_error(code: str, message: str = "error") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "InvokeAgent",
    )


def _make_agent_response(*chunks: str) -> dict:
    return {
        "completion": [
            {"chunk": {"bytes": chunk.encode("utf-8")}}
            for chunk in chunks
        ]
    }


def _make_config() -> ReviewerConfig:
    return ReviewerConfig(
        agent=ReviewerAgent(
            enabled=True,
            agent_id="agent-123",
            agent_alias_id="alias-456",
        )
    )


def test_invoke_reads_agent_completion_stream() -> None:
    client = MagicMock()
    client.invoke_agent.return_value = _make_agent_response(
        "<answer>",
        "{\"ok\": true}",
        "</answer>",
    )
    agent = BedrockAgentClient(config=_make_config(), client=client)

    result = agent.invoke("System prompt", "User prompt")

    assert result == "{\"ok\": true}"
    kwargs = client.invoke_agent.call_args.kwargs
    assert kwargs["agentId"] == "agent-123"
    assert kwargs["agentAliasId"] == "alias-456"
    assert "endSession" not in kwargs


def test_invoke_rejects_return_control() -> None:
    client = MagicMock()
    client.invoke_agent.return_value = {"completion": [{"returnControl": {}}]}
    agent = BedrockAgentClient(config=_make_config(), client=client)

    with pytest.raises(BedrockError, match="requested external action"):
        agent.invoke("System prompt", "User prompt")


@patch("scripts.bedrock_agent_client.time.sleep")
def test_invoke_retries_retryable_errors(mock_sleep) -> None:
    client = MagicMock()
    client.invoke_agent.side_effect = [
        _make_client_error("ThrottlingException", "Rate exceeded"),
        _make_agent_response("ok"),
    ]
    agent = BedrockAgentClient(config=_make_config(), client=client)

    result = agent.invoke("System prompt", "User prompt")

    assert result == "ok"
    assert client.invoke_agent.call_count == 2
    assert mock_sleep.call_count == 1


def test_invoke_enforces_input_budget() -> None:
    config = _make_config()
    config.max_input_tokens = 1
    client = MagicMock()
    agent = BedrockAgentClient(config=config, client=client)

    with pytest.raises(BedrockError) as exc_info:
        agent.invoke("System prompt", "This prompt is too long")

    assert exc_info.value.error_code == "InputTooLarge"
    client.invoke_agent.assert_not_called()

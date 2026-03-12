"""Amazon Bedrock Agent Runtime wrapper for the PR reviewer."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Protocol

from botocore.exceptions import ClientError

from scripts.bedrock_client import (
    BedrockError,
    PromptClient,
    TokenBudgetLimiter,
    _build_project_context_text,
    _compute_backoff_delay,
    _estimate_tokens,
    _is_retryable_error,
)
from scripts.config import ProjectContext


class BedrockAgentRuntimeClient(Protocol):
    """Subset of the Bedrock Agent Runtime client used by this module."""

    def invoke_agent(self, **kwargs: Any) -> dict: ...


class BedrockAgentConfig(Protocol):
    """Configuration shape required by the Bedrock agent client."""

    @property
    def bedrock_agent_id(self) -> str: ...

    @property
    def bedrock_agent_alias_id(self) -> str: ...

    @property
    def max_input_tokens(self) -> int: ...

    @property
    def max_output_tokens(self) -> int: ...

    @property
    def max_retries_bedrock(self) -> int: ...

    @property
    def project(self) -> ProjectContext: ...


_ANSWER_TAG_PATTERN = re.compile(
    r"<(?:answer|final_response)>(.*?)</(?:answer|final_response)>",
    re.DOTALL | re.IGNORECASE,
)


def _strip_agent_wrappers(text: str) -> str:
    """Normalize common Bedrock Agent wrapper tags around the final answer."""
    match = _ANSWER_TAG_PATTERN.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


class BedrockAgentClient(PromptClient):
    """Wrapper around Bedrock Agent Runtime invoke_agent."""

    def __init__(
        self,
        config: BedrockAgentConfig,
        *,
        client: BedrockAgentRuntimeClient,
        rate_limiter: TokenBudgetLimiter | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._project_context = _build_project_context_text(config.project)
        self._rate_limiter = rate_limiter

    def invoke(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model_id: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        del model_id, temperature

        composed_prompt = (
            f"{system_prompt}\n\n"
            f"## Project Context\n{self._project_context}\n\n"
            f"## Task\n{user_prompt}"
        )
        estimated_input_tokens = _estimate_tokens(composed_prompt)
        if estimated_input_tokens > self._config.max_input_tokens:
            raise BedrockError(
                "Bedrock agent prompt exceeds max_input_tokens.",
                error_code="InputTooLarge",
                retryable=False,
            )

        output_tokens = max_output_tokens or self._config.max_output_tokens
        reserved_tokens = estimated_input_tokens + output_tokens
        max_attempts = self._config.max_retries_bedrock + 1
        session_id = uuid.uuid4().hex
        last_error: ClientError | None = None

        for attempt in range(max_attempts):
            if self._rate_limiter is not None:
                if not self._rate_limiter.can_use_tokens(reserved_tokens):
                    raise BedrockError(
                        "daily token budget exhausted",
                        error_code="TokenBudgetExceeded",
                        retryable=False,
                    )
                self._rate_limiter.record_token_usage(reserved_tokens)

            try:
                response = self._client.invoke_agent(
                    agentId=self._config.bedrock_agent_id,
                    agentAliasId=self._config.bedrock_agent_alias_id,
                    sessionId=session_id,
                    inputText=composed_prompt,
                    enableTrace=False,
                )
                return _strip_agent_wrappers(_read_completion_stream(response))
            except ClientError as exc:
                last_error = exc
                if not _is_retryable_error(exc):
                    message = exc.response.get("Error", {}).get("Message", str(exc))
                    raise BedrockError(
                        f"Bedrock Agent API error: {message}",
                        error_code=exc.response.get("Error", {}).get("Code"),
                        retryable=False,
                    ) from exc
                if attempt >= max_attempts - 1:
                    message = exc.response.get("Error", {}).get("Message", str(exc))
                    raise BedrockError(
                        f"Bedrock Agent API error after {max_attempts} attempts: {message}",
                        error_code=exc.response.get("Error", {}).get("Code"),
                        retryable=True,
                    ) from exc
            delay = _compute_backoff_delay(attempt)
            time.sleep(delay)

        raise BedrockError(
            f"Bedrock Agent API error after {max_attempts} attempts",
            error_code=(
                last_error.response.get("Error", {}).get("Code")
                if last_error is not None else None
            ),
            retryable=True,
        )


def _read_completion_stream(response: dict[str, Any]) -> str:
    """Extract text content from a Bedrock Agent Runtime completion stream."""
    completion = response.get("completion")
    if completion is None:
        raise BedrockError(
            "Unexpected Bedrock Agent response format: missing completion stream.",
            error_code="UnexpectedResponse",
            retryable=False,
        )

    chunks: list[str] = []
    for event in completion:
        if not isinstance(event, dict):
            continue
        if "chunk" in event:
            data = event["chunk"].get("bytes")
            if isinstance(data, (bytes, bytearray)):
                chunks.append(data.decode("utf-8", errors="replace"))
            elif isinstance(data, str):
                chunks.append(data)
        elif "returnControl" in event:
            raise BedrockError(
                "Bedrock agent requested external action.",
                error_code="ReturnControl",
                retryable=False,
            )

    text = "".join(chunks).strip()
    if not text:
        raise BedrockError(
            "Unexpected Bedrock Agent response format: empty completion stream.",
            error_code="UnexpectedResponse",
            retryable=False,
        )
    return text

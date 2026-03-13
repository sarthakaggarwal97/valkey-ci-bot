"""Amazon Bedrock API wrapper for the CI Failure Bot.

Provides a client that calls the Bedrock Converse API with retry logic,
token limit enforcement, and project context injection.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Protocol

import boto3
from botocore.exceptions import ClientError

from scripts.config import ProjectContext

logger = logging.getLogger(__name__)

# Retry constants
_BASE_DELAY = 1.0  # seconds
_MAX_DELAY = 30.0  # seconds

# HTTP status codes
_THROTTLING_CODE = "ThrottlingException"
_SERVICE_UNAVAILABLE_CODE = "ServiceUnavailableException"
_INTERNAL_SERVER_ERROR_CODE = "InternalServerException"
_MODEL_ERROR_CODE = "ModelErrorException"

_RETRYABLE_ERROR_CODES = frozenset({
    _THROTTLING_CODE,
    _SERVICE_UNAVAILABLE_CODE,
    _INTERNAL_SERVER_ERROR_CODE,
    _MODEL_ERROR_CODE,
    "ThrottledException",
    "TooManyRequestsException",
    "ServiceException",
})


class BedrockError(Exception):
    """Raised when a Bedrock API call fails with a non-retryable error
    or after exhausting all retries."""

    def __init__(self, message: str, *, error_code: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class BedrockRuntimeClient(Protocol):
    """Subset of the Bedrock runtime client used by this module."""

    def converse(self, **kwargs: Any) -> dict: ...


class PromptClient(Protocol):
    """Common interface for model and agent-backed prompt execution."""

    def invoke(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model_id: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str: ...

    def invoke_with_schema(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        tool_name: str,
        tool_description: str,
        json_schema: dict,
        model_id: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Invoke with a tool-use JSON schema for structured output.

        Implementations that don't support tool use should fall back to
        a plain ``invoke`` call.
        """
        ...


class TokenBudgetLimiter(Protocol):
    """Interface used for coarse Bedrock budget enforcement."""

    def can_use_tokens(self, amount: int) -> bool: ...
    def record_token_usage(self, amount: int) -> None: ...


class BedrockConfig(Protocol):
    """Configuration shape required by the Bedrock client."""

    @property
    def bedrock_model_id(self) -> str: ...

    @property
    def max_input_tokens(self) -> int: ...

    @property
    def max_output_tokens(self) -> int: ...

    @property
    def max_retries_bedrock(self) -> int: ...

    @property
    def project(self) -> ProjectContext: ...


def _build_project_context_text(project: ProjectContext) -> str:
    """Format project context for inclusion in the system prompt."""
    parts = [
        f"Language: {project.language}",
        f"Build system: {project.build_system}",
        f"Test frameworks: {', '.join(project.test_frameworks)}",
    ]
    if project.description:
        parts.append(f"Project description: {project.description}")
    return "\n".join(parts)


def _compute_backoff_delay(attempt: int) -> float:
    """Compute exponential backoff delay with full jitter.

    delay = random(0, min(max_delay, base_delay * 2^attempt))
    """
    exp_delay = min(_MAX_DELAY, _BASE_DELAY * (2 ** attempt))
    return random.uniform(0, exp_delay)


def _is_retryable_error(error: ClientError) -> bool:
    """Check if a botocore ClientError is retryable."""
    error_code = error.response.get("Error", {}).get("Code", "")
    return error_code in _RETRYABLE_ERROR_CODES


def _estimate_tokens(text: str) -> int:
    """Estimate token count conservatively from text length."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


class BedrockClient:
    """Wrapper around the Amazon Bedrock Converse API.

    Handles authentication, token limits, retry logic, and project
    context injection for all model invocations.
    """

    def __init__(
        self,
        config: BedrockConfig,
        *,
        client: BedrockRuntimeClient | None = None,
        rate_limiter: TokenBudgetLimiter | None = None,
    ):
        """Initialize the Bedrock client.

        Args:
            config: Bot configuration containing model ID, token limits, and
                project context.
            client: Optional pre-configured boto3 bedrock-runtime client.
                If not provided, one is created using default credentials
                (IAM credentials from environment / GitHub Actions secrets).
        """
        self._config = config
        self._client = client or boto3.client("bedrock-runtime")
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
        """Call Bedrock Converse API with the configured model.

        Includes project context in the system prompt, enforces token limits,
        and retries with exponential backoff on throttling/service errors.

        Args:
            system_prompt: The system-level instructions for the model.
            user_prompt: The user message to send to the model.
            model_id: Optional model override for this call.
            max_output_tokens: Optional output-token override for this call.
            temperature: Optional sampling temperature for this call.

        Returns:
            The model's text response.

        Raises:
            BedrockError: On non-retryable errors or after exhausting retries.
        """
        full_system_prompt = (
            f"{system_prompt}\n\n"
            f"## Project Context\n{self._project_context}"
        )
        estimated_input_tokens = (
            _estimate_tokens(full_system_prompt) + _estimate_tokens(user_prompt)
        )
        if estimated_input_tokens > self._config.max_input_tokens:
            raise BedrockError(
                "Bedrock input prompt exceeds max_input_tokens.",
                error_code="InputTooLarge",
                retryable=False,
            )

        output_tokens = max_output_tokens or self._config.max_output_tokens
        messages = [
            {
                "role": "user",
                "content": [{"text": user_prompt}],
            }
        ]

        converse_kwargs: dict[str, Any] = {
            "modelId": model_id or self._config.bedrock_model_id,
            "system": [{"text": full_system_prompt}],
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": output_tokens,
            },
        }
        if temperature is not None:
            converse_kwargs["inferenceConfig"]["temperature"] = temperature

        max_attempts = self._config.max_retries_bedrock + 1  # initial + retries
        last_error: ClientError | None = None
        reserved_tokens = estimated_input_tokens + output_tokens

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
                response = self._client.converse(**converse_kwargs)
                # Track actual token usage from the API response when available
                self._adjust_token_usage(response, reserved_tokens)
                return self._extract_response_text(response)

            except ClientError as exc:
                last_error = exc
                error_code = exc.response.get("Error", {}).get("Code", "")
                error_message = exc.response.get("Error", {}).get("Message", str(exc))

                if not _is_retryable_error(exc):
                    logger.error(
                        "Non-retryable Bedrock error (code=%s): %s",
                        error_code, error_message,
                    )
                    raise BedrockError(
                        f"Bedrock API error: {error_message}",
                        error_code=error_code,
                        retryable=False,
                    ) from exc

                retries_left = max_attempts - attempt - 1
                if retries_left == 0:
                    logger.error(
                        "Bedrock retries exhausted after %d attempts (code=%s): %s",
                        max_attempts, error_code, error_message,
                    )
                    raise BedrockError(
                        f"Bedrock API error after {max_attempts} attempts: {error_message}",
                        error_code=error_code,
                        retryable=True,
                    ) from exc

                delay = _compute_backoff_delay(attempt)
                logger.warning(
                    "Retryable Bedrock error (code=%s), attempt %d/%d. "
                    "Retrying in %.2fs. Error: %s",
                    error_code, attempt + 1, max_attempts, delay, error_message,
                )
                time.sleep(delay)

        # Should not reach here, but just in case
        raise BedrockError(
            f"Bedrock API error after {max_attempts} attempts",
            error_code=last_error.response.get("Error", {}).get("Code", "") if last_error else None,
            retryable=True,
        )

    @staticmethod
    def _extract_response_text(response: dict) -> str:
        """Extract the text content from a Converse API response."""
        try:
            output = response["output"]["message"]["content"]
            text_parts = [block["text"] for block in output if "text" in block]
            return "".join(text_parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise BedrockError(
                f"Unexpected Bedrock response format: {exc}",
                error_code=None,
                retryable=False,
            ) from exc

    @staticmethod
    def _extract_tool_use_json(response: dict) -> str:
        """Extract JSON from a tool-use response block.

        When the model is invoked with a toolConfig, it returns a toolUse
        content block whose ``input`` field contains the structured JSON.
        Falls back to text extraction if no toolUse block is found.
        """
        try:
            content = response["output"]["message"]["content"]
            for block in content:
                if "toolUse" in block:
                    import json as _json
                    return _json.dumps(block["toolUse"]["input"])
            # Fallback: no toolUse block, try text
            text_parts = [block["text"] for block in content if "text" in block]
            return "".join(text_parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise BedrockError(
                f"Unexpected Bedrock tool-use response format: {exc}",
                error_code=None,
                retryable=False,
            ) from exc

    def _adjust_token_usage(self, response: dict, reserved_tokens: int) -> None:
        """Correct rate-limiter accounting with actual token counts from the API.

        The Converse API returns ``usage.inputTokens`` and
        ``usage.outputTokens`` in every response.  We pre-reserved
        ``reserved_tokens`` before the call; now we adjust the difference
        so the budget reflects reality instead of estimates.
        """
        if self._rate_limiter is None:
            return
        usage = response.get("usage")
        if not usage:
            return
        actual = usage.get("inputTokens", 0) + usage.get("outputTokens", 0)
        if actual > 0:
            diff = reserved_tokens - actual
            if diff > 0:
                # We over-reserved — give tokens back
                self._rate_limiter.record_token_usage(-diff)
            elif diff < 0:
                # We under-reserved — charge the extra
                self._rate_limiter.record_token_usage(-diff)
        logger.debug(
            "Token usage: reserved=%d, actual=%d (input=%d, output=%d), adjustment=%+d",
            reserved_tokens,
            actual,
            usage.get("inputTokens", 0),
            usage.get("outputTokens", 0),
            actual - reserved_tokens,
        )

    def invoke_with_schema(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        tool_name: str,
        tool_description: str,
        json_schema: dict,
        model_id: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Call Bedrock Converse API with tool-use for structured JSON output.

        This uses Bedrock's native toolConfig to constrain the model to
        return valid JSON matching the provided schema, eliminating the
        need for fragile JSON extraction from prose responses.

        Args:
            system_prompt: The system-level instructions for the model.
            user_prompt: The user message to send to the model.
            tool_name: Name of the tool (e.g. "generate_review_json").
            tool_description: Description of what the tool produces.
            json_schema: JSON Schema dict describing the expected output.
            model_id: Optional model override for this call.
            max_output_tokens: Optional output-token override for this call.
            temperature: Optional sampling temperature for this call.

        Returns:
            JSON string from the tool-use response.

        Raises:
            BedrockError: On non-retryable errors or after exhausting retries.
        """
        full_system_prompt = (
            f"{system_prompt}\n\n"
            f"## Project Context\n{self._project_context}"
        )
        estimated_input_tokens = (
            _estimate_tokens(full_system_prompt) + _estimate_tokens(user_prompt)
        )
        if estimated_input_tokens > self._config.max_input_tokens:
            raise BedrockError(
                "Bedrock input prompt exceeds max_input_tokens.",
                error_code="InputTooLarge",
                retryable=False,
            )

        output_tokens = max_output_tokens or self._config.max_output_tokens
        messages = [
            {
                "role": "user",
                "content": [{"text": user_prompt}],
            }
        ]

        converse_kwargs: dict[str, Any] = {
            "modelId": model_id or self._config.bedrock_model_id,
            "system": [{"text": full_system_prompt}],
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": output_tokens,
            },
            "toolConfig": {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool_name,
                            "description": tool_description,
                            "inputSchema": {
                                "json": json_schema,
                            },
                        }
                    }
                ],
            },
        }
        if temperature is not None:
            converse_kwargs["inferenceConfig"]["temperature"] = temperature

        max_attempts = self._config.max_retries_bedrock + 1
        last_error: ClientError | None = None
        reserved_tokens = estimated_input_tokens + output_tokens

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
                response = self._client.converse(**converse_kwargs)
                self._adjust_token_usage(response, reserved_tokens)
                return self._extract_tool_use_json(response)

            except ClientError as exc:
                last_error = exc
                error_code = exc.response.get("Error", {}).get("Code", "")
                error_message = exc.response.get("Error", {}).get("Message", str(exc))

                if not _is_retryable_error(exc):
                    raise BedrockError(
                        f"Bedrock API error: {error_message}",
                        error_code=error_code,
                        retryable=False,
                    ) from exc

                retries_left = max_attempts - attempt - 1
                if retries_left == 0:
                    raise BedrockError(
                        f"Bedrock API error after {max_attempts} attempts: {error_message}",
                        error_code=error_code,
                        retryable=True,
                    ) from exc

                delay = _compute_backoff_delay(attempt)
                logger.warning(
                    "Retryable Bedrock error (code=%s), attempt %d/%d. "
                    "Retrying in %.2fs. Error: %s",
                    error_code, attempt + 1, max_attempts, delay, error_message,
                )
                time.sleep(delay)

        raise BedrockError(
            f"Bedrock API error after {max_attempts} attempts",
            error_code=last_error.response.get("Error", {}).get("Code", "") if last_error else None,
            retryable=True,
        )

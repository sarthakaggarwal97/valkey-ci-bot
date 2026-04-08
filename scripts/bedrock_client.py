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


class ToolHandler(Protocol):
    """Callback interface for executing tool calls during multi-turn review."""

    def execute(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call and return the result as a string."""
        ...


class TerminalToolValidator(Protocol):
    """Optional callback for validating terminal tool submissions."""

    def validate_terminal_tool(
        self,
        tool_name: str,
        tool_input: dict,
    ) -> tuple[bool, str]:
        """Return whether the terminal tool submission should end the loop."""
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
    if project.source_dirs:
        parts.append(f"Source directories: {', '.join(project.source_dirs)}")
    if project.test_dirs:
        parts.append(f"Test directories: {', '.join(project.test_dirs)}")
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


def _is_tool_choice_rejected(error: ClientError) -> bool:
    """Return True when Bedrock rejects forced toolChoice support."""
    error_message = error.response.get("Error", {}).get("Message", "")
    normalized_message = error_message.lower().replace(" ", "")
    return "toolchoice" in normalized_message


def _estimate_tokens(text: str) -> int:
    """Estimate token count conservatively from text length."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _summarize_tool_result(text: str, *, max_chars: int = 180) -> str:
    """Render a compact one-line summary of a tool result for logs."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 3] + "..."


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

    def _record_ai_metric(self, name: str, amount: int = 1) -> None:
        """Record a best-effort AI execution metric when the limiter supports it."""
        if self._rate_limiter is None:
            return
        recorder = getattr(self._rate_limiter, "record_ai_metric", None)
        if callable(recorder):
            recorder(name, amount)

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
        self._record_ai_metric("bedrock.invoke.calls")
        estimated_input_tokens = (
            _estimate_tokens(full_system_prompt) + _estimate_tokens(user_prompt)
        )
        if estimated_input_tokens > self._config.max_input_tokens:
            self._record_ai_metric("bedrock.errors.input_too_large")
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

        # Reserve tokens once before the retry loop.  On retryable failures
        # the reservation stays in place (the work *was* attempted); on
        # success, _adjust_token_usage corrects the estimate to actuals.
        if self._rate_limiter is not None:
            if not self._rate_limiter.can_use_tokens(reserved_tokens):
                self._record_ai_metric("bedrock.errors.token_budget_exceeded")
                raise BedrockError(
                    "daily token budget exhausted",
                    error_code="TokenBudgetExceeded",
                    retryable=False,
                )
            self._rate_limiter.record_token_usage(reserved_tokens)

        for attempt in range(max_attempts):
            try:
                response = self._client.converse(**converse_kwargs)
                # Track actual token usage from the API response when available
                self._adjust_token_usage(response, reserved_tokens)
                self._record_ai_metric("bedrock.invoke.success")
                return self._extract_response_text(response)

            except ClientError as exc:
                last_error = exc
                error_code = exc.response.get("Error", {}).get("Code", "")
                error_message = exc.response.get("Error", {}).get("Message", str(exc))

                if not _is_retryable_error(exc):
                    self._record_ai_metric("bedrock.errors.non_retryable")
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
                    self._record_ai_metric("bedrock.errors.retry_exhausted")
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
                self._record_ai_metric("bedrock.retries")
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
            if diff != 0:
                # Positive diff → over-reserved (give back); negative → under-reserved (charge extra).
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
        self._record_ai_metric("bedrock.invoke_schema.calls")
        estimated_input_tokens = (
            _estimate_tokens(full_system_prompt) + _estimate_tokens(user_prompt)
        )
        if estimated_input_tokens > self._config.max_input_tokens:
            self._record_ai_metric("bedrock.errors.input_too_large")
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
                "toolChoice": {
                    "tool": {
                        "name": tool_name,
                    },
                },
            },
        }
        if temperature is not None:
            converse_kwargs["inferenceConfig"]["temperature"] = temperature

        max_attempts = self._config.max_retries_bedrock + 1
        last_error: ClientError | None = None
        reserved_tokens = estimated_input_tokens + output_tokens

        # Reserve tokens once before the retry loop (same as invoke).
        if self._rate_limiter is not None:
            if not self._rate_limiter.can_use_tokens(reserved_tokens):
                self._record_ai_metric("bedrock.errors.token_budget_exceeded")
                raise BedrockError(
                    "daily token budget exhausted",
                    error_code="TokenBudgetExceeded",
                    retryable=False,
                )
            self._rate_limiter.record_token_usage(reserved_tokens)

        for attempt in range(max_attempts):
            try:
                response = self._client.converse(**converse_kwargs)
                self._adjust_token_usage(response, reserved_tokens)
                self._record_ai_metric("bedrock.invoke_schema.success")
                return self._extract_tool_use_json(response)

            except ClientError as exc:
                last_error = exc
                error_code = exc.response.get("Error", {}).get("Code", "")
                error_message = exc.response.get("Error", {}).get("Message", str(exc))

                if _is_tool_choice_rejected(exc):
                    self._record_ai_metric("bedrock.schema_tool_choice_rejected")
                    logger.info(
                        "Structured toolChoice was rejected; retrying schema "
                        "tool-use without forced toolChoice."
                    )
                    fallback_kwargs = {**converse_kwargs}
                    fallback_tool_config = dict(fallback_kwargs["toolConfig"])
                    fallback_tool_config.pop("toolChoice", None)
                    fallback_kwargs["toolConfig"] = fallback_tool_config
                    try:
                        response = self._converse_with_retry(fallback_kwargs)
                        self._adjust_token_usage(response, reserved_tokens)
                        self._record_ai_metric(
                            "bedrock.schema_tool_choice_fallback_success"
                        )
                        self._record_ai_metric("bedrock.invoke_schema.success")
                        return self._extract_tool_use_json(response)
                    except ClientError as fallback_exc:
                        self._record_ai_metric(
                            "bedrock.schema_tool_choice_fallback_error"
                        )
                        last_error = fallback_exc
                        error_code = fallback_exc.response.get("Error", {}).get("Code", "")
                        error_message = fallback_exc.response.get("Error", {}).get(
                            "Message", str(fallback_exc)
                        )
                        if not _is_retryable_error(fallback_exc):
                            self._record_ai_metric("bedrock.errors.non_retryable")
                            raise BedrockError(
                                f"Bedrock API error: {error_message}",
                                error_code=error_code,
                                retryable=False,
                            ) from fallback_exc
                        raise BedrockError(
                            f"Bedrock API error after fallback schema tool-use "
                            f"attempts: {error_message}",
                            error_code=error_code,
                            retryable=True,
                        ) from fallback_exc

                if not _is_retryable_error(exc):
                    self._record_ai_metric("bedrock.errors.non_retryable")
                    raise BedrockError(
                        f"Bedrock API error: {error_message}",
                        error_code=error_code,
                        retryable=False,
                    ) from exc

                retries_left = max_attempts - attempt - 1
                if retries_left == 0:
                    self._record_ai_metric("bedrock.errors.retry_exhausted")
                    raise BedrockError(
                        f"Bedrock API error after {max_attempts} attempts: {error_message}",
                        error_code=error_code,
                        retryable=True,
                    ) from exc

                delay = _compute_backoff_delay(attempt)
                self._record_ai_metric("bedrock.retries")
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

    def _converse_with_retry(self, converse_kwargs: dict[str, Any]) -> dict:
        """Call ``self._client.converse`` with retry on transient errors."""
        max_attempts = self._config.max_retries_bedrock + 1
        for attempt in range(max_attempts):
            try:
                return self._client.converse(**converse_kwargs)
            except ClientError as exc:
                if not _is_retryable_error(exc) or attempt == max_attempts - 1:
                    if _is_retryable_error(exc):
                        self._record_ai_metric("bedrock.errors.retry_exhausted")
                    else:
                        self._record_ai_metric("bedrock.errors.non_retryable")
                    raise
                delay = _compute_backoff_delay(attempt)
                error_code = exc.response.get("Error", {}).get("Code", "")
                self._record_ai_metric("bedrock.retries")
                logger.warning(
                    "Retryable error in converse (code=%s), attempt %d/%d, "
                    "retrying in %.2fs.",
                    error_code, attempt + 1, max_attempts, delay,
                )
                time.sleep(delay)
        # Unreachable, but keeps mypy happy
        raise BedrockError("converse retries exhausted", retryable=True)

    def converse_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        tools: list[dict],
        tool_handler: "ToolHandler",
        terminal_tool: str,
        max_turns: int = 20,
        model_id: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Run a multi-turn Converse loop with tool use.

        The model can call tools (e.g. ``get_file``) to gather context,
        and the loop continues until it calls the *terminal_tool* or
        *max_turns* is reached.  The terminal tool's input JSON is
        returned as the final result.

        Args:
            system_prompt: System-level instructions.
            user_prompt: Initial user message.
            tools: List of Bedrock toolSpec dicts.
            tool_handler: Callback that executes non-terminal tool calls.
            terminal_tool: Name of the tool whose invocation ends the loop.
            max_turns: Maximum number of tool-use round-trips.
            model_id: Optional model override.
            max_output_tokens: Optional output-token override.
            temperature: Optional sampling temperature.

        Returns:
            JSON string from the terminal tool invocation.
        """
        import json as _json

        full_system_prompt = (
            f"{system_prompt}\n\n"
            f"## Project Context\n{self._project_context}"
        )
        self._record_ai_metric("bedrock.tool_loop.calls")
        output_tokens = max_output_tokens or self._config.max_output_tokens
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [{"text": user_prompt}]},
        ]

        converse_kwargs: dict[str, Any] = {
            "modelId": model_id or self._config.bedrock_model_id,
            "system": [{"text": full_system_prompt}],
            "inferenceConfig": {"maxTokens": output_tokens},
            "toolConfig": {"tools": tools},
        }
        if temperature is not None:
            converse_kwargs["inferenceConfig"]["temperature"] = temperature

        for turn in range(max_turns):
            converse_kwargs["messages"] = messages

            # Budget check per turn
            estimated = _estimate_tokens(
                full_system_prompt
            ) + sum(
                _estimate_tokens(str(m)) for m in messages
            ) + output_tokens
            if self._rate_limiter is not None:
                if not self._rate_limiter.can_use_tokens(estimated):
                    self._record_ai_metric("bedrock.errors.token_budget_exceeded")
                    raise BedrockError(
                        "daily token budget exhausted during tool-use loop",
                        error_code="TokenBudgetExceeded",
                        retryable=False,
                    )
                self._rate_limiter.record_token_usage(estimated)

            turn_started = time.monotonic()
            response = self._converse_with_retry(converse_kwargs)
            self._record_ai_metric("bedrock.tool_loop.turns")
            turn_elapsed = time.monotonic() - turn_started
            self._adjust_token_usage(response, estimated)

            assistant_content = response["output"]["message"]["content"]
            messages.append({"role": "assistant", "content": assistant_content})

            # Collect all tool_use blocks from the response
            tool_use_blocks = [
                block for block in assistant_content if "toolUse" in block
            ]
            logger.info(
                "Tool-use turn %d completed in %.2fs with %d tool call(s).",
                turn + 1,
                turn_elapsed,
                len(tool_use_blocks),
            )

            if not tool_use_blocks:
                self._record_ai_metric("bedrock.tool_loop.text_without_tool")
                text_parts = [
                    block["text"] for block in assistant_content if "text" in block
                ]
                assistant_text = "".join(text_parts).strip()
                logger.info(
                    "Model returned text without tool call on turn %d.",
                    turn + 1,
                )
                reminder = (
                    f"You must continue using tools or call {terminal_tool} with valid JSON."
                )
                if assistant_text:
                    reminder += (
                        "\n\nYour last text response was:\n"
                        f"{assistant_text[:2000]}"
                    )
                messages.append({
                    "role": "user",
                    "content": [{"text": reminder}],
                })
                continue

            # Process each tool call
            tool_results: list[dict] = []
            terminal_result: str | None = None

            for block in tool_use_blocks:
                tool_use = block["toolUse"]
                name = tool_use["name"]
                tool_use_id = tool_use["toolUseId"]
                tool_input = tool_use.get("input", {})

                if name == terminal_tool:
                    accepted = True
                    result_text = "Review submitted."
                    validator = getattr(tool_handler, "validate_terminal_tool", None)
                    if callable(validator):
                        try:
                            validation = validator(name, tool_input)
                            if (
                                isinstance(validation, tuple)
                                and len(validation) == 2
                            ):
                                accepted = bool(validation[0])
                                result_text = str(validation[1] or result_text)
                        except Exception as exc:
                            accepted = False
                            result_text = f"Terminal tool validation failed: {exc}"
                            self._record_ai_metric(
                                "bedrock.tool_loop.terminal_validation_errors"
                            )
                            logger.warning(
                                "Terminal tool %s validation failed on turn %d: %s",
                                terminal_tool,
                                turn + 1,
                                exc,
                            )
                    if accepted:
                        terminal_result = _json.dumps(tool_input)
                    else:
                        self._record_ai_metric(
                            "bedrock.tool_loop.terminal_validation_rejections"
                        )
                        logger.info(
                            "Terminal tool %s rejected on turn %d: %s",
                            terminal_tool,
                            turn + 1,
                            _summarize_tool_result(result_text),
                        )
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"text": result_text}],
                        }
                    })
                else:
                    logger.info(
                        "Tool call turn %d: %s(%s)",
                        turn + 1, name,
                        _json.dumps(tool_input)[:200],
                    )
                    try:
                        result_text = tool_handler.execute(name, tool_input)
                    except Exception as exc:
                        result_text = f"Error: {exc}"
                        self._record_ai_metric("bedrock.tool_loop.tool_errors")
                        logger.warning("Tool %s failed: %s", name, exc)
                    logger.info(
                        "Tool result turn %d: %s -> %s",
                        turn + 1,
                        name,
                        _summarize_tool_result(result_text),
                    )
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"text": result_text}],
                        }
                    })

            if terminal_result is not None:
                logger.info(
                    "Terminal tool %s called on turn %d.", terminal_tool, turn + 1,
                )
                self._record_ai_metric("bedrock.tool_loop.success")
                return terminal_result

            # Feed tool results back for the next turn
            messages.append({"role": "user", "content": tool_results})

        logger.warning(
            "Tool-use loop exhausted %d turns without terminal tool. "
            "Forcing final submission.", max_turns,
        )
        self._record_ai_metric("bedrock.tool_loop.forced_submissions")

        # Every prior toolUse already received a matching toolResult in the
        # loop above. Sending more toolResult blocks here corrupts the Bedrock
        # message history, so force the final turn with a plain user reminder.
        messages.append({
            "role": "user",
            "content": [{
                "text": (
                    f"Turn limit reached. You MUST call {terminal_tool} now with "
                    "whatever findings you have so far. Do not answer in plain text."
                ),
            }],
        })

        # Only offer the terminal tool so the model is forced to use it
        forced_kwargs = {**converse_kwargs}
        forced_kwargs["messages"] = messages
        forced_tools = [
            t for t in tools
            if t.get("toolSpec", {}).get("name") == terminal_tool
        ]
        forced_kwargs["toolConfig"] = {
            "tools": forced_tools,
        }
        if forced_tools:
            forced_kwargs["toolConfig"]["toolChoice"] = {
                "tool": {
                    "name": terminal_tool,
                },
            }

        try:
            estimated = _estimate_tokens(full_system_prompt) + sum(
                _estimate_tokens(str(m)) for m in messages
            ) + output_tokens
            if self._rate_limiter is not None:
                if not self._rate_limiter.can_use_tokens(estimated):
                    self._record_ai_metric("bedrock.errors.token_budget_exceeded")
                    raise BedrockError(
                        "daily token budget exhausted during forced submission",
                        error_code="TokenBudgetExceeded",
                        retryable=False,
                    )
                self._rate_limiter.record_token_usage(estimated)

            try:
                response = self._converse_with_retry(forced_kwargs)
            except ClientError as exc:
                if not _is_tool_choice_rejected(exc):
                    raise
                self._record_ai_metric("bedrock.forced_terminal_tool_choice_rejected")
                logger.info(
                    "Forced terminal toolChoice was rejected; retrying forced "
                    "submission with only the terminal tool exposed."
                )
                fallback_forced_kwargs = {**forced_kwargs}
                fallback_forced_kwargs["toolConfig"] = {
                    "tools": forced_tools,
                }
                response = self._converse_with_retry(fallback_forced_kwargs)
            self._adjust_token_usage(response, estimated)
            assistant_content = response["output"]["message"]["content"]

            for block in assistant_content:
                if "toolUse" in block and block["toolUse"]["name"] == terminal_tool:
                    tool_input = block["toolUse"].get("input", {})
                    validator = getattr(tool_handler, "validate_terminal_tool", None)
                    if callable(validator):
                        validation = validator(terminal_tool, tool_input)
                        if (
                            isinstance(validation, tuple)
                            and len(validation) == 2
                            and not bool(validation[0])
                        ):
                            reason = str(validation[1] or "terminal tool rejected")
                            self._record_ai_metric(
                                "bedrock.tool_loop.terminal_validation_rejections"
                            )
                            raise BedrockError(
                                f"Forced terminal tool rejected: {reason}",
                                error_code="TerminalToolRejected",
                                retryable=False,
                            )
                    logger.info(
                        "Terminal tool %s called on forced turn.", terminal_tool,
                    )
                    self._record_ai_metric("bedrock.tool_loop.success")
                    return _json.dumps(tool_input)

            # If still no terminal tool, extract any text
            text_parts = [
                block["text"] for block in assistant_content if "text" in block
            ]
            if text_parts:
                self._record_ai_metric("bedrock.tool_loop.forced_plain_text_returns")
                return "".join(text_parts)
        except Exception as exc:
            self._record_ai_metric("bedrock.tool_loop.forced_submission_errors")
            logger.warning("Forced submission failed: %s", exc)

        self._record_ai_metric("bedrock.tool_loop.exhausted")
        raise BedrockError(
            f"Tool-use loop did not complete within {max_turns} turns.",
            error_code="ToolUseLoopExhausted",
            retryable=False,
        )

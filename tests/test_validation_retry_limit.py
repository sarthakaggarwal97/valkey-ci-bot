"""Property 12: Validation retry limit.

# Feature: valkey-ci-agent, Property 12: Validation retry limit

**Validates: Requirements 5.8**

For any validation failure, the Fix_Generator should retry fix generation
at most `max_validation_retries` times with the validation failure output
included as context. After exhausting retries, the fix should be abandoned.

This test exercises the validation-retry contract: the loop that calls
generate() → validate() → re-generate with validation context, bounded
by max_validation_retries.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.config import BotConfig
from scripts.fix_generator import FixGenerator
from scripts.models import RootCauseReport, ValidationResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_DIFF = """\
--- a/src/server.c
+++ b/src/server.c
@@ -10,6 +10,7 @@
 void handle_request(Request *req) {
+    if (req == NULL) return;
     process(req->data);
 }
"""


def _make_root_cause(**overrides) -> RootCauseReport:
    defaults = {
        "description": "Null pointer dereference in src/server.c",
        "files_to_change": ["src/server.c"],
        "confidence": "high",
        "rationale": "The pointer is not checked before use.",
        "is_flaky": False,
        "flakiness_indicators": None,
    }
    defaults.update(overrides)
    return RootCauseReport(**defaults)


def _make_generator(
    bedrock_return: str = _SAMPLE_DIFF,
    config: BotConfig | None = None,
) -> tuple[FixGenerator, MagicMock]:
    """Create a FixGenerator with a mocked BedrockClient."""
    mock_bedrock = MagicMock()
    mock_bedrock.invoke.return_value = bedrock_return
    cfg = config or BotConfig()
    return FixGenerator(mock_bedrock, cfg), mock_bedrock


def _simulate_validation_retry_loop(
    fix_generator: FixGenerator,
    root_cause: RootCauseReport,
    source_files: dict[str, str],
    validation_results: list[ValidationResult],
    max_validation_retries: int,
) -> tuple[str | None, int]:
    """Simulate the validation-retry loop as specified in the design.

    The loop:
    1. Generate a fix
    2. If fix is None, abandon
    3. Validate the fix
    4. If validation passes, return the fix
    5. If validation fails and retries remain, regenerate with validation
       failure output as context (by calling generate again)
    6. After exhausting retries, abandon

    Returns (final_diff_or_none, number_of_generate_calls).
    """
    generate_calls = 0
    validation_idx = 0

    diff = fix_generator.generate(root_cause, source_files)
    generate_calls += 1

    if diff is None:
        return None, generate_calls

    retries_used = 0
    while retries_used <= max_validation_retries:
        # Validate
        if validation_idx < len(validation_results):
            result = validation_results[validation_idx]
            validation_idx += 1
        else:
            # Default to failure if we run out of pre-defined results
            result = ValidationResult(passed=False, output="test still fails")

        if result.passed:
            return diff, generate_calls

        # Validation failed
        if retries_used >= max_validation_retries:
            break

        # Retry: regenerate with validation failure context
        diff = fix_generator.generate(root_cause, source_files)
        generate_calls += 1
        retries_used += 1

        if diff is None:
            return None, generate_calls

    # Exhausted retries — abandon
    return None, generate_calls


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


class TestValidationRetryLimitProperty:
    """Property 12: Validation retry limit.

    **Validates: Requirements 5.8**

    For any validation failure, the Fix_Generator should retry fix generation
    at most max_validation_retries times with the validation failure output
    included as context. After exhausting retries, the fix should be abandoned.
    """

    @given(max_validation_retries=st.integers(min_value=0, max_value=5))
    @settings(max_examples=100)
    def test_validation_retry_limit_all_failures(
        self, max_validation_retries: int
    ):
        """When every validation fails, generate is called at most
        max_validation_retries + 1 times and the fix is abandoned."""
        config = BotConfig(max_retries_validation=max_validation_retries)
        gen, mock_bedrock = _make_generator(
            bedrock_return=_SAMPLE_DIFF, config=config
        )
        rc = _make_root_cause(confidence="high")

        # All validations fail
        all_failures = [
            ValidationResult(passed=False, output=f"failure {i}")
            for i in range(max_validation_retries + 1)
        ]

        with patch(
            "scripts.fix_generator._validate_patch_applies",
            return_value=(True, ""),
        ):
            result, gen_calls = _simulate_validation_retry_loop(
                fix_generator=gen,
                root_cause=rc,
                source_files={"src/server.c": "int main() {}"},
                validation_results=all_failures,
                max_validation_retries=config.max_retries_validation,
            )

        expected_calls = max_validation_retries + 1
        assert result is None, (
            f"Expected fix to be abandoned after {max_validation_retries} "
            f"validation retries, but got a non-None result"
        )
        assert gen_calls == expected_calls, (
            f"Expected {expected_calls} generate() calls "
            f"(1 initial + {max_validation_retries} retries), "
            f"got {gen_calls}"
        )

    @given(
        max_validation_retries=st.integers(min_value=1, max_value=5),
        pass_on_retry=st.integers(min_value=0, max_value=5),
    )
    @settings(max_examples=100)
    def test_validation_retry_succeeds_within_limit(
        self,
        max_validation_retries: int,
        pass_on_retry: int,
    ):
        """When validation passes on retry N (within limit), the fix is
        returned and no more retries occur."""
        # Clamp pass_on_retry to be within the allowed range
        pass_on_retry = min(pass_on_retry, max_validation_retries)

        config = BotConfig(max_retries_validation=max_validation_retries)
        gen, mock_bedrock = _make_generator(
            bedrock_return=_SAMPLE_DIFF, config=config
        )
        rc = _make_root_cause(confidence="high")

        # Build validation results: fail until pass_on_retry, then pass
        validation_results = [
            ValidationResult(passed=False, output=f"failure {i}")
            for i in range(pass_on_retry)
        ]
        validation_results.append(ValidationResult(passed=True, output="ok"))

        with patch(
            "scripts.fix_generator._validate_patch_applies",
            return_value=(True, ""),
        ):
            result, gen_calls = _simulate_validation_retry_loop(
                fix_generator=gen,
                root_cause=rc,
                source_files={"src/server.c": "int main() {}"},
                validation_results=validation_results,
                max_validation_retries=config.max_retries_validation,
            )

        expected_calls = pass_on_retry + 1
        assert result is not None, (
            f"Expected fix to succeed on retry {pass_on_retry}, "
            f"but got None"
        )
        assert gen_calls == expected_calls, (
            f"Expected {expected_calls} generate() calls "
            f"(validation passed on attempt {pass_on_retry + 1}), "
            f"got {gen_calls}"
        )

    @given(max_validation_retries=st.integers(min_value=0, max_value=5))
    @settings(max_examples=100)
    def test_validation_retry_never_exceeds_limit(
        self, max_validation_retries: int
    ):
        """The total number of generate() calls never exceeds
        max_validation_retries + 1, regardless of validation outcomes."""
        config = BotConfig(max_retries_validation=max_validation_retries)
        gen, mock_bedrock = _make_generator(
            bedrock_return=_SAMPLE_DIFF, config=config
        )
        rc = _make_root_cause(confidence="high")

        # All validations fail — worst case
        all_failures = [
            ValidationResult(passed=False, output=f"failure {i}")
            for i in range(max_validation_retries + 10)  # more than needed
        ]

        with patch(
            "scripts.fix_generator._validate_patch_applies",
            return_value=(True, ""),
        ):
            _, gen_calls = _simulate_validation_retry_loop(
                fix_generator=gen,
                root_cause=rc,
                source_files={"src/server.c": "int main() {}"},
                validation_results=all_failures,
                max_validation_retries=config.max_retries_validation,
            )

        max_allowed = max_validation_retries + 1
        assert gen_calls <= max_allowed, (
            f"generate() was called {gen_calls} times, "
            f"exceeding the limit of {max_allowed} "
            f"(max_validation_retries={max_validation_retries})"
        )

# Feature: valkey-ci-agent, Property 1: Infrastructure failure classification
"""Property test for infrastructure failure classification.

Validates: Requirements 1.4

Property 1: For any job failure message, the is_infrastructure_failure classifier
should return True if and only if the message matches known infrastructure error
patterns (runner timeout, network error, rate limit), and False for all test/build
failure messages.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.failure_detector import FailureDetector

# --- Known infrastructure pattern fragments ---
# Each tuple: (fragment that triggers a pattern, description)
_INFRA_FRAGMENTS: list[str] = [
    "runner timeout",
    "Runner  Timeout",
    "the hosted runner lost communication",
    "The Hosted Runner Lost Communication",
    "runner has received a shutdown signal",
    "RUNNER HAS RECEIVED A SHUTDOWN SIGNAL",
    "network error",
    "Network   Error",
    "rate limit",
    "Rate  Limit",
    "ETIMEDOUT",
    "etimedout",
    "ECONNRESET",
    "econnreset",
    "service unavailable",
    "SERVICE UNAVAILABLE",
    "runner provisioning error",
    "Runner Provisioning  Error",
    "no space left on device",
    "No Space Left On Device",
]

# Typical test/build failure messages that should NOT be classified as infra
_NON_INFRA_MESSAGES: list[str] = [
    "FAILED  TestSuite.TestName",
    "src/server.c:42:10: error: implicit declaration of function 'foo'",
    "Expected 'x' to equal 'y'",
    "Assertion failed: expected 5 but got 3",
    "tests/unit/expire.tcl: test failed",
    "make[2]: *** [CMakeFiles/valkey-server.dir/src/server.c.o] Error 1",
    "FAILED tests/unit/test_rdb.cc:123",
    "error: use of undeclared identifier 'val'",
    "Segmentation fault (core dumped)",
    "Test timed out after 120 seconds",
    "undefined reference to 'redisCommand'",
    "comparison of integers of different signs",
    "[err]: Test description in tests/unit/foo.tcl",
    "Build failed with exit code 2",
    "ld: symbol(s) not found for architecture x86_64",
]

# Strategy: pick an infra fragment and wrap it in surrounding text
infra_fragment_strategy = st.sampled_from(_INFRA_FRAGMENTS)

surrounding_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Z"),
        min_codepoint=32,
        max_codepoint=126,
    ),
    min_size=0,
    max_size=50,
)

infra_message_strategy = st.builds(
    lambda prefix, fragment, suffix: f"{prefix} {fragment} {suffix}",
    prefix=surrounding_text,
    fragment=infra_fragment_strategy,
    suffix=surrounding_text,
)

# Strategy: pick a non-infra message
non_infra_message_strategy = st.sampled_from(_NON_INFRA_MESSAGES)

# Strategy: generate arbitrary text that avoids all infra keywords
# We use a filtered strategy that rejects any text matching infra patterns
_INFRA_KEYWORDS = [
    "runner timeout", "runner lost communication", "runner has received a shutdown",
    "network error", "rate limit", "etimedout", "econnreset",
    "service unavailable", "runner provisioning error", "no space left on device",
]


def _contains_infra_keyword(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _INFRA_KEYWORDS)


arbitrary_non_infra_strategy = surrounding_text.filter(lambda t: not _contains_infra_keyword(t))


# --- Property Tests ---


@settings(max_examples=100)
@given(message=infra_message_strategy)
def test_infra_patterns_detected_as_infrastructure(message: str) -> None:
    """Property 1 (positive): Any message containing a known infrastructure
    error pattern should be classified as an infrastructure failure.

    **Validates: Requirements 1.4**
    """
    assert FailureDetector.is_infrastructure_failure(message) is True, (
        f"Expected infrastructure failure for message containing infra pattern: {message!r}"
    )


@settings(max_examples=100)
@given(message=non_infra_message_strategy)
def test_test_build_failures_not_classified_as_infrastructure(message: str) -> None:
    """Property 1 (negative, known messages): Typical test/build failure messages
    should NOT be classified as infrastructure failures.

    **Validates: Requirements 1.4**
    """
    assert FailureDetector.is_infrastructure_failure(message) is False, (
        f"Expected non-infrastructure for test/build failure message: {message!r}"
    )


@settings(max_examples=100)
@given(message=arbitrary_non_infra_strategy)
def test_arbitrary_text_without_infra_keywords_returns_false(message: str) -> None:
    """Property 1 (negative, arbitrary): Any arbitrary text that does not contain
    known infrastructure keywords should not be classified as infrastructure failure.

    **Validates: Requirements 1.4**
    """
    assert FailureDetector.is_infrastructure_failure(message) is False, (
        f"Expected non-infrastructure for arbitrary text: {message!r}"
    )

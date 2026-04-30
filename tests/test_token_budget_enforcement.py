# Feature: valkey-ci-agent, Property 24: Token budget enforcement
"""Property tests for token budget enforcement.

Property 24: For any sequence of Bedrock API calls, the cumulative token usage
should be tracked. When the daily token budget is exhausted, no further Bedrock
calls should be made.

THE Bot SHALL track cumulative Bedrock API token usage per 24-hour period and
stop processing new failures when a configurable token budget is exhausted.

**Validates: Requirements 10.4**
"""

from __future__ import annotations

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from scripts.config import BotConfig
from scripts.rate_limiter import RateLimiter

# --- Strategies ---

# Daily token budget: at least 1 to be meaningful
budget_st = st.integers(min_value=1, max_value=10_000_000)

# Individual token usage amounts (non-negative)
token_amount_st = st.integers(min_value=0, max_value=1_000_000)

# Sequence of token usage recordings
usage_sequence_st = st.lists(
    st.integers(min_value=1, max_value=500_000),
    min_size=1,
    max_size=30,
)


# --- Property Tests ---


@settings(max_examples=100)
@given(budget=budget_st, usages=usage_sequence_st)
def test_cumulative_usage_never_exceeds_budget_when_checked(
    budget: int,
    usages: list[int],
) -> None:
    """When can_use_tokens() is checked before each recording, cumulative usage
    never exceeds the budget.

    For any daily_token_budget and sequence of token usage recordings, if we
    only record usage when can_use_tokens() returns True, the cumulative usage
    should never exceed the budget.

    **Validates: Requirements 10.4**
    """
    config = BotConfig(daily_token_budget=budget)
    limiter = RateLimiter(config)

    for amount in usages:
        if limiter.can_use_tokens(amount):
            limiter.record_token_usage(amount)

    assert limiter.get_token_usage() <= budget


@settings(max_examples=100)
@given(budget=budget_st, usages=usage_sequence_st)
def test_can_use_tokens_false_when_budget_would_be_exceeded(
    budget: int,
    usages: list[int],
) -> None:
    """can_use_tokens() returns False when the requested amount would push
    cumulative usage above the budget.

    After recording some usage, requesting an amount that would exceed the
    budget must be denied.

    **Validates: Requirements 10.4**
    """
    config = BotConfig(daily_token_budget=budget)
    limiter = RateLimiter(config)

    # Record usage respecting the budget
    for amount in usages:
        if limiter.can_use_tokens(amount):
            limiter.record_token_usage(amount)

    current = limiter.get_token_usage()
    remaining = budget - current

    # Any request larger than remaining capacity must be denied
    if remaining < budget:  # some tokens were used
        over_amount = remaining + 1
        assert limiter.can_use_tokens(over_amount) is False


@settings(max_examples=100)
@given(budget=budget_st, usages=usage_sequence_st)
def test_can_use_tokens_true_when_under_budget(
    budget: int,
    usages: list[int],
) -> None:
    """can_use_tokens() returns True when the requested amount fits within
    the remaining budget.

    After recording some usage, requesting an amount that fits within the
    remaining budget must be allowed.

    **Validates: Requirements 10.4**
    """
    config = BotConfig(daily_token_budget=budget)
    limiter = RateLimiter(config)

    # Record usage respecting the budget
    for amount in usages:
        if limiter.can_use_tokens(amount):
            limiter.record_token_usage(amount)

    current = limiter.get_token_usage()
    remaining = budget - current

    # Any request that fits within remaining capacity must be allowed
    if remaining > 0:
        assert limiter.can_use_tokens(remaining) is True
        # Also check a smaller amount
        assert limiter.can_use_tokens(1) is True

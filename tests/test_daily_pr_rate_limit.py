# Feature: valkey-ci-agent, Property 22: Daily PR rate limit
"""Property tests for daily PR rate limit.

Property 22: For any sequence of PR creation attempts within a 24-hour window,
the total number of created PRs should not exceed max_prs_per_day. Excess
failures should be queued.

**Validates: Requirements 10.1**
"""

from __future__ import annotations

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from scripts.config import BotConfig
from scripts.rate_limiter import RateLimiter

# --- Strategies ---

max_prs_st = st.integers(min_value=1, max_value=20)

# Number of PR creation attempts (may exceed the limit)
num_attempts_st = st.integers(min_value=0, max_value=40)

# Fingerprints for queued failures
fingerprint_st = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122),
    min_size=8,
    max_size=20,
)


# --- Property Tests ---


@settings(max_examples=100)
@given(max_prs=max_prs_st, num_attempts=num_attempts_st)
def test_created_prs_never_exceed_daily_limit(
    max_prs: int,
    num_attempts: int,
) -> None:
    """After any number of attempts, the count of created PRs never exceeds max_prs_per_day.

    For each attempt, we check can_create_pr() and only record if allowed.
    The total recorded PRs must never exceed the configured limit.

    **Validates: Requirements 10.1**
    """
    config = BotConfig(max_prs_per_day=max_prs)
    limiter = RateLimiter(config)

    created = 0
    for _ in range(num_attempts):
        if limiter.can_create_pr():
            limiter.record_pr_created()
            created += 1

    assert created <= max_prs


@settings(max_examples=100)
@given(max_prs=max_prs_st)
def test_can_create_pr_true_before_limit_reached(
    max_prs: int,
) -> None:
    """Before creating max_prs_per_day PRs, can_create_pr() returns True.

    For each PR created up to N-1, the next call to can_create_pr() should
    still return True.

    **Validates: Requirements 10.1**
    """
    config = BotConfig(max_prs_per_day=max_prs)
    limiter = RateLimiter(config)

    for i in range(max_prs):
        assert limiter.can_create_pr() is True, (
            f"can_create_pr() returned False after only {i} PRs "
            f"(limit is {max_prs})"
        )
        limiter.record_pr_created()


@settings(max_examples=100)
@given(max_prs=max_prs_st)
def test_can_create_pr_false_at_limit(
    max_prs: int,
) -> None:
    """After creating exactly max_prs_per_day PRs, can_create_pr() returns False.

    **Validates: Requirements 10.1**
    """
    config = BotConfig(max_prs_per_day=max_prs)
    limiter = RateLimiter(config)

    for _ in range(max_prs):
        limiter.record_pr_created()

    assert limiter.can_create_pr() is False, (
        f"can_create_pr() returned True after creating {max_prs} PRs "
        f"(limit is {max_prs})"
    )


@settings(max_examples=100)
@given(
    max_prs=max_prs_st,
    num_attempts=num_attempts_st,
    fingerprints=st.lists(fingerprint_st, min_size=1, max_size=40),
)
def test_excess_failures_are_queued(
    max_prs: int,
    num_attempts: int,
    fingerprints: list[str],
) -> None:
    """When the daily limit is reached, excess failures are queued.

    Simulates a sequence of PR creation attempts. When can_create_pr()
    returns False, the failure fingerprint is queued. The number of
    created PRs plus the number of queued failures should equal the
    total number of attempts (up to the fingerprints available).

    **Validates: Requirements 10.1**
    """
    config = BotConfig(max_prs_per_day=max_prs)
    limiter = RateLimiter(config)

    total = min(num_attempts, len(fingerprints))
    created = 0
    queued = 0

    for i in range(total):
        if limiter.can_create_pr():
            limiter.record_pr_created()
            created += 1
        else:
            limiter.queue_failure(fingerprints[i])
            queued += 1

    assert created <= max_prs
    # Every attempt either created a PR or queued the failure
    assert created + queued == total

    # If we attempted more than the limit, some must be queued
    if total > max_prs:
        assert queued > 0
        assert len(limiter.get_queued_failures()) > 0

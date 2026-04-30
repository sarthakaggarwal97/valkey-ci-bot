# Feature: valkey-ci-agent, Property 26: Queued failures are drained by reconciliation runs
"""Property tests for queued failure reconciliation drain.

Property 26: For any queued failure created because of a 24-hour PR limit
or open-PR cap, a later scheduled reconciliation run after the limit resets
should re-enqueue that failure for normal processing even if no new CI
failure occurs.

THE Bot SHALL include a scheduled reconciliation run that drains queued
failures after rate limits reset, even if no new CI failure occurs.

**Validates: Requirements 10.1, 10.6**
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from scripts.config import BotConfig
from scripts.rate_limiter import RateLimiter

# --- Strategies ---

max_prs_st = st.integers(min_value=1, max_value=10)

fingerprint_st = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122),
    min_size=8,
    max_size=24,
)

queued_fingerprints_st = st.lists(
    fingerprint_st,
    min_size=1,
    max_size=15,
    unique=True,
)


# --- Helpers ---

def _simulate_reconciliation_drain(
    limiter: RateLimiter,
) -> tuple[list[str], list[str]]:
    """Simulate a reconciliation run that drains queued failures.

    Iterates over queued failures and processes each one where the rate
    limiter allows PR creation. Processed failures are dequeued.

    Returns (processed, remaining) fingerprint lists.
    """
    processed: list[str] = []
    # Snapshot the queue before draining to avoid mutation during iteration
    queued = limiter.get_queued_failures()
    for fp in queued:
        if limiter.can_create_pr():
            limiter.record_pr_created()
            limiter.dequeue_failure(fp)
            processed.append(fp)
        else:
            break  # Rate limit hit, stop draining
    remaining = limiter.get_queued_failures()
    return processed, remaining


# --- Property Tests ---


class TestQueuedFailureDrainProperty:
    """Property 26: Queued failures are drained by reconciliation runs.

    **Validates: Requirements 10.1, 10.6**
    """

    @given(
        max_prs=max_prs_st,
        fingerprints=queued_fingerprints_st,
    )
    @settings(max_examples=100)
    def test_queued_failures_drain_after_rate_limit_resets(
        self,
        max_prs: int,
        fingerprints: list[str],
    ) -> None:
        """When rate limits reset (new 24h window), reconciliation drains
        queued failures up to the new daily limit.

        1. Fill the daily PR limit to force queuing
        2. Queue failures
        3. Simulate 24h passing (rate limit reset)
        4. Run reconciliation — queued failures should be drained

        **Validates: Requirements 10.1, 10.6**
        """
        config = BotConfig(max_prs_per_day=max_prs)
        limiter = RateLimiter(config)

        # Exhaust the daily PR limit
        for _ in range(max_prs):
            limiter.record_pr_created()
        assert limiter.can_create_pr() is False

        # Queue failures
        for fp in fingerprints:
            limiter.queue_failure(fp)
        assert len(limiter.get_queued_failures()) == len(fingerprints)

        # Simulate 24h passing — move all PR timestamps to >24h ago
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        limiter._pr_timestamps = [old_time] * max_prs

        # Rate limit should now be reset
        assert limiter.can_create_pr() is True

        # Run reconciliation drain
        processed, remaining = _simulate_reconciliation_drain(limiter)

        # Should drain up to max_prs_per_day from the queue
        expected_drained = min(len(fingerprints), max_prs)
        assert len(processed) == expected_drained
        assert len(remaining) == len(fingerprints) - expected_drained

        # Processed fingerprints should no longer be in the queue
        for fp in processed:
            assert fp not in limiter.get_queued_failures()

    @given(
        max_prs=max_prs_st,
        fingerprints=queued_fingerprints_st,
    )
    @settings(max_examples=100)
    def test_queued_failures_remain_when_rate_limit_active(
        self,
        max_prs: int,
        fingerprints: list[str],
    ) -> None:
        """If rate limits are still active, queued failures remain in the queue.

        **Validates: Requirements 10.1, 10.6**
        """
        config = BotConfig(max_prs_per_day=max_prs)
        limiter = RateLimiter(config)

        # Exhaust the daily PR limit (timestamps are fresh — within 24h)
        for _ in range(max_prs):
            limiter.record_pr_created()
        assert limiter.can_create_pr() is False

        # Queue failures
        for fp in fingerprints:
            limiter.queue_failure(fp)

        # Run reconciliation WITHOUT resetting the rate limit
        processed, remaining = _simulate_reconciliation_drain(limiter)

        # Nothing should be drained
        assert len(processed) == 0
        assert len(remaining) == len(fingerprints)

        # All fingerprints should still be queued
        for fp in fingerprints:
            assert fp in limiter.get_queued_failures()

    @given(
        max_prs=max_prs_st,
        fingerprints=queued_fingerprints_st,
    )
    @settings(max_examples=100)
    def test_dequeued_failures_are_removed_as_processed(
        self,
        max_prs: int,
        fingerprints: list[str],
    ) -> None:
        """Queued failures are dequeued one-by-one as they are processed.

        After each successful processing, the fingerprint is removed from
        the queue. The queue shrinks monotonically during reconciliation.

        **Validates: Requirements 10.1, 10.6**
        """
        config = BotConfig(max_prs_per_day=max_prs)
        limiter = RateLimiter(config)

        # Queue failures
        for fp in fingerprints:
            limiter.queue_failure(fp)

        # Process one at a time and verify queue shrinks
        initial_size = len(limiter.get_queued_failures())
        queued_snapshot = limiter.get_queued_failures()
        processed_count = 0

        for fp in queued_snapshot:
            if limiter.can_create_pr():
                limiter.record_pr_created()
                limiter.dequeue_failure(fp)
                processed_count += 1
                current_size = len(limiter.get_queued_failures())
                assert current_size == initial_size - processed_count
            else:
                break

        # Final queue size is consistent
        assert len(limiter.get_queued_failures()) == initial_size - processed_count

    @given(
        max_prs=max_prs_st,
        first_batch=queued_fingerprints_st,
        second_batch=queued_fingerprints_st,
    )
    @settings(max_examples=100)
    def test_reconciliation_drains_without_new_ci_failure(
        self,
        max_prs: int,
        first_batch: list[str],
        second_batch: list[str],
    ) -> None:
        """Reconciliation drains queued failures even if no new CI failure
        occurs between runs. Simulates two reconciliation cycles.

        **Validates: Requirements 10.6**
        """
        # Ensure batches don't overlap
        second_batch = [
            fp for fp in second_batch if fp not in first_batch
        ]
        assume(len(second_batch) > 0)

        config = BotConfig(max_prs_per_day=max_prs)
        limiter = RateLimiter(config)

        # Queue first batch and exhaust limit
        all_fps = first_batch + second_batch
        for fp in all_fps:
            limiter.queue_failure(fp)

        total_queued = len(all_fps)

        # First reconciliation cycle (fresh limiter, no PRs yet)
        processed_1, remaining_1 = _simulate_reconciliation_drain(limiter)
        drained_1 = len(processed_1)
        assert drained_1 == min(total_queued, max_prs)

        # If there are remaining items, simulate time passing and drain again
        if remaining_1:
            old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
            limiter._pr_timestamps = [old_time] * len(limiter._pr_timestamps)

            processed_2, remaining_2 = _simulate_reconciliation_drain(limiter)
            drained_2 = len(processed_2)
            assert drained_2 == min(len(remaining_1), max_prs)

            # Total drained across both cycles
            total_drained = drained_1 + drained_2
            assert total_drained <= total_queued

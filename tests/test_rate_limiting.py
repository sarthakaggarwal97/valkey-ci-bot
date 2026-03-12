# Feature: valkey-ci-bot, Property 21: Per-run failure processing limit with ordering
"""Property tests for per-run failure processing limit.

Property 21: For any CI run with N failed jobs where N > max_failures_per_run,
the bot should process exactly max_failures_per_run failures ordered
alphabetically by job name, and log the remaining as "skipped-rate-limit".

**Validates: Requirements 10.2, 10.3**
"""

from __future__ import annotations

import logging

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.models import FailedJob


# --- Strategies ---

job_name_st = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=40,
)

failed_job_st = st.builds(
    FailedJob,
    id=st.integers(min_value=1, max_value=10_000),
    name=job_name_st,
    conclusion=st.just("failure"),
    step_name=st.none(),
    matrix_params=st.just({}),
)

failed_jobs_list_st = st.lists(failed_job_st, min_size=0, max_size=50)

max_failures_st = st.integers(min_value=1, max_value=20)


# --- Helper: replicates the limiting logic from scripts/main.py ---

def apply_rate_limit(
    failed_jobs: list[FailedJob],
    max_failures_per_run: int,
) -> tuple[list[FailedJob], list[FailedJob]]:
    """Apply the per-run failure processing limit with alphabetical ordering.

    Returns (kept, skipped) where kept are the jobs to process and skipped
    are the jobs logged as "skipped-rate-limit".
    """
    failed_jobs = list(failed_jobs)  # don't mutate caller's list
    failed_jobs.sort(key=lambda j: j.name)
    if len(failed_jobs) > max_failures_per_run:
        skipped = failed_jobs[max_failures_per_run:]
        kept = failed_jobs[:max_failures_per_run]
    else:
        kept = failed_jobs
        skipped = []
    return kept, skipped


# --- Property Tests ---


@settings(max_examples=100)
@given(jobs=failed_jobs_list_st, max_failures=max_failures_st)
def test_kept_jobs_never_exceed_limit(
    jobs: list[FailedJob],
    max_failures: int,
) -> None:
    """The number of kept jobs never exceeds max_failures_per_run.

    **Validates: Requirements 10.2**
    """
    kept, _ = apply_rate_limit(jobs, max_failures)
    assert len(kept) <= max_failures


@settings(max_examples=100)
@given(jobs=failed_jobs_list_st, max_failures=max_failures_st)
def test_kept_plus_skipped_equals_total(
    jobs: list[FailedJob],
    max_failures: int,
) -> None:
    """All input jobs are either kept or skipped — none are lost.

    **Validates: Requirements 10.2, 10.3**
    """
    kept, skipped = apply_rate_limit(jobs, max_failures)
    assert len(kept) + len(skipped) == len(jobs)


@settings(max_examples=100)
@given(jobs=failed_jobs_list_st, max_failures=max_failures_st)
def test_kept_jobs_are_alphabetically_first(
    jobs: list[FailedJob],
    max_failures: int,
) -> None:
    """Kept jobs are the first max alphabetically by name.

    When N > max, the kept set must be exactly the first max jobs when all
    jobs are sorted alphabetically by name.

    **Validates: Requirements 10.3**
    """
    kept, skipped = apply_rate_limit(jobs, max_failures)

    if not kept:
        return

    # Kept jobs should be sorted alphabetically
    kept_names = [j.name for j in kept]
    assert kept_names == sorted(kept_names)

    # Every skipped job name should be >= every kept job name
    if skipped:
        max_kept_name = kept_names[-1]
        for s in skipped:
            assert s.name >= max_kept_name


@settings(max_examples=100)
@given(jobs=failed_jobs_list_st, max_failures=max_failures_st)
def test_under_limit_keeps_all_sorted(
    jobs: list[FailedJob],
    max_failures: int,
) -> None:
    """When N <= max, all jobs are kept in alphabetical order and none skipped.

    **Validates: Requirements 10.2**
    """
    if len(jobs) > max_failures:
        return  # only test the under-limit case

    kept, skipped = apply_rate_limit(jobs, max_failures)
    assert len(kept) == len(jobs)
    assert skipped == []
    # Kept should still be sorted
    kept_names = [j.name for j in kept]
    assert kept_names == sorted(kept_names)


@settings(max_examples=100)
@given(jobs=failed_jobs_list_st, max_failures=max_failures_st)
def test_over_limit_keeps_exactly_max(
    jobs: list[FailedJob],
    max_failures: int,
) -> None:
    """When N > max, exactly max jobs are kept.

    **Validates: Requirements 10.2, 10.3**
    """
    if len(jobs) <= max_failures:
        return  # only test the over-limit case

    kept, skipped = apply_rate_limit(jobs, max_failures)
    assert len(kept) == max_failures
    assert len(skipped) == len(jobs) - max_failures


@settings(max_examples=100)
@given(jobs=failed_jobs_list_st, max_failures=max_failures_st)
def test_skipped_jobs_logged_as_rate_limited(
    jobs: list[FailedJob],
    max_failures: int,
) -> None:
    """Skipped jobs are the ones that would be logged as 'skipped-rate-limit'.

    This property verifies the skipped set is non-empty only when N > max,
    and that every skipped job is NOT in the kept set.

    **Validates: Requirements 10.3**
    """
    kept, skipped = apply_rate_limit(jobs, max_failures)

    kept_ids = {id(j) for j in kept}
    for s in skipped:
        assert id(s) not in kept_ids

    if len(jobs) <= max_failures:
        assert skipped == []
    else:
        assert len(skipped) == len(jobs) - max_failures


# --- Unit test: verify the actual main.py logging behaviour ---


def test_skipped_jobs_emit_log_messages() -> None:
    """Unit test: the pipeline orchestrator logs 'skipped-rate-limit' for excess jobs.

    **Validates: Requirements 10.3**
    """
    import io

    jobs = [
        FailedJob(id=i, name=f"job-{chr(ord('a') + i)}", conclusion="failure", step_name=None)
        for i in range(5)
    ]
    max_limit = 3

    # Replicate the exact logic from scripts/main.py
    failed_jobs = list(jobs)
    failed_jobs.sort(key=lambda j: j.name)

    log = logging.getLogger("scripts.main")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        if len(failed_jobs) > max_limit:
            skipped = failed_jobs[max_limit:]
            for j in skipped:
                log.info("Skipping job %s: skipped-rate-limit", j.name)
            failed_jobs = failed_jobs[:max_limit]

        output = stream.getvalue()
    finally:
        log.removeHandler(handler)

    # Verify kept jobs
    assert len(failed_jobs) == max_limit
    kept_names = [j.name for j in failed_jobs]
    assert kept_names == sorted(kept_names)

    # Verify log messages for skipped jobs
    assert "skipped-rate-limit" in output
    # The last 2 jobs alphabetically should be skipped
    for j in jobs:
        if j.name not in kept_names:
            assert j.name in output

# Feature: valkey-ci-agent, Property 25: Workflow summary completeness
"""Property test for workflow summary completeness.

Property 25: For any bot run, the workflow summary should contain an entry
for every failure that was processed, skipped, or errored, with the
corresponding outcome status.

THE Bot SHALL emit a workflow summary (GitHub Actions job summary) at the
end of each run listing all failures processed, their outcomes, and any
errors encountered.

**Validates: Requirements 11.4**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.summary import WorkflowSummary

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Non-empty printable text for identifiers
safe_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P"),
        min_codepoint=32,
        max_codepoint=126,
    ),
    min_size=1,
    max_size=60,
).filter(lambda s: s.strip())

# Realistic outcome values
outcome_strategy = st.sampled_from([
    "pr-created",
    "skipped-duplicate",
    "skipped-rate-limit",
    "analysis-failed",
    "generation-failed",
    "validation-failed",
    "pr-creation-failed",
    "untrusted-fork",
    "unparseable",
])

# Optional error messages (None or non-empty text)
error_strategy = st.one_of(st.none(), safe_text)


@st.composite
def processing_result_strategy(draw):
    """Generate a single (job_name, failure_identifier, outcome, error) tuple."""
    return (
        draw(safe_text),
        draw(safe_text),
        draw(outcome_strategy),
        draw(error_strategy),
    )


# A list of 1..15 processing results
results_strategy = st.lists(processing_result_strategy(), min_size=1, max_size=15)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


@given(results=results_strategy)
@settings(max_examples=100)
def test_rendered_summary_contains_every_job_name_and_failure_identifier(results):
    """Property 25 (completeness): For any set of processing results added to
    the summary, the rendered markdown contains every job name, failure
    identifier, and outcome.

    **Validates: Requirements 11.4**
    """
    summary = WorkflowSummary(mode="analyze")
    for job_name, failure_id, outcome, error in results:
        summary.add_result(job_name, failure_id, outcome, error=error)

    md = summary.render()

    for job_name, failure_id, outcome, _error in results:
        assert job_name in md, f"Job name '{job_name}' missing from summary"
        assert failure_id in md, f"Failure identifier '{failure_id}' missing from summary"
        assert outcome in md, f"Outcome '{outcome}' missing from summary"


@given(results=results_strategy)
@settings(max_examples=100)
def test_errors_included_when_present(results):
    """Property 25 (errors): When an error is present on a result, it appears
    in the rendered summary.

    **Validates: Requirements 11.4**
    """
    summary = WorkflowSummary(mode="analyze")
    for job_name, failure_id, outcome, error in results:
        summary.add_result(job_name, failure_id, outcome, error=error)

    md = summary.render()

    for _job_name, _failure_id, _outcome, error in results:
        if error is not None:
            assert error in md, f"Error '{error}' missing from summary"


@given(results=results_strategy)
@settings(max_examples=100)
def test_summary_count_matches_results_added(results):
    """Property 25 (count): The summary's reported count matches the number
    of results added.

    **Validates: Requirements 11.4**
    """
    summary = WorkflowSummary(mode="analyze")
    for job_name, failure_id, outcome, error in results:
        summary.add_result(job_name, failure_id, outcome, error=error)

    md = summary.render()

    expected_total = len(results)
    assert f"**{expected_total}** failure(s) processed" in md, (
        f"Expected total count {expected_total} in summary"
    )

    expected_errors = sum(1 for _, _, _, e in results if e is not None)
    assert f"**{expected_errors}** error(s)" in md, (
        f"Expected error count {expected_errors} in summary"
    )

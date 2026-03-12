# Feature: valkey-ci-bot, Property 5: FailureReport contains all required fields
"""Property test for FailureReport completeness.

Validates: Requirements 2.6

Property 5: For any parsed failure, the resulting FailureReport should contain
non-empty values for workflow name, job name, commit SHA, failure source, and
at least one parsed failure or a raw log excerpt with the unparseable flag set.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.models import FailureReport, ParsedFailure

# --- Strategies ---

parser_types = st.sampled_from(["gtest", "tcl", "build", "sentinel", "cluster", "module"])

parsed_failure_strategy = st.builds(
    ParsedFailure,
    failure_identifier=st.text(min_size=1),
    test_name=st.one_of(st.none(), st.text(min_size=1)),
    file_path=st.text(min_size=1),
    error_message=st.text(min_size=1),
    assertion_details=st.one_of(st.none(), st.text()),
    line_number=st.one_of(st.none(), st.integers(min_value=1)),
    stack_trace=st.one_of(st.none(), st.text()),
    parser_type=parser_types,
)

# Strategy for a valid (complete) FailureReport: either has parsed failures or is unparseable with raw excerpt
failure_report_with_parsed = st.builds(
    FailureReport,
    workflow_name=st.text(min_size=1),
    job_name=st.text(min_size=1),
    matrix_params=st.dictionaries(st.text(min_size=1), st.text()),
    commit_sha=st.from_regex(r"[0-9a-f]{40}", fullmatch=True),
    failure_source=st.sampled_from(["trusted", "untrusted-fork"]),
    parsed_failures=st.lists(parsed_failure_strategy, min_size=1, max_size=5),
    raw_log_excerpt=st.one_of(st.none(), st.text()),
    is_unparseable=st.just(False),
)

failure_report_unparseable = st.builds(
    FailureReport,
    workflow_name=st.text(min_size=1),
    job_name=st.text(min_size=1),
    matrix_params=st.dictionaries(st.text(min_size=1), st.text()),
    commit_sha=st.from_regex(r"[0-9a-f]{40}", fullmatch=True),
    failure_source=st.sampled_from(["trusted", "untrusted-fork"]),
    parsed_failures=st.just([]),
    raw_log_excerpt=st.text(min_size=1),
    is_unparseable=st.just(True),
)

# Any valid FailureReport is one of the two forms
valid_failure_report_strategy = st.one_of(failure_report_with_parsed, failure_report_unparseable)


# --- Property Test ---


@settings(max_examples=100)
@given(report=valid_failure_report_strategy)
def test_failure_report_contains_all_required_fields(report: FailureReport) -> None:
    """Property 5: FailureReport contains all required fields.

    **Validates: Requirements 2.6**

    For any FailureReport, the following must hold:
    - workflow_name is non-empty
    - job_name is non-empty
    - commit_sha is non-empty
    - failure_source is non-empty
    - Either parsed_failures is non-empty, or raw_log_excerpt is non-empty
      with is_unparseable set to True
    """
    # Required string fields must be non-empty
    assert report.workflow_name, "workflow_name must be non-empty"
    assert report.job_name, "job_name must be non-empty"
    assert report.commit_sha, "commit_sha must be non-empty"
    assert report.failure_source, "failure_source must be non-empty"
    assert report.failure_source in ("trusted", "untrusted-fork"), (
        f"failure_source must be 'trusted' or 'untrusted-fork', got '{report.failure_source}'"
    )

    # Must have either parsed failures or an unparseable raw excerpt
    has_parsed = len(report.parsed_failures) > 0
    has_raw_excerpt = report.raw_log_excerpt is not None and len(report.raw_log_excerpt) > 0 and report.is_unparseable

    assert has_parsed or has_raw_excerpt, (
        "FailureReport must contain at least one parsed failure "
        "or a non-empty raw_log_excerpt with is_unparseable=True"
    )

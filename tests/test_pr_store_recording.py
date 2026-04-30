# Feature: valkey-ci-agent, Property 14: PR creation records in failure store
"""Property test for PR creation records in failure store.

Property 14: For any successfully created PR, the Failure_Store should
contain an entry mapping the failure fingerprint to the PR URL with
status "open".

**Validates: Requirements 6.6**
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.failure_store import FailureStore
from scripts.models import FailureReport, ParsedFailure, RootCauseReport
from scripts.pr_manager import PRManager, _compute_fingerprint

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

safe_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P"), min_codepoint=32, max_codepoint=126
    ),
    min_size=1,
    max_size=60,
).filter(lambda s: s.strip())

file_path_strategy = st.from_regex(r"[a-z][a-z0-9_/]*\.[a-z]{1,4}", fullmatch=True)

confidence_strategy = st.sampled_from(["high", "medium"])

parser_type_strategy = st.sampled_from(
    ["gtest", "tcl", "build", "sentinel", "cluster", "module"]
)

pr_number_strategy = st.integers(min_value=1, max_value=99999)


@st.composite
def parsed_failure_strategy(draw):
    """Generate a valid ParsedFailure."""
    return ParsedFailure(
        failure_identifier=draw(safe_text),
        test_name=draw(st.one_of(st.none(), safe_text)),
        file_path=draw(file_path_strategy),
        error_message=draw(safe_text),
        assertion_details=draw(st.one_of(st.none(), safe_text)),
        line_number=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=10000))),
        stack_trace=draw(st.one_of(st.none(), safe_text)),
        parser_type=draw(parser_type_strategy),
    )


@st.composite
def failure_report_strategy(draw):
    """Generate a valid trusted FailureReport with at least one parsed failure."""
    failures = draw(st.lists(parsed_failure_strategy(), min_size=1, max_size=3))
    return FailureReport(
        workflow_name=draw(safe_text),
        job_name=draw(safe_text),
        matrix_params=draw(
            st.fixed_dictionaries(
                {}, optional={"os": st.sampled_from(["ubuntu-latest", "macos-latest"])}
            )
        ),
        commit_sha=draw(st.from_regex(r"[0-9a-f]{12,40}", fullmatch=True)),
        failure_source="trusted",
        parsed_failures=failures,
        raw_log_excerpt=None,
        is_unparseable=False,
    )


@st.composite
def root_cause_strategy(draw):
    """Generate a valid RootCauseReport."""
    return RootCauseReport(
        description=draw(safe_text),
        files_to_change=draw(st.lists(file_path_strategy, min_size=1, max_size=5)),
        confidence=draw(confidence_strategy),
        rationale=draw(safe_text),
        is_flaky=False,
        flakiness_indicators=None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_PATCH = "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n+b\n"


def _make_mock_repo(pr_number: int = 1):
    """Create a mock GitHub repo for PRManager."""
    repo = MagicMock()
    ref = MagicMock()
    ref.object.sha = "aabbccdd11223344"
    repo.get_git_ref.return_value = ref
    repo.create_git_ref.return_value = MagicMock()

    contents = MagicMock()
    contents.decoded_content = b"a\n"
    contents.sha = "file_sha_000"
    repo.get_contents.return_value = contents
    repo.update_file.return_value = {"commit": MagicMock()}

    pr = MagicMock()
    pr.number = pr_number
    pr.html_url = f"https://github.com/owner/repo/pull/{pr_number}"
    repo.create_pull.return_value = pr

    return repo


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@given(
    report=failure_report_strategy(),
    root_cause=root_cause_strategy(),
    pr_number=pr_number_strategy,
)
@settings(max_examples=100)
def test_pr_creation_records_fingerprint_with_open_status(report, root_cause, pr_number):
    """Property 14: After a successful PR creation, the failure store contains
    an entry mapping the failure fingerprint to the PR URL with status "open".

    **Validates: Requirements 6.6**
    """
    repo = _make_mock_repo(pr_number)
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = FailureStore()
    mgr = PRManager(gh, "owner/repo", store)

    pr_url = mgr.create_pr(MINIMAL_PATCH, report, root_cause, "unstable")

    # The expected fingerprint is derived from the first parsed failure
    fingerprint = _compute_fingerprint(report)

    # The store must contain the fingerprint
    assert fingerprint in store.entries, (
        f"Fingerprint {fingerprint!r} not found in failure store after PR creation"
    )

    entry = store.entries[fingerprint]

    # The entry must map to the returned PR URL
    assert entry.pr_url == pr_url, (
        f"Expected pr_url={pr_url!r}, got {entry.pr_url!r}"
    )

    # The entry status must be "open"
    assert entry.status == "open", (
        f"Expected status='open', got {entry.status!r}"
    )

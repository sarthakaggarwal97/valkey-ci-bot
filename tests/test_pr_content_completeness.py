# Feature: valkey-ci-agent, Property 13: PR content completeness
"""Property test for PR content completeness.

Property 13: For any validated fix, the created PR should have: a branch
named `bot/fix/<fingerprint>`, a commit message containing the failure
identifier (or test name when available) and job name, a PR body containing
a link to the failing CI run, the failure summary, root cause analysis,
confidence level, and an AI-generated disclaimer, and the `bot-fix` label
applied.

**Validates: Requirements 6.1, 6.2, 6.4, 6.5**
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from scripts.failure_store import FailureStore
from scripts.models import FailureReport, ParsedFailure, RootCauseReport
from scripts.pr_manager import (
    PRManager,
    _build_commit_message,
    _build_pr_body,
    _compute_fingerprint,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Safe non-empty text for identifiers and messages
safe_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P"), min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=60,
).filter(lambda s: s.strip())

# File paths
file_path_strategy = st.from_regex(r"[a-z][a-z0-9_/]*\.[a-z]{1,4}", fullmatch=True)

# Confidence levels
confidence_strategy = st.sampled_from(["high", "medium"])

# Parser types
parser_type_strategy = st.sampled_from(["gtest", "tcl", "build", "sentinel", "cluster", "module"])


@st.composite
def parsed_failure_strategy(draw):
    """Generate a valid ParsedFailure."""
    test_name = draw(st.one_of(st.none(), safe_text))
    return ParsedFailure(
        failure_identifier=draw(safe_text),
        test_name=test_name,
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
        matrix_params=draw(st.fixed_dictionaries({}, optional={
            "os": st.sampled_from(["ubuntu-latest", "macos-latest"]),
        })),
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

def _make_mock_repo():
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
    pr.number = 1
    pr.html_url = "https://github.com/owner/repo/pull/1"
    repo.create_pull.return_value = pr

    return repo


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

@given(report=failure_report_strategy(), root_cause=root_cause_strategy())
@settings(deadline=None, max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_branch_name_follows_fingerprint_pattern(report, root_cause):
    """Property 13 (branch): The branch is always named bot/fix/<fingerprint>.

    **Validates: Requirements 6.1**
    """
    fingerprint = _compute_fingerprint(report)

    repo = _make_mock_repo()
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = FailureStore()
    mgr = PRManager(gh, "owner/repo", store)

    mgr.create_pr("--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n+b\n", report, root_cause, "unstable")

    ref_call = repo.create_git_ref.call_args
    created_ref = ref_call.kwargs.get("ref") or ref_call[1].get("ref")
    assert created_ref == f"refs/heads/bot/fix/{fingerprint}"


@given(report=failure_report_strategy(), root_cause=root_cause_strategy())
@settings(deadline=None, max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_commit_message_contains_identifier_and_job(report, root_cause):
    """Property 13 (commit message): The commit message always contains the
    failure identifier (or test name when available) and the job name.

    **Validates: Requirements 6.2**
    """
    msg = _build_commit_message(report, root_cause)

    pf = report.parsed_failures[0]
    expected_id = pf.test_name if pf.test_name else pf.failure_identifier
    assert expected_id in msg, f"Expected '{expected_id}' in commit message"
    assert report.job_name in msg, f"Expected job name '{report.job_name}' in commit message"


@given(report=failure_report_strategy(), root_cause=root_cause_strategy())
@settings(deadline=None, max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_pr_body_contains_all_required_elements(report, root_cause):
    """Property 13 (PR body): The PR body always contains a link to the
    failing CI run, the failure summary, root cause analysis, confidence
    level, and an AI-generated disclaimer.

    **Validates: Requirements 6.4**
    """
    run_url = f"https://github.com/owner/repo/actions/runs/{report.commit_sha}"
    body = _build_pr_body(report, root_cause, run_url)

    # CI run link
    assert run_url in body, "PR body must contain the CI run link"

    # Failure summary — first parsed failure identifier should appear
    pf = report.parsed_failures[0]
    assert pf.failure_identifier in body, "PR body must contain failure identifier"

    # Root cause analysis
    assert root_cause.description in body, "PR body must contain root cause description"
    assert root_cause.rationale in body, "PR body must contain rationale"

    # Confidence level
    assert root_cause.confidence in body, "PR body must contain confidence level"

    # AI disclaimer
    body_lower = body.lower()
    assert "ai agent" in body_lower or "ai" in body_lower, "PR body must contain AI disclaimer"
    assert "human review" in body_lower, "PR body must mention human review"


@given(report=failure_report_strategy(), root_cause=root_cause_strategy())
@settings(deadline=None, max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_bot_fix_label_is_applied(report, root_cause):
    """Property 13 (label): The `bot-fix` label is always applied to the PR.

    **Validates: Requirements 6.5**
    """
    repo = _make_mock_repo()
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = FailureStore()
    mgr = PRManager(gh, "owner/repo", store)

    mgr.create_pr("--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n+b\n", report, root_cause, "unstable")

    pr_mock = repo.create_pull.return_value
    pr_mock.add_to_labels.assert_called_once_with("bot-fix")

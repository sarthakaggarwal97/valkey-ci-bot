# Feature: valkey-ci-agent, Property 27: Untrusted fork failures never execute privileged stages
"""Property test for untrusted fork trust gating.

Property 27: For any workflow_run whose head repository differs from the
consumer repository, the bot should stop before Bedrock-backed fix generation,
validation, branch creation, or PR creation, and it should record the outcome
as ``untrusted-fork``.

THE Bot SHALL classify each Failure_Event as trusted same-repository work or
untrusted fork-originated work before invoking privileged stages. Untrusted
fork-originated failures SHALL be skipped before Bedrock-backed analysis/fix
generation, validation, or PR creation and logged as "untrusted-fork".

**Validates: Requirements 1.6, 5.5, 6.3**
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from scripts.config import BotConfig, ValidationProfile
from scripts.failure_detector import FailureDetector
from scripts.failure_store import FailureStore
from scripts.models import (
    FailureReport,
    ParsedFailure,
    ValidationResult,
    WorkflowRun,
)
from scripts.pr_manager import PRManager
from scripts.validation_runner import ValidationRunner

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

sha_strategy = st.from_regex(r"[0-9a-f]{40}", fullmatch=True)

repo_name_strategy = st.from_regex(r"[a-z][a-z0-9\-]{0,19}/[a-z][a-z0-9\-]{0,19}", fullmatch=True)

file_path_strategy = st.from_regex(r"[a-z][a-z0-9_/]*\.[a-z]{1,4}", fullmatch=True)

parser_type_strategy = st.sampled_from(
    ["gtest", "tcl", "build", "sentinel", "cluster", "module"]
)


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
def untrusted_workflow_run_strategy(draw):
    """Generate a WorkflowRun from a fork (untrusted)."""
    consumer = draw(repo_name_strategy)
    # Fork repo must differ from consumer
    fork = draw(repo_name_strategy.filter(lambda r: r != consumer))
    return (
        WorkflowRun(
            id=draw(st.integers(min_value=1, max_value=10**9)),
            name=draw(safe_text),
            event="pull_request",
            head_sha=draw(sha_strategy),
            head_branch=draw(safe_text),
            head_repository=fork,
            is_fork=True,
            conclusion="failure",
            workflow_file=draw(
                st.sampled_from(["ci.yml", "daily.yml", "weekly.yml", "external.yml"])
            ),
        ),
        consumer,
    )


@st.composite
def trusted_workflow_run_strategy(draw):
    """Generate a WorkflowRun from the same repo (trusted)."""
    consumer = draw(repo_name_strategy)
    return (
        WorkflowRun(
            id=draw(st.integers(min_value=1, max_value=10**9)),
            name=draw(safe_text),
            event="push",
            head_sha=draw(sha_strategy),
            head_branch=draw(safe_text),
            head_repository=consumer,
            is_fork=False,
            conclusion="failure",
            workflow_file=draw(
                st.sampled_from(["ci.yml", "daily.yml", "weekly.yml", "external.yml"])
            ),
        ),
        consumer,
    )


@st.composite
def untrusted_failure_report_strategy(draw):
    """Generate a FailureReport marked as untrusted-fork."""
    failures = draw(st.lists(parsed_failure_strategy(), min_size=1, max_size=3))
    return FailureReport(
        workflow_name=draw(safe_text),
        job_name=draw(safe_text),
        matrix_params=draw(
            st.fixed_dictionaries(
                {}, optional={"os": st.sampled_from(["ubuntu-latest", "macos-latest"])}
            )
        ),
        commit_sha=draw(sha_strategy),
        failure_source="untrusted-fork",
        parsed_failures=failures,
    )


@st.composite
def trusted_failure_report_strategy(draw):
    """Generate a FailureReport marked as trusted."""
    failures = draw(st.lists(parsed_failure_strategy(), min_size=1, max_size=3))
    return FailureReport(
        workflow_name=draw(safe_text),
        job_name=draw(safe_text),
        matrix_params={},
        commit_sha=draw(sha_strategy),
        failure_source="trusted",
        parsed_failures=failures,
    )


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


@given(data=untrusted_workflow_run_strategy())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_untrusted_fork_classified_correctly(data):
    """Property 27 (classify_trust): For any workflow_run whose head repository
    differs from the consumer repository, classify_trust returns 'untrusted-fork'.

    **Validates: Requirements 1.6**
    """
    workflow_run, consumer_repo = data
    result = FailureDetector.classify_trust(workflow_run, consumer_repo)
    assert result == "untrusted-fork", (
        f"Expected 'untrusted-fork' for fork repo "
        f"'{workflow_run.head_repository}' vs consumer '{consumer_repo}'"
    )


@given(data=trusted_workflow_run_strategy())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_trusted_repo_classified_correctly(data):
    """Property 27 (classify_trust): For any workflow_run whose head repository
    matches the consumer repository and is_fork is False, classify_trust
    returns 'trusted'.

    **Validates: Requirements 1.6**
    """
    workflow_run, consumer_repo = data
    result = FailureDetector.classify_trust(workflow_run, consumer_repo)
    assert result == "trusted", (
        f"Expected 'trusted' for same repo "
        f"'{workflow_run.head_repository}' == '{consumer_repo}'"
    )


@given(report=untrusted_failure_report_strategy())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_untrusted_fork_validation_skipped(report):
    """Property 27 (validation): For any untrusted fork failure, validation
    is skipped and returns output 'untrusted-fork'.

    **Validates: Requirements 5.5**
    """
    config = BotConfig(
        validation_profiles=[
            ValidationProfile(
                job_name_pattern=".*",
                build_commands=["make"],
                test_commands=["make test"],
            )
        ]
    )
    runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")

    result = runner.validate("--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n", report)

    assert result.passed is False
    assert result.output == "untrusted-fork"


@given(report=untrusted_failure_report_strategy())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_untrusted_fork_pr_creation_skipped(report):
    """Property 27 (PR creation): For any untrusted fork failure, PR creation
    is skipped and raises ValueError with 'fork-pr-no-write-access'.

    **Validates: Requirements 6.3**
    """
    gh = MagicMock()
    store = FailureStore()
    mgr = PRManager(gh, "owner/repo", store)

    root_cause = MagicMock()
    root_cause.description = "test root cause"
    root_cause.files_to_change = ["src/foo.c"]
    root_cause.confidence = "high"
    root_cause.rationale = "test rationale"

    with pytest.raises(ValueError, match="fork-pr-no-write-access"):
        mgr.create_pr(
            "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n",
            report,
            root_cause,
            "unstable",
        )

    # GitHub API should never be called for untrusted forks
    gh.get_repo.assert_not_called()


@given(report=trusted_failure_report_strategy())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_trusted_failure_validation_proceeds(report):
    """Property 27 (trusted validation): For any trusted failure, validation
    proceeds normally (does not short-circuit with 'untrusted-fork').

    **Validates: Requirements 1.6, 5.5**
    """
    config = BotConfig(
        validation_profiles=[
            ValidationProfile(
                job_name_pattern=".*",
                build_commands=["make"],
                test_commands=["make test"],
            )
        ]
    )
    runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")

    # Mock the subprocess-based steps so we don't actually clone/build
    from unittest.mock import patch as mock_patch

    with mock_patch.object(runner, "_checkout_repo", return_value=(True, "")), \
         mock_patch.object(runner, "_apply_patch", return_value=(True, "")), \
         mock_patch(
             "scripts.validation_runner._run_commands",
             return_value=(True, "OK"),
         ):
        result = runner.validate("--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n", report)

    assert result.passed is True
    assert result.output != "untrusted-fork"


@given(report=trusted_failure_report_strategy())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_trusted_failure_pr_creation_proceeds(report):
    """Property 27 (trusted PR creation): For any trusted failure, PR creation
    proceeds normally (does not raise fork-pr-no-write-access).

    **Validates: Requirements 1.6, 6.3**
    """
    repo_mock = MagicMock()
    ref = MagicMock()
    ref.object.sha = "aabbccdd11223344"
    repo_mock.get_git_ref.return_value = ref
    repo_mock.create_git_ref.return_value = MagicMock()

    contents = MagicMock()
    contents.decoded_content = b"a\n"
    contents.sha = "file_sha_000"
    repo_mock.get_contents.return_value = contents
    repo_mock.update_file.return_value = {"commit": MagicMock()}

    pr = MagicMock()
    pr.number = 1
    pr.html_url = "https://github.com/owner/repo/pull/1"
    repo_mock.create_pull.return_value = pr

    gh = MagicMock()
    gh.get_repo.return_value = repo_mock
    store = FailureStore()
    mgr = PRManager(gh, "owner/repo", store)

    root_cause = MagicMock()
    root_cause.description = "test root cause"
    root_cause.files_to_change = ["src/foo.c"]
    root_cause.confidence = "high"
    root_cause.rationale = "test rationale"

    # Should not raise ValueError
    pr_url = mgr.create_pr(
        "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n+b\n",
        report,
        root_cause,
        "unstable",
    )

    assert pr_url == "https://github.com/owner/repo/pull/1"
    gh.get_repo.assert_called_once()

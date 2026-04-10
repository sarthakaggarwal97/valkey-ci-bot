"""Tests for reconciliation preflight checks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from github.GithubException import GithubException

from scripts.models import (
    FailureReport,
    ParsedFailure,
    RootCauseReport,
    failure_report_to_dict,
    root_cause_report_to_dict,
)
from scripts.preflight_reconciliation import _resolve_target_branch, run_preflight


def _make_report(target_branch: str = "unstable") -> FailureReport:
    return FailureReport(
        workflow_name="Daily",
        job_name="unit",
        matrix_params={},
        commit_sha="abc123",
        failure_source="trusted",
        repo_full_name="valkey-io/valkey",
        workflow_run_id=123,
        target_branch=target_branch,
        parsed_failures=[
            ParsedFailure(
                failure_identifier="suite.case",
                test_name="suite.case",
                file_path="src/foo.c",
                error_message="boom",
                assertion_details=None,
                line_number=None,
                stack_trace=None,
                parser_type="gtest",
            )
        ],
        raw_log_excerpt=None,
        is_unparseable=False,
    )


def _make_root_cause() -> RootCauseReport:
    return RootCauseReport(
        description="Fix off-by-one",
        files_to_change=["src/foo.c"],
        confidence="high",
        rationale="Validated locally",
        is_flaky=False,
        flakiness_indicators=None,
    )


def test_resolve_target_branch_prefers_payload_value() -> None:
    payload = {
        "target_branch": "8.1",
        "failure_report": failure_report_to_dict(_make_report(target_branch="unstable")),
    }

    assert _resolve_target_branch(payload) == "8.1"


@patch("scripts.preflight_reconciliation.FailureStore")
@patch("scripts.preflight_reconciliation.Github")
def test_run_preflight_collects_target_branches(
    mock_github_cls,
    mock_failure_store_cls,
) -> None:
    repo = MagicMock()
    mock_github_cls.return_value.get_repo.return_value = repo

    report = _make_report(target_branch="unstable")
    store = mock_failure_store_cls.return_value
    store.list_queued_failures.return_value = ["fp1", "fp2"]
    store.get_entry.side_effect = [
        MagicMock(
            queued_pr_payload={
                "failure_report": failure_report_to_dict(report),
                "root_cause": root_cause_report_to_dict(_make_root_cause()),
                "patch": "diff",
                "target_branch": "8.1",
            }
        ),
        MagicMock(
            queued_pr_payload={
                "failure_report": failure_report_to_dict(report),
                "root_cause": root_cause_report_to_dict(_make_root_cause()),
                "patch": "diff",
            }
        ),
    ]

    result = run_preflight(
        "sarthakaggarwal97/valkey",
        "token",
        state_github_token="state-token",
        state_repo_name="owner/bot",
    )

    assert result.queued_failure_count == 2
    assert result.target_branches == ["8.1", "unstable"]
    assert result.missing_branches == []
    repo.get_git_ref.assert_any_call("heads/8.1")
    repo.get_git_ref.assert_any_call("heads/unstable")


@patch("scripts.preflight_reconciliation.FailureStore")
@patch("scripts.preflight_reconciliation.Github")
def test_run_preflight_reports_missing_branches(
    mock_github_cls,
    mock_failure_store_cls,
) -> None:
    repo = MagicMock()

    def get_git_ref(ref: str):
        if ref == "heads/unstable":
            return MagicMock()
        raise GithubException(404, {"message": "Not Found"})

    repo.get_git_ref.side_effect = get_git_ref
    mock_github_cls.return_value.get_repo.return_value = repo

    store = mock_failure_store_cls.return_value
    store.list_queued_failures.return_value = ["fp1", "fp2"]
    store.get_entry.side_effect = [
        MagicMock(
            queued_pr_payload={
                "failure_report": failure_report_to_dict(_make_report()),
                "root_cause": root_cause_report_to_dict(_make_root_cause()),
                "patch": "diff",
                "target_branch": "unstable",
            }
        ),
        MagicMock(
            queued_pr_payload={
                "failure_report": failure_report_to_dict(_make_report(target_branch="9.1")),
                "root_cause": root_cause_report_to_dict(_make_root_cause()),
                "patch": "diff",
            }
        ),
    ]

    result = run_preflight("sarthakaggarwal97/valkey", "token")

    assert result.target_branches == ["9.1", "unstable"]
    assert result.missing_branches == ["9.1"]

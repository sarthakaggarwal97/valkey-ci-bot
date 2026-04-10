"""Tests for GitHub-native draft PR proof campaigns."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts.config import BotConfig
from scripts.models import FailureReport, ParsedFailure
from scripts.prove_pr_fix import run_proof_campaign


def _failure_report_json() -> str:
    report = FailureReport(
        workflow_name="Daily",
        job_name="test-ubuntu-jemalloc",
        matrix_params={},
        commit_sha="deadbeef",
        failure_source="trusted",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="unit/maxmemory",
                test_name="unit/maxmemory",
                file_path="tests/unit/maxmemory.tcl",
                error_message="assertion failed",
                assertion_details=None,
                line_number=None,
                stack_trace=None,
                parser_type="tcl",
            )
        ],
        workflow_file="daily.yml",
        repo_full_name="valkey-io/valkey",
        workflow_run_id=123,
        target_branch="unstable",
    )
    return json.dumps(
        {
            "workflow_name": report.workflow_name,
            "job_name": report.job_name,
            "matrix_params": report.matrix_params,
            "commit_sha": report.commit_sha,
            "failure_source": report.failure_source,
            "parsed_failures": [
                {
                    "failure_identifier": report.parsed_failures[0].failure_identifier,
                    "test_name": report.parsed_failures[0].test_name,
                    "file_path": report.parsed_failures[0].file_path,
                    "error_message": report.parsed_failures[0].error_message,
                    "assertion_details": None,
                    "line_number": None,
                    "stack_trace": None,
                    "parser_type": report.parsed_failures[0].parser_type,
                }
            ],
            "workflow_file": report.workflow_file,
            "repo_full_name": report.repo_full_name,
            "workflow_run_id": report.workflow_run_id,
            "target_branch": report.target_branch,
        }
    )


@patch("scripts.prove_pr_fix._proof_run_url", return_value="https://github.com/sarthakaggarwal97/valkey-ci-agent/actions/runs/1")
@patch("scripts.prove_pr_fix._upsert_proof_comment", return_value="https://github.com/owner/repo/pull/7#issuecomment-1")
@patch("scripts.prove_pr_fix._remove_bot_fix_label", return_value=True)
@patch("scripts.prove_pr_fix._land_upstream_pr", return_value=("https://github.com/valkey-io/valkey/pull/99", False))
@patch("scripts.prove_pr_fix._mark_ready_for_review", return_value=True)
@patch("scripts.prove_pr_fix.ValidationRunner")
@patch("scripts.prove_pr_fix.EventLedger")
@patch("scripts.prove_pr_fix.FailureStore")
@patch("scripts.prove_pr_fix.load_config", return_value=BotConfig())
@patch("scripts.prove_pr_fix.Github")
def test_run_proof_campaign_marks_ready_after_clean_proof(
    mock_github,
    mock_load_config,
    mock_failure_store,
    mock_event_ledger,
    mock_validation_runner,
    mock_mark_ready,
    mock_land_upstream,
    mock_remove_label,
    mock_comment,
    mock_proof_run_url,
) -> None:
    target_gh = MagicMock()
    state_gh = MagicMock()
    landing_gh = MagicMock()
    mock_github.side_effect = [target_gh, state_gh, landing_gh]

    repo = MagicMock()
    pr = MagicMock()
    pr.number = 7
    pr.title = "[bot-fix] Fix unit/maxmemory"
    pr.body = "Body"
    pr.html_url = "https://github.com/owner/repo/pull/7"
    pr.draft = True
    pr.head = SimpleNamespace(
        sha="cafebabe",
        ref="bot/fix/fp-proof",
        repo=SimpleNamespace(
            full_name="owner/repo",
            owner=SimpleNamespace(login="owner"),
        ),
    )
    pr.base = SimpleNamespace(ref="unstable")
    repo.get_pull.return_value = pr
    target_gh.get_repo.return_value = repo

    validation_runner = mock_validation_runner.return_value
    validation_runner.validate.return_value = SimpleNamespace(
        passed=True,
        output="Validation passed across 100/100 consecutive runs.",
        strategy="local",
        passed_runs=100,
        attempted_runs=100,
    )

    args = SimpleNamespace(
        repo="owner/repo",
        pr_number=7,
        fingerprint="fp-proof",
        failure_report_json=_failure_report_json(),
        config=".github/valkey-daily-bot.yml",
        token="token",
        landing_token="landing-token",
        state_token="state-token",
        state_repo="owner/state-repo",
        repeat_count=100,
    )

    result = run_proof_campaign(args)

    assert result["proof_status"] == "passed"
    assert result["ready_for_review"] is True
    assert result["landing_status"] == "passed"
    assert result["landing_url"] == "https://github.com/valkey-io/valkey/pull/99"
    validation_runner.validate.assert_called_once()
    assert validation_runner.validate.call_args.args[0] == ""
    assert validation_runner.validate.call_args.kwargs["repeat_count"] == 100
    failure_store = mock_failure_store.return_value
    assert failure_store.update_proof_campaign.call_count == 2
    failure_store.update_landing_campaign.assert_called_once()
    mock_mark_ready.assert_called_once_with("owner/repo", 7, "token")
    mock_land_upstream.assert_called_once()
    mock_remove_label.assert_called_once_with(pr)
    mock_comment.assert_called_once()
    event_ledger = mock_event_ledger.return_value
    recorded_types = [call.args[0] for call in event_ledger.record.call_args_list]
    assert "proof.started" in recorded_types
    assert "proof.passed" in recorded_types
    assert "pr.ready_for_review" in recorded_types
    assert "pr.landed" in recorded_types


@patch("scripts.prove_pr_fix._proof_run_url", return_value="")
@patch("scripts.prove_pr_fix._upsert_proof_comment", return_value="https://github.com/owner/repo/pull/7#issuecomment-2")
@patch("scripts.prove_pr_fix._land_upstream_pr")
@patch("scripts.prove_pr_fix._mark_ready_for_review")
@patch("scripts.prove_pr_fix.ValidationRunner")
@patch("scripts.prove_pr_fix.EventLedger")
@patch("scripts.prove_pr_fix.FailureStore")
@patch("scripts.prove_pr_fix.load_config", return_value=BotConfig())
@patch("scripts.prove_pr_fix.Github")
def test_run_proof_campaign_keeps_draft_on_failed_proof(
    mock_github,
    mock_load_config,
    mock_failure_store,
    mock_event_ledger,
    mock_validation_runner,
    mock_mark_ready,
    mock_land_upstream,
    mock_comment,
    mock_proof_run_url,
) -> None:
    target_gh = MagicMock()
    state_gh = MagicMock()
    mock_github.side_effect = [target_gh, state_gh]

    repo = MagicMock()
    pr = MagicMock()
    pr.number = 7
    pr.html_url = "https://github.com/owner/repo/pull/7"
    pr.draft = True
    pr.head = SimpleNamespace(
        sha="cafebabe",
        ref="bot/fix/fp-proof",
        repo=SimpleNamespace(full_name="owner/repo", owner=SimpleNamespace(login="owner")),
    )
    pr.base = SimpleNamespace(ref="unstable")
    repo.get_pull.return_value = pr
    target_gh.get_repo.return_value = repo

    validation_runner = mock_validation_runner.return_value
    validation_runner.validate.return_value = SimpleNamespace(
        passed=False,
        output="Tests failed:\nflaky regression",
        strategy="local",
        passed_runs=12,
        attempted_runs=13,
    )

    args = SimpleNamespace(
        repo="owner/repo",
        pr_number=7,
        fingerprint="fp-proof",
        failure_report_json=_failure_report_json(),
        config=".github/valkey-daily-bot.yml",
        token="token",
        landing_token="landing-token",
        state_token="state-token",
        state_repo="owner/state-repo",
        repeat_count=100,
    )

    result = run_proof_campaign(args)

    assert result["proof_status"] == "failed"
    assert result["ready_for_review"] is False
    mock_mark_ready.assert_not_called()
    mock_land_upstream.assert_not_called()
    event_ledger = mock_event_ledger.return_value
    recorded_types = [call.args[0] for call in event_ledger.record.call_args_list]
    assert "proof.failed" in recorded_types


@patch("scripts.prove_pr_fix._proof_run_url", return_value="")
@patch("scripts.prove_pr_fix._upsert_proof_comment", return_value="https://github.com/owner/repo/pull/7#issuecomment-3")
@patch("scripts.prove_pr_fix._land_upstream_pr", side_effect=RuntimeError("boom"))
@patch("scripts.prove_pr_fix._mark_ready_for_review", return_value=True)
@patch("scripts.prove_pr_fix.ValidationRunner")
@patch("scripts.prove_pr_fix.EventLedger")
@patch("scripts.prove_pr_fix.FailureStore")
@patch("scripts.prove_pr_fix.load_config", return_value=BotConfig())
@patch("scripts.prove_pr_fix.Github")
def test_run_proof_campaign_records_failed_upstream_landing(
    mock_github,
    mock_load_config,
    mock_failure_store,
    mock_event_ledger,
    mock_validation_runner,
    mock_mark_ready,
    mock_land_upstream,
    mock_comment,
    mock_proof_run_url,
) -> None:
    target_gh = MagicMock()
    state_gh = MagicMock()
    landing_gh = MagicMock()
    mock_github.side_effect = [target_gh, state_gh, landing_gh]

    repo = MagicMock()
    pr = MagicMock()
    pr.number = 7
    pr.title = "[bot-fix] Fix unit/maxmemory"
    pr.body = "Body"
    pr.html_url = "https://github.com/owner/repo/pull/7"
    pr.draft = True
    pr.head = SimpleNamespace(
        sha="cafebabe",
        ref="bot/fix/fp-proof",
        repo=SimpleNamespace(full_name="owner/repo", owner=SimpleNamespace(login="owner")),
    )
    pr.base = SimpleNamespace(ref="unstable")
    repo.get_pull.return_value = pr
    target_gh.get_repo.return_value = repo

    validation_runner = mock_validation_runner.return_value
    validation_runner.validate.return_value = SimpleNamespace(
        passed=True,
        output="Validation passed across 100/100 consecutive runs.",
        strategy="local",
        passed_runs=100,
        attempted_runs=100,
    )

    args = SimpleNamespace(
        repo="owner/repo",
        pr_number=7,
        fingerprint="fp-proof",
        failure_report_json=_failure_report_json(),
        config=".github/valkey-daily-bot.yml",
        token="token",
        landing_token="landing-token",
        state_token="state-token",
        state_repo="owner/state-repo",
        repeat_count=100,
    )

    result = run_proof_campaign(args)

    assert result["proof_status"] == "passed"
    assert result["landing_status"] == "failed"
    assert "upstream-landing-failed" in result["landing_summary"]
    mock_mark_ready.assert_called_once_with("owner/repo", 7, "token")
    mock_land_upstream.assert_called_once()
    mock_failure_store.return_value.update_landing_campaign.assert_called_once()
    recorded_types = [call.args[0] for call in mock_event_ledger.return_value.record.call_args_list]
    assert "pr.landing_failed" in recorded_types

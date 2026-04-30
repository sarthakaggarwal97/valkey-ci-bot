"""Tests for centralized workflow monitoring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from github.GithubException import GithubException

from scripts.main import PipelineResult
from scripts.monitor_workflow_runs import MonitorArgs, monitor, parse_args


def _args(**overrides) -> MonitorArgs:
    defaults = dict(
        target_repo="valkey-io/valkey",
        workflow_file="daily.yml",
        events=("schedule",),
        config_path=".github/valkey-daily-bot.yml",
        target_token="target-token",
        state_token="state-token",
        state_repo="owner/valkey-ci-agent",
        max_runs=14,
        aws_region="us-east-1",
        dry_run=False,
        queue_only=False,
        verbose=False,
    )
    defaults.update(overrides)
    return MonitorArgs(**defaults)


def _run(run_id: int, conclusion: str) -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.run_number = run_id - 900
    run.event = "schedule"
    run.conclusion = conclusion
    run.created_at.isoformat.return_value = f"2026-04-{run_id - 94:02d}T02:00:00+00:00"
    run.head_sha = f"sha-{run_id}"
    run.html_url = f"https://github.com/valkey-io/valkey/actions/runs/{run_id}"
    return run


@pytest.fixture(autouse=True)
def _mock_event_ledger():
    with patch("scripts.monitor_workflow_runs.EventLedger") as mock_cls:
        yield mock_cls.return_value


@patch("scripts.monitor_workflow_runs.run_pipeline")
@patch("scripts.monitor_workflow_runs.Github")
@patch("scripts.monitor_workflow_runs.MonitorStateStore")
def test_monitor_processes_only_new_failed_runs_and_updates_watermark(
    mock_state_store_cls,
    mock_github_cls,
    mock_run_pipeline,
    _mock_event_ledger,
) -> None:
    state_store = mock_state_store_cls.return_value
    state_store.get_last_seen_run_id.return_value = 100

    workflow = MagicMock()
    workflow.get_runs.return_value = [
        _run(103, "success"),
        _run(102, "failure"),
        _run(101, "failure"),
        _run(99, "failure"),
    ]
    repo = MagicMock()
    repo.get_workflow.return_value = workflow
    repo.get_contents.side_effect = GithubException(404, {"message": "missing state"})
    mock_github_cls.return_value.get_repo.return_value = repo
    mock_run_pipeline.return_value = PipelineResult(
        reports=[MagicMock()],
        job_outcomes=[{"job_name": "test-job", "failure_identifier": "test-id", "outcome": "pr-created"}],
    )

    result = monitor(_args())

    assert [item["run_id"] for item in result["runs"]] == [101, 102, 103]
    assert result["runs"][0]["created_at"] == "2026-04-07T02:00:00+00:00"
    assert [item["action"] for item in result["runs"]] == [
        "processed-failure",
        "processed-failure",
        "skip-non-failure",
    ]
    assert mock_run_pipeline.call_count == 2
    assert result["runs"][0]["job_outcomes"] == [
        {"job_name": "test-job", "failure_identifier": "test-id", "outcome": "pr-created"},
    ]
    assert result["has_queued_failures"] is False
    state_store.mark_seen.assert_called_once_with(
        "valkey-io/valkey:daily.yml:schedule",
        last_seen_run_id=103,
        target_repo="valkey-io/valkey",
        workflow_file="daily.yml",
        event="schedule",
    )
    state_store.save.assert_called_once()
    _mock_event_ledger.record.assert_any_call(
        "monitor.failure_processed",
        "valkey-io/valkey:daily.yml:101",
        failure_reports=1,
        job_outcome_count=1,
        allow_pr_creation=True,
    )
    _mock_event_ledger.record.assert_any_call(
        "monitor.run_skipped",
        "valkey-io/valkey:daily.yml:103",
        reason="non-failure",
    )
    _mock_event_ledger.save.assert_called_once()


@patch("scripts.monitor_workflow_runs.run_pipeline")
@patch("scripts.monitor_workflow_runs.Github")
@patch("scripts.monitor_workflow_runs.MonitorStateStore")
def test_monitor_dry_run_does_not_process_or_advance_state(
    mock_state_store_cls,
    mock_github_cls,
    mock_run_pipeline,
    _mock_event_ledger,
) -> None:
    state_store = mock_state_store_cls.return_value
    state_store.get_last_seen_run_id.return_value = 100

    workflow = MagicMock()
    workflow.get_runs.return_value = [_run(101, "failure")]
    repo = MagicMock()
    repo.get_workflow.return_value = workflow
    repo.get_contents.side_effect = GithubException(404, {"message": "missing state"})
    mock_github_cls.return_value.get_repo.return_value = repo

    result = monitor(_args(dry_run=True))

    assert result["runs"][0]["action"] == "would-process-failure"
    mock_run_pipeline.assert_not_called()
    state_store.mark_seen.assert_not_called()
    state_store.save.assert_not_called()
    _mock_event_ledger.record.assert_any_call(
        "monitor.run_dry_run",
        "valkey-io/valkey:daily.yml:101",
        reason="failure-observed",
    )


@patch("scripts.monitor_workflow_runs.run_pipeline")
@patch("scripts.monitor_workflow_runs.Github")
@patch("scripts.monitor_workflow_runs.MonitorStateStore")
def test_monitor_advances_watermark_on_pipeline_error(
    mock_state_store_cls,
    mock_github_cls,
    mock_run_pipeline,
    _mock_event_ledger,
) -> None:
    """Pipeline errors should advance the watermark so the monitor does not
    get stuck retrying a permanently failing run on every invocation."""
    state_store = mock_state_store_cls.return_value
    state_store.get_last_seen_run_id.return_value = 100

    workflow = MagicMock()
    workflow.get_runs.return_value = [_run(101, "failure"), _run(102, "failure")]
    repo = MagicMock()
    repo.get_workflow.return_value = workflow
    repo.get_contents.side_effect = GithubException(404, {"message": "missing state"})
    mock_github_cls.return_value.get_repo.return_value = repo
    mock_run_pipeline.side_effect = RuntimeError("boom")

    result = monitor(_args())

    # Both runs should be attempted (continue, not break).
    assert len(result["runs"]) == 2
    assert result["runs"][0]["action"] == "pipeline-error"
    assert result["runs"][1]["action"] == "pipeline-error"
    # Watermark should advance past both failed runs.
    assert result["new_last_seen_run_id"] == 102
    state_store.mark_seen.assert_called_once()
    state_store.save.assert_called_once()
    _mock_event_ledger.record.assert_any_call(
        "monitor.pipeline_error",
        "valkey-io/valkey:daily.yml:101",
        error="boom",
    )


@patch("scripts.monitor_workflow_runs.run_pipeline")
@patch("scripts.monitor_workflow_runs.Github")
@patch("scripts.monitor_workflow_runs.MonitorStateStore")
def test_monitor_passes_queue_only_to_pipeline(
    mock_state_store_cls,
    mock_github_cls,
    mock_run_pipeline,
    _mock_event_ledger,
) -> None:
    state_store = mock_state_store_cls.return_value
    state_store.get_last_seen_run_id.return_value = 100

    workflow = MagicMock()
    workflow.get_runs.return_value = [_run(101, "failure")]
    repo = MagicMock()
    repo.get_workflow.return_value = workflow
    repo.get_contents.side_effect = GithubException(404, {"message": "missing state"})
    mock_github_cls.return_value.get_repo.return_value = repo
    mock_run_pipeline.return_value = PipelineResult(reports=[MagicMock()], job_outcomes=[])

    monitor(_args(queue_only=True))

    _, kwargs = mock_run_pipeline.call_args
    assert kwargs["allow_pr_creation"] is False
    assert kwargs["state_github_token"] == "state-token"
    assert kwargs["state_repo_name"] == "owner/valkey-ci-agent"


def test_parse_args_supports_repeated_and_comma_separated_events() -> None:
    args = parse_args(
        [
            "--target-repo",
            "valkey-io/valkey",
            "--workflow-file",
            "ci.yml",
            "--event",
            "pull_request,push",
            "--event",
            "schedule",
            "--config",
            ".github/valkey-daily-bot.yml",
            "--target-token",
            "target-token",
            "--state-token",
            "state-token",
            "--state-repo",
            "owner/valkey-ci-agent",
        ]
    )

    assert args.events == ("pull_request", "push", "schedule")


@patch("scripts.monitor_workflow_runs.run_pipeline")
@patch("scripts.monitor_workflow_runs.Github")
@patch("scripts.monitor_workflow_runs.MonitorStateStore")
def test_monitor_merges_runs_across_multiple_events(
    mock_state_store_cls,
    mock_github_cls,
    mock_run_pipeline,
    _mock_event_ledger,
) -> None:
    state_store = mock_state_store_cls.return_value
    state_store.get_last_seen_run_id.return_value = 100

    workflow = MagicMock()

    def _get_runs(*, event: str, status: str):
        assert status == "completed"
        if event == "pull_request":
            pr_run = _run(103, "failure")
            pr_run.event = "pull_request"
            return [pr_run]
        push_run = _run(104, "failure")
        push_run.event = "push"
        return [push_run]

    workflow.get_runs.side_effect = _get_runs
    repo = MagicMock()
    repo.get_workflow.return_value = workflow
    repo.get_contents.side_effect = GithubException(404, {"message": "missing state"})
    mock_github_cls.return_value.get_repo.return_value = repo
    mock_run_pipeline.return_value = PipelineResult(reports=[MagicMock()], job_outcomes=[])

    result = monitor(
        _args(
            workflow_file="ci.yml",
            events=("pull_request", "push"),
        )
    )

    assert [item["run_id"] for item in result["runs"]] == [103, 104]
    assert [item["event"] for item in result["runs"]] == ["pull_request", "push"]
    state_store.mark_seen.assert_called_once_with(
        "valkey-io/valkey:ci.yml:pull_request,push",
        last_seen_run_id=104,
        target_repo="valkey-io/valkey",
        workflow_file="ci.yml",
        event="pull_request,push",
    )

"""Tests for centralized workflow monitoring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts.monitor_workflow_runs import MonitorArgs, monitor


def _args(**overrides) -> MonitorArgs:
    defaults = dict(
        target_repo="valkey-io/valkey",
        workflow_file="daily.yml",
        event="schedule",
        config_path=".github/valkey-daily-bot.yml",
        target_token="target-token",
        state_token="state-token",
        state_repo="owner/valkey-ci-bot",
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
    run.conclusion = conclusion
    run.head_sha = f"sha-{run_id}"
    run.html_url = f"https://github.com/valkey-io/valkey/actions/runs/{run_id}"
    return run


@patch("scripts.monitor_workflow_runs.run_pipeline")
@patch("scripts.monitor_workflow_runs.Github")
@patch("scripts.monitor_workflow_runs.MonitorStateStore")
def test_monitor_processes_only_new_failed_runs_and_updates_watermark(
    mock_state_store_cls,
    mock_github_cls,
    mock_run_pipeline,
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
    mock_github_cls.return_value.get_repo.return_value = repo
    mock_run_pipeline.return_value = [object()]

    result = monitor(_args())

    assert [item["run_id"] for item in result["runs"]] == [101, 102, 103]
    assert [item["action"] for item in result["runs"]] == [
        "processed-failure",
        "processed-failure",
        "skip-non-failure",
    ]
    assert mock_run_pipeline.call_count == 2
    assert result["has_queued_failures"] is False
    state_store.mark_seen.assert_called_once_with(
        "valkey-io/valkey:daily.yml:schedule",
        last_seen_run_id=103,
        target_repo="valkey-io/valkey",
        workflow_file="daily.yml",
        event="schedule",
    )
    state_store.save.assert_called_once()


@patch("scripts.monitor_workflow_runs.run_pipeline")
@patch("scripts.monitor_workflow_runs.Github")
@patch("scripts.monitor_workflow_runs.MonitorStateStore")
def test_monitor_dry_run_does_not_process_or_advance_state(
    mock_state_store_cls,
    mock_github_cls,
    mock_run_pipeline,
) -> None:
    state_store = mock_state_store_cls.return_value
    state_store.get_last_seen_run_id.return_value = 100

    workflow = MagicMock()
    workflow.get_runs.return_value = [_run(101, "failure")]
    repo = MagicMock()
    repo.get_workflow.return_value = workflow
    mock_github_cls.return_value.get_repo.return_value = repo

    result = monitor(_args(dry_run=True))

    assert result["runs"][0]["action"] == "would-process-failure"
    mock_run_pipeline.assert_not_called()
    state_store.mark_seen.assert_not_called()
    state_store.save.assert_not_called()


@patch("scripts.monitor_workflow_runs.run_pipeline")
@patch("scripts.monitor_workflow_runs.Github")
@patch("scripts.monitor_workflow_runs.MonitorStateStore")
def test_monitor_stops_without_advancing_failed_run_on_pipeline_error(
    mock_state_store_cls,
    mock_github_cls,
    mock_run_pipeline,
) -> None:
    state_store = mock_state_store_cls.return_value
    state_store.get_last_seen_run_id.return_value = 100

    workflow = MagicMock()
    workflow.get_runs.return_value = [_run(101, "failure"), _run(102, "failure")]
    repo = MagicMock()
    repo.get_workflow.return_value = workflow
    mock_github_cls.return_value.get_repo.return_value = repo
    mock_run_pipeline.side_effect = RuntimeError("boom")

    result = monitor(_args())

    assert result["runs"][0]["action"] == "pipeline-error"
    state_store.mark_seen.assert_not_called()
    state_store.save.assert_not_called()


@patch("scripts.monitor_workflow_runs.run_pipeline")
@patch("scripts.monitor_workflow_runs.Github")
@patch("scripts.monitor_workflow_runs.MonitorStateStore")
def test_monitor_passes_queue_only_to_pipeline(
    mock_state_store_cls,
    mock_github_cls,
    mock_run_pipeline,
) -> None:
    state_store = mock_state_store_cls.return_value
    state_store.get_last_seen_run_id.return_value = 100

    workflow = MagicMock()
    workflow.get_runs.return_value = [_run(101, "failure")]
    repo = MagicMock()
    repo.get_workflow.return_value = workflow
    mock_github_cls.return_value.get_repo.return_value = repo
    mock_run_pipeline.return_value = [object()]

    monitor(_args(queue_only=True))

    _, kwargs = mock_run_pipeline.call_args
    assert kwargs["allow_pr_creation"] is False
    assert kwargs["state_github_token"] == "state-token"
    assert kwargs["state_repo_name"] == "owner/valkey-ci-bot"

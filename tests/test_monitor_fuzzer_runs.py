"""Tests for centralized Valkey fuzzer workflow monitoring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts.models import FuzzerSignal
from scripts.monitor_fuzzer_runs import MonitorArgs, monitor


def _args(**overrides) -> MonitorArgs:
    defaults = dict(
        target_repo="valkey-io/valkey-fuzzer",
        workflow_file="fuzzer-run.yml",
        event="schedule",
        config_path=".github/valkey-fuzzer-bot.yml",
        target_token="target-token",
        state_token="state-token",
        state_repo="owner/valkey-ci-agent",
        max_runs=6,
        aws_region="us-east-1",
        dry_run=False,
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
    run.html_url = f"https://github.com/valkey-io/valkey-fuzzer/actions/runs/{run_id}"
    return run


@patch("scripts.monitor_fuzzer_runs._make_bedrock_client")
@patch("scripts.monitor_fuzzer_runs.FuzzerIssuePublisher")
@patch("scripts.monitor_fuzzer_runs.FuzzerRunAnalyzer")
@patch("scripts.monitor_fuzzer_runs.Github")
@patch("scripts.monitor_fuzzer_runs.MonitorStateStore")
def test_monitor_analyzes_new_runs_and_updates_watermark(
    mock_state_store_cls,
    mock_github_cls,
    mock_analyzer_cls,
    mock_issue_publisher_cls,
    mock_make_bedrock_client,
) -> None:
    state_store = mock_state_store_cls.return_value
    state_store.get_last_seen_run_id.return_value = 100

    workflow = MagicMock()
    workflow.get_runs.return_value = [_run(102, "failure"), _run(101, "success")]
    repo = MagicMock()
    repo.get_workflow.return_value = workflow
    mock_github_cls.return_value.get_repo.return_value = repo
    mock_make_bedrock_client.return_value = (MagicMock(), None)
    mock_issue_publisher_cls.return_value.upsert_issue.return_value = (
        "created",
        "https://github.com/valkey-io/valkey-fuzzer/issues/1",
    )

    analyzer = mock_analyzer_cls.return_value
    analyzer.analyze_workflow_run.side_effect = [
        MagicMock(
            run_id=101,
            run_url="https://example.com/101",
            conclusion="success",
            overall_status="normal",
            scenario_id="seed-101",
            seed="101",
            anomalies=[],
            normal_signals=["ok"],
            summary="Healthy run.",
            reproduction_hint="valkey-fuzzer cluster --seed 101",
        ),
        MagicMock(
            run_id=102,
            run_url="https://example.com/102",
            conclusion="failure",
            overall_status="anomalous",
            scenario_id="seed-102",
            seed="102",
            anomalies=[FuzzerSignal(title="Slot coverage drop", severity="high", evidence="coverage fell below threshold")],
            normal_signals=[],
            summary="Slot coverage failed.",
            reproduction_hint="valkey-fuzzer cluster --seed 102",
        ),
    ]

    result = monitor(_args())

    assert [item["run_id"] for item in result["runs"]] == [101, 102]
    assert [item["action"] for item in result["runs"]] == ["analyzed", "analyzed"]
    assert analyzer.analyze_workflow_run.call_count == 2
    assert "issue_action" not in result["runs"][0]
    assert result["runs"][1]["issue_action"] == "created"
    assert result["has_anomalies"] is True
    mock_issue_publisher_cls.return_value.upsert_issue.assert_called_once()
    state_store.mark_seen.assert_called_once_with(
        "valkey-io/valkey-fuzzer:fuzzer-run.yml:schedule",
        last_seen_run_id=102,
        target_repo="valkey-io/valkey-fuzzer",
        workflow_file="fuzzer-run.yml",
        event="schedule",
    )
    state_store.save.assert_called_once()


@patch("scripts.monitor_fuzzer_runs._make_bedrock_client")
@patch("scripts.monitor_fuzzer_runs.FuzzerIssuePublisher")
@patch("scripts.monitor_fuzzer_runs.FuzzerRunAnalyzer")
@patch("scripts.monitor_fuzzer_runs.Github")
@patch("scripts.monitor_fuzzer_runs.MonitorStateStore")
def test_monitor_dry_run_does_not_analyze_or_advance_state(
    mock_state_store_cls,
    mock_github_cls,
    mock_analyzer_cls,
    mock_issue_publisher_cls,
    mock_make_bedrock_client,
) -> None:
    state_store = mock_state_store_cls.return_value
    state_store.get_last_seen_run_id.return_value = 100

    workflow = MagicMock()
    workflow.get_runs.return_value = [_run(101, "success")]
    repo = MagicMock()
    repo.get_workflow.return_value = workflow
    mock_github_cls.return_value.get_repo.return_value = repo
    mock_make_bedrock_client.return_value = (MagicMock(), None)

    result = monitor(_args(dry_run=True))

    assert result["runs"][0]["action"] == "would-analyze"
    mock_analyzer_cls.return_value.analyze_workflow_run.assert_not_called()
    mock_issue_publisher_cls.return_value.upsert_issue.assert_not_called()
    state_store.mark_seen.assert_not_called()
    state_store.save.assert_not_called()


@patch("scripts.monitor_fuzzer_runs._make_bedrock_client")
@patch("scripts.monitor_fuzzer_runs.FuzzerIssuePublisher")
@patch("scripts.monitor_fuzzer_runs.FuzzerRunAnalyzer")
@patch("scripts.monitor_fuzzer_runs.Github")
@patch("scripts.monitor_fuzzer_runs.MonitorStateStore")
def test_monitor_advances_watermark_on_analysis_error_and_continues(
    mock_state_store_cls,
    mock_github_cls,
    mock_analyzer_cls,
    mock_issue_publisher_cls,
    mock_make_bedrock_client,
) -> None:
    state_store = mock_state_store_cls.return_value
    state_store.get_last_seen_run_id.return_value = 100

    workflow = MagicMock()
    workflow.get_runs.return_value = [_run(102, "failure"), _run(101, "failure")]
    repo = MagicMock()
    repo.get_workflow.return_value = workflow
    mock_github_cls.return_value.get_repo.return_value = repo
    mock_make_bedrock_client.return_value = (MagicMock(), None)

    analyzer = mock_analyzer_cls.return_value
    analyzer.analyze_workflow_run.side_effect = [
        RuntimeError("boom"),
        MagicMock(
            run_id=102,
            run_url="https://example.com/102",
            conclusion="failure",
            overall_status="normal",
            scenario_id="seed-102",
            seed="102",
            anomalies=[],
            normal_signals=[],
            summary="Recovered.",
            reproduction_hint="valkey-fuzzer cluster --seed 102",
        ),
    ]

    result = monitor(_args())

    assert [item["run_id"] for item in result["runs"]] == [101, 102]
    assert [item["action"] for item in result["runs"]] == ["analysis-error", "analyzed"]
    assert result["new_run_count"] == 2
    assert mock_issue_publisher_cls.return_value.upsert_issue.call_count == 0
    state_store.mark_seen.assert_called_once_with(
        "valkey-io/valkey-fuzzer:fuzzer-run.yml:schedule",
        last_seen_run_id=102,
        target_repo="valkey-io/valkey-fuzzer",
        workflow_file="fuzzer-run.yml",
        event="schedule",
    )
    state_store.save.assert_called_once()

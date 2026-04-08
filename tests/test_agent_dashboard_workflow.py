"""Regression tests for dashboard workflow wiring."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _get_on_block(workflow: dict) -> dict:
    if "on" in workflow:
        return workflow["on"]
    return workflow[True]


def _step(workflow: dict, name: str) -> dict:
    return next(
        step
        for step in workflow["jobs"]["dashboard"]["steps"]
        if step["name"] == name
    )


def test_dashboard_workflow_generates_static_artifacts() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/agent-dashboard.yml")
    on_block = _get_on_block(workflow)

    assert "schedule" in on_block
    assert "workflow_dispatch" in on_block
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["env"]["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] is True
    assert workflow["jobs"]["dashboard"]["concurrency"]["group"] == (
        "ci-agent-capability-dashboard"
    )

    bot_data_checkout = _step(workflow, "Check out bot data snapshots")
    generate_step = _step(workflow, "Generate capability dashboard")
    upload_step = _step(workflow, "Upload dashboard artifact")

    assert bot_data_checkout["continue-on-error"] is True
    assert bot_data_checkout["with"]["ref"] == "bot-data"
    assert bot_data_checkout["with"]["path"] == "bot-data"
    assert "-m scripts.agent_dashboard" in generate_step["run"]
    assert "--failure-store bot-data/failure-store.json" in generate_step["run"]
    assert "--review-state bot-data/review-state.json" in generate_step["run"]
    assert "--event-log bot-data/agent-events.jsonl" in generate_step["run"]
    assert upload_step["uses"] == "actions/upload-artifact@v4"


def test_monitor_workflows_publish_capability_dashboard() -> None:
    daily = _load_yaml(REPO_ROOT / ".github/workflows/monitor-valkey-daily.yml")
    fuzzer = _load_yaml(REPO_ROOT / ".github/workflows/monitor-valkey-fuzzer.yml")

    daily_steps = daily["jobs"]["monitor"]["steps"]
    fuzzer_steps = fuzzer["jobs"]["monitor"]["steps"]
    daily_generate = next(
        step for step in daily_steps if step["name"] == "Generate capability dashboard"
    )
    fuzzer_generate = next(
        step for step in fuzzer_steps if step["name"] == "Generate capability dashboard"
    )
    daily_upload = next(
        step for step in daily_steps if step["name"] == "Upload capability dashboard"
    )
    fuzzer_upload = next(
        step for step in fuzzer_steps if step["name"] == "Upload capability dashboard"
    )

    assert "--daily-result monitor-result.json" in daily_generate["run"]
    assert "--fuzzer-result fuzzer-monitor-result.json" in fuzzer_generate["run"]
    assert "--event-log bot-data/agent-events.jsonl" in daily_generate["run"]
    assert "--event-log bot-data/agent-events.jsonl" in fuzzer_generate["run"]
    assert daily_upload["uses"] == "actions/upload-artifact@v4"
    assert fuzzer_upload["uses"] == "actions/upload-artifact@v4"

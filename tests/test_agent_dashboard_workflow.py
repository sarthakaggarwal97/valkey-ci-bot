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

    bot_data_checkout = _step(workflow, "Check out agent data snapshots")
    setup_step = _step(workflow, "Set up Python 3.11")
    daily_health_step = _step(workflow, "Generate daily CI health report")
    acceptance_step = _step(workflow, "Run replay acceptance scorecard")
    generate_step = _step(workflow, "Generate capability dashboard")
    site_step = _step(workflow, "Generate observability site")
    upload_step = _step(workflow, "Upload dashboard artifact")

    assert bot_data_checkout["continue-on-error"] is True
    assert bot_data_checkout["with"]["ref"] == "bot-data"
    assert bot_data_checkout["with"]["path"] == "bot-data"
    assert setup_step["uses"] == "actions/setup-python@v6"
    assert "-m scripts.daily_health_report" in daily_health_step["run"]
    assert "-m scripts.valkey_acceptance" in acceptance_step["run"]
    assert "-m scripts.agent_dashboard" in generate_step["run"]
    assert "--failure-store bot-data/failure-store.json" in generate_step["run"]
    assert "--review-state bot-data/review-state.json" in generate_step["run"]
    assert "--event-log bot-data/agent-events.jsonl" in generate_step["run"]
    assert "--acceptance-result acceptance-report.json" in generate_step["run"]
    assert "--daily-health daily-health-report.json" in generate_step["run"]
    assert "--output-html agent-dashboard.html" in generate_step["run"]
    assert "-m scripts.agent_dashboard_site" in site_step["run"]
    assert "--dashboard-json agent-dashboard.json" in site_step["run"]
    assert upload_step["uses"] == "actions/upload-artifact@v4"
    assert "agent-dashboard.html" in upload_step["with"]["path"]
    assert "dashboard-site" in upload_step["with"]["path"]


def test_monitor_workflows_publish_capability_dashboard() -> None:
    daily = _load_yaml(REPO_ROOT / ".github/workflows/monitor-valkey-daily.yml")
    fuzzer = _load_yaml(REPO_ROOT / ".github/workflows/monitor-valkey-fuzzer.yml")

    daily_steps = daily["jobs"]["monitor"]["steps"]
    fuzzer_steps = fuzzer["jobs"]["monitor"]["steps"]
    daily_generate = next(
        step for step in daily_steps if step["name"] == "Generate capability dashboard"
    )
    daily_site = next(
        step for step in daily_steps if step["name"] == "Generate observability site"
    )
    fuzzer_generate = next(
        step for step in fuzzer_steps if step["name"] == "Generate capability dashboard"
    )
    fuzzer_site = next(
        step for step in fuzzer_steps if step["name"] == "Generate observability site"
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
    assert "--output-html agent-dashboard.html" in daily_generate["run"]
    assert "--output-html agent-dashboard.html" in fuzzer_generate["run"]
    assert "-m scripts.agent_dashboard_site" in daily_site["run"]
    assert "-m scripts.agent_dashboard_site" in fuzzer_site["run"]
    assert daily_upload["uses"] == "actions/upload-artifact@v4"
    assert fuzzer_upload["uses"] == "actions/upload-artifact@v4"
    assert "agent-dashboard.html" in daily_upload["with"]["path"]
    assert "agent-dashboard.html" in fuzzer_upload["with"]["path"]
    assert "dashboard-site" in daily_upload["with"]["path"]
    assert "dashboard-site" in fuzzer_upload["with"]["path"]


def test_publish_workflow_builds_pages_site() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/publish-dashboard-site.yml")
    on_block = _get_on_block(workflow)
    build_job = workflow["jobs"]["build"]
    deploy_job = workflow["jobs"]["deploy"]

    assert "workflow_dispatch" in on_block
    assert workflow["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert build_job["concurrency"]["group"] == "publish-dashboard-site"

    build_steps = build_job["steps"]
    generate_dashboard = next(
        step for step in build_steps if step["name"] == "Generate capability dashboard"
    )
    generate_site = next(
        step for step in build_steps if step["name"] == "Generate observability site"
    )
    upload_pages = next(
        step for step in build_steps if step["name"] == "Upload Pages artifact"
    )

    assert "--acceptance-result acceptance-report.json" in generate_dashboard["run"]
    assert "--daily-health daily-health-report.json" in generate_dashboard["run"]
    assert "-m scripts.agent_dashboard_site" in generate_site["run"]
    assert upload_pages["uses"] == "actions/upload-pages-artifact@v4"
    assert upload_pages["with"]["path"] == "dashboard-site"
    assert deploy_job["needs"] == "build"

"""Regression tests for the CI agent replay lab workflow."""

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


def test_replay_lab_runs_acceptance_harness_and_uploads_scorecard() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/agent-replay-lab.yml")
    on_block = _get_on_block(workflow)
    job = workflow["jobs"]["replay-lab"]

    assert "workflow_dispatch" in on_block
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}
    assert workflow["env"]["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] is True

    run_step = next(step for step in job["steps"] if step["name"] == "Run replay lab")
    bot_data_checkout = next(
        step for step in job["steps"] if step["name"] == "Check out agent data snapshots"
    )
    site_step = next(
        step for step in job["steps"] if step["name"] == "Generate observability site"
    )
    upload_step = next(
        step for step in job["steps"] if step["name"] == "Upload replay lab artifacts"
    )

    assert "-m scripts.valkey_acceptance" in run_step["run"]
    assert "--json-output acceptance-report.json" in run_step["run"]
    assert "--run-models" in run_step["run"]
    assert bot_data_checkout["with"]["ref"] == "bot-data"
    assert "-m scripts.agent_dashboard" in site_step["run"]
    assert "-m scripts.agent_dashboard_site" in site_step["run"]
    assert upload_step["uses"] == "actions/upload-artifact@v4"
    assert "dashboard-site" in upload_step["with"]["path"]

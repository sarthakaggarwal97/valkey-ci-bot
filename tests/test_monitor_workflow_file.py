"""Regression tests for the centralized Valkey Daily monitor workflow."""

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


def test_monitor_workflow_uses_oidc_and_app_token_support() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/monitor-valkey-daily.yml")
    on_block = _get_on_block(workflow)
    monitor_job = workflow["jobs"]["monitor"]
    job_env = monitor_job["env"]

    assert workflow["permissions"] == {
        "actions": "write",
        "contents": "write",
        "id-token": "write",
    }
    assert on_block["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    assert workflow["env"]["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] is True
    assert "concurrency" not in workflow
    assert "github.event_name != 'workflow_dispatch'" in job_env["MONITOR_DRY_RUN"]
    assert "VALKEY_GITHUB_TOKEN" in job_env
    assert "VALKEY_GITHUB_APP_ID" in job_env
    assert "VALKEY_GITHUB_APP_PRIVATE_KEY" in job_env
    assert "VALKEY_FORK_REPO" in job_env
    assert "VALKEY_FORK_GITHUB_TOKEN" in job_env
    assert monitor_job["concurrency"]["group"] == "monitor-valkey-daily-scan"
    assert "create-draft-prs" not in workflow["jobs"]

    app_token_step = next(
        step
        for step in monitor_job["steps"]
        if step["name"] == "Create target repository GitHub App token"
    )
    setup_step = next(
        step
        for step in monitor_job["steps"]
        if step["name"] == "Set up Python 3.11"
    )
    configure_step = next(
        step
        for step in monitor_job["steps"]
        if step["name"] == "Configure AWS credentials from OIDC role"
    )
    assert setup_step["uses"] == "actions/setup-python@v6"
    assert configure_step["uses"] == "aws-actions/configure-aws-credentials@v5"
    assert app_token_step["uses"] == "actions/create-github-app-token@v2"
    assert app_token_step["with"]["owner"] == "valkey-io"
    assert app_token_step["with"]["repositories"] == "valkey"
    assert app_token_step["with"]["permission-actions"] == "read"
    assert app_token_step["with"]["permission-contents"] == "write"
    assert app_token_step["with"]["permission-pull-requests"] == "write"

    for step in monitor_job["steps"]:
        if "if" in step:
            assert "secrets." not in step["if"]


def test_monitor_workflow_runs_central_monitor_script() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/monitor-valkey-daily.yml")

    run_step = next(
        step
        for step in workflow["jobs"]["monitor"]["steps"]
        if step["name"] == "Monitor and queue Valkey Daily failures"
    )
    script = run_step["run"]
    assert "-m scripts.monitor_workflow_runs" in script
    assert '--target-repo "valkey-io/valkey"' in script
    assert '--workflow-file "daily.yml"' in script
    assert '--config ".github/valkey-daily-bot.yml"' in script
    assert "--queue-only" in script

    preflight_step = next(
        step
        for step in workflow["jobs"]["monitor"]["steps"]
        if step["name"] == "Validate fork base branches for queued fixes"
    )
    reconcile_step = next(
        step
        for step in workflow["jobs"]["monitor"]["steps"]
        if step["name"] == "Create draft PRs for queued fixes"
    )
    select_fork_token_step = next(
        step
        for step in workflow["jobs"]["monitor"]["steps"]
        if step["name"] == "Select fork repository token"
    )
    preflight_script = preflight_step["run"]
    assert "-m scripts.preflight_reconciliation" in preflight_script
    assert "--repo \"${VALKEY_FORK_REPO}\"" in preflight_script
    assert "steps.capture-monitor.outputs.has_queued_failures" in preflight_step["if"]
    assert "steps.capture-monitor.outputs.has_queued_failures" in select_fork_token_step["if"]

    reconcile_script = reconcile_step["run"]
    assert "--mode reconcile" in reconcile_script
    assert "--repo \"${VALKEY_FORK_REPO}\"" in reconcile_script
    assert "--draft-prs" in reconcile_script

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
    job_env = workflow["jobs"]["monitor"]["env"]
    reconcile_env = workflow["jobs"]["create-approved-prs"]["env"]

    assert workflow["permissions"] == {"contents": "write", "id-token": "write"}
    assert on_block["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    assert workflow["env"]["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] is True
    assert "concurrency" not in workflow
    assert "github.event_name != 'workflow_dispatch'" in job_env["MONITOR_DRY_RUN"]
    assert "VALKEY_GITHUB_TOKEN" in job_env
    assert "VALKEY_GITHUB_APP_ID" in job_env
    assert "VALKEY_GITHUB_APP_PRIVATE_KEY" in job_env
    assert "VALKEY_GITHUB_TOKEN" in reconcile_env
    assert workflow["jobs"]["monitor"]["concurrency"]["group"] == "monitor-valkey-daily-scan"
    assert workflow["jobs"]["create-approved-prs"]["concurrency"]["group"] == "monitor-valkey-daily-approval"

    app_token_step = next(
        step
        for step in workflow["jobs"]["monitor"]["steps"]
        if step["name"] == "Create target repository GitHub App token"
    )
    setup_step = next(
        step
        for step in workflow["jobs"]["monitor"]["steps"]
        if step["name"] == "Set up Python 3.11"
    )
    configure_step = next(
        step
        for step in workflow["jobs"]["monitor"]["steps"]
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
    assert workflow["jobs"]["create-approved-prs"]["environment"]["name"] == "valkey-pr-approval"
    assert workflow["jobs"]["create-approved-prs"]["if"] == "${{ needs.monitor.outputs.effective_dry_run != 'true' && needs.monitor.outputs.has_queued_failures == 'true' }}"

    for job_name in ("monitor", "create-approved-prs"):
        for step in workflow["jobs"][job_name]["steps"]:
            if "if" in step:
                assert "secrets." not in step["if"]


def test_monitor_workflow_runs_central_monitor_script() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/monitor-valkey-daily.yml")

    run_step = next(
        step
        for step in workflow["jobs"]["monitor"]["steps"]
        if step["name"] == "Monitor and process Valkey Daily failures"
    )
    script = run_step["run"]
    assert "-m scripts.monitor_workflow_runs" in script
    assert '--target-repo "valkey-io/valkey"' in script
    assert '--workflow-file "daily.yml"' in script
    assert '--config ".github/valkey-daily-bot.yml"' in script
    assert "--queue-only" in script

    reconcile_step = next(
        step
        for step in workflow["jobs"]["create-approved-prs"]["steps"]
        if step["name"] == "Create approved Valkey PRs"
    )
    reconcile_script = reconcile_step["run"]
    assert "--mode reconcile" in reconcile_script
    assert '--state-repo "${{ github.repository }}"' in reconcile_script

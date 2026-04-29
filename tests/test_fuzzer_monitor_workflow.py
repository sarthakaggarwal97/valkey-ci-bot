"""Regression tests for the centralized Valkey fuzzer monitor workflow."""

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


def test_fuzzer_monitor_workflow_uses_oidc_and_app_token_support() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/monitor-valkey-fuzzer.yml")
    on_block = _get_on_block(workflow)
    job_env = workflow["jobs"]["monitor"]["env"]

    assert workflow["permissions"] == {"contents": "write", "id-token": "write"}
    assert on_block["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    assert workflow["env"]["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] is True
    assert workflow["jobs"]["monitor"]["concurrency"]["group"] == "monitor-valkey-fuzzer-scan"
    assert "github.event_name != 'workflow_dispatch'" in job_env["MONITOR_DRY_RUN"]
    assert "VALKEY_GITHUB_APP_ID" in job_env
    assert "VALKEY_GITHUB_TOKEN" not in job_env
    assert "VALKEY_GITHUB_APP_PRIVATE_KEY" not in job_env

    app_token_step = next(
        step
        for step in workflow["jobs"]["monitor"]["steps"]
        if step["name"] == "Create target repository GitHub App token"
    )
    configure_step = next(
        step
        for step in workflow["jobs"]["monitor"]["steps"]
        if step["name"] == "Configure AWS credentials from OIDC role"
    )
    assert app_token_step["uses"] == "actions/create-github-app-token@v2"
    assert app_token_step["if"] == "${{ steps.target-auth.outputs.use-app == 'true' }}"
    assert app_token_step["with"]["owner"] == "valkey-io"
    assert app_token_step["with"]["repositories"] == "valkey-fuzzer"
    assert app_token_step["with"]["permission-actions"] == "read"
    assert app_token_step["with"]["permission-contents"] == "read"
    assert app_token_step["with"]["permission-issues"] == "write"
    assert configure_step["uses"] == "aws-actions/configure-aws-credentials@v5"


def test_fuzzer_monitor_workflow_runs_analysis_script() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/monitor-valkey-fuzzer.yml")

    run_step = next(
        step
        for step in workflow["jobs"]["monitor"]["steps"]
        if step["name"] == "Analyze Valkey fuzzer runs"
    )
    script = run_step["run"]
    assert "-m scripts.monitor_fuzzer_runs" in script
    assert '--target-repo "valkey-io/valkey-fuzzer"' in script
    assert '--workflow-file "fuzzer-run.yml"' in script
    assert '--config ".github/valkey-fuzzer-bot.yml"' in script

    upload_step = next(
        step
        for step in workflow["jobs"]["monitor"]["steps"]
        if step["name"] == "Upload fuzzer analysis result"
    )
    assert upload_step["uses"] == "actions/upload-artifact@v4"

"""Regression tests for the Bedrock KB refresh workflow wiring."""

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


def test_refresh_workflow_uses_oidc_and_dry_run_dispatch_default() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/refresh-bedrock-kb.yml")
    on_block = _get_on_block(workflow)
    dispatch_inputs = on_block["workflow_dispatch"]["inputs"]

    assert dispatch_inputs["dry_run"]["default"] is True
    assert dispatch_inputs["missing_only"]["default"] is False
    assert dispatch_inputs["skip_web_sync"]["default"] is False
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}

    configure_step = next(
        step
        for step in workflow["jobs"]["refresh"]["steps"]
        if step["name"] == "Configure AWS credentials from OIDC role"
    )
    assert configure_step["uses"] == "aws-actions/configure-aws-credentials@v4"
    assert configure_step["with"]["role-to-assume"] == "${{ secrets.AWS_ROLE_ARN }}"


def test_refresh_workflow_runs_refresh_script() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/refresh-bedrock-kb.yml")

    run_step = next(
        step
        for step in workflow["jobs"]["refresh"]["steps"]
        if step["name"] == "Refresh Bedrock knowledge bases"
    )
    run_script = run_step["run"]
    assert "scripts/bedrock_kb_refresh.py" in run_script
    assert "--dry-run" in run_script
    assert "--missing-only" in run_script
    assert "--skip-web-sync" in run_script

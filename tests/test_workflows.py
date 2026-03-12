"""Regression tests for reusable workflow wiring."""

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


def test_analyze_workflow_checks_out_bot_repository() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/analyze-failure.yml")
    on_block = _get_on_block(workflow)
    inputs = on_block["workflow_call"]["inputs"]
    secrets = on_block["workflow_call"]["secrets"]

    assert "bot_repository" in inputs
    assert "bot_ref" in inputs
    assert "aws_region" in inputs
    assert "AWS_ROLE_ARN" in secrets
    assert "GITHUB_TOKEN" not in secrets

    checkout_step = next(
        step
        for step in workflow["jobs"]["run-pipeline"]["steps"]
        if step["name"] == "Checkout bot repository"
    )
    assert checkout_step["with"]["repository"] == "${{ inputs.bot_repository }}"
    assert checkout_step["with"]["ref"] == "${{ inputs.bot_ref }}"
    assert checkout_step["uses"] == "actions/checkout@v6"

    assert workflow["jobs"]["run-pipeline"]["permissions"]["id-token"] == "write"

    role_step = next(
        step
        for step in workflow["jobs"]["run-pipeline"]["steps"]
        if step["name"] == "Configure AWS credentials from OIDC role"
    )
    assert role_step["with"]["role-to-assume"] == "${{ secrets.AWS_ROLE_ARN }}"
    assert role_step["with"]["aws-region"] == "${{ inputs.aws_region }}"

    analyze_step = next(
        step
        for step in workflow["jobs"]["run-pipeline"]["steps"]
        if step["name"] == "Run analysis pipeline"
    )
    assert "python -m scripts.main" in analyze_step["run"]


def test_example_caller_passes_bot_checkout_inputs() -> None:
    workflow = _load_yaml(REPO_ROOT / "examples/caller-workflow.yml")

    analyze_with = workflow["jobs"]["analyze"]["with"]
    assert analyze_with["bot_repository"] == "valkey-io/valkey-ci-bot"
    assert analyze_with["bot_ref"] == "v1"
    assert analyze_with["aws_region"] == "${{ vars.CI_BOT_AWS_REGION || 'us-east-1' }}"

    reconcile_with = workflow["jobs"]["reconcile"]["with"]
    assert reconcile_with["bot_repository"] == "valkey-io/valkey-ci-bot"
    assert reconcile_with["bot_ref"] == "v1"
    assert reconcile_with["aws_region"] == "${{ vars.CI_BOT_AWS_REGION || 'us-east-1' }}"

    analyze_secrets = workflow["jobs"]["analyze"]["secrets"]
    assert analyze_secrets["AWS_ROLE_ARN"] == "${{ secrets.CI_BOT_AWS_ROLE_ARN }}"
    assert "GITHUB_TOKEN" not in analyze_secrets


def test_ci_workflow_declares_checkout_permissions_and_current_action() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/ci.yml")

    assert workflow["permissions"] == {"contents": "read"}

    checkout_step = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step["name"] == "Checkout repository"
    )
    assert checkout_step["uses"] == "actions/checkout@v6"
    assert checkout_step["with"]["persist-credentials"] is False

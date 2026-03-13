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

    # bot_repository/bot_ref removed for security — workflow always
    # checks out its own repository at the called ref.
    assert "bot_repository" not in inputs
    assert "bot_ref" not in inputs
    assert "aws_region" in inputs
    assert "AWS_ROLE_ARN" in secrets
    assert "GITHUB_TOKEN" not in secrets
    assert workflow["env"]["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] is True

    checkout_step = next(
        step
        for step in workflow["jobs"]["run-pipeline"]["steps"]
        if step["name"] == "Checkout bot repository"
    )
    # No repository/ref override — uses the called workflow's own repo
    assert "repository" not in checkout_step.get("with", {})
    assert "ref" not in checkout_step.get("with", {})
    assert checkout_step["uses"] == "actions/checkout@v6"

    assert workflow["jobs"]["run-pipeline"]["permissions"]["id-token"] == "write"

    role_step = next(
        step
        for step in workflow["jobs"]["run-pipeline"]["steps"]
        if step["name"] == "Configure AWS credentials from OIDC role"
    )
    setup_step = next(
        step
        for step in workflow["jobs"]["run-pipeline"]["steps"]
        if step["name"] == "Set up Python 3.11"
    )
    assert setup_step["uses"] == "actions/setup-python@v6"
    assert role_step["uses"] == "aws-actions/configure-aws-credentials@v5"
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
    assert "bot_repository" not in analyze_with
    assert "bot_ref" not in analyze_with
    assert analyze_with["aws_region"] == "${{ vars.CI_BOT_AWS_REGION || 'us-east-1' }}"

    reconcile_with = workflow["jobs"]["reconcile"]["with"]
    assert "bot_repository" not in reconcile_with
    assert "bot_ref" not in reconcile_with
    assert reconcile_with["aws_region"] == "${{ vars.CI_BOT_AWS_REGION || 'us-east-1' }}"

    analyze_secrets = workflow["jobs"]["analyze"]["secrets"]
    assert analyze_secrets["AWS_ROLE_ARN"] == "${{ secrets.CI_BOT_AWS_ROLE_ARN }}"
    assert "GITHUB_TOKEN" not in analyze_secrets


def test_ci_workflow_declares_checkout_permissions_and_current_action() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/ci.yml")

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["env"]["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] is True

    checkout_step = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step["name"] == "Checkout repository"
    )
    setup_step = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step["name"] == "Set up Python 3.11"
    )
    assert checkout_step["uses"] == "actions/checkout@v6"
    assert checkout_step["with"]["persist-credentials"] is False
    assert setup_step["uses"] == "actions/setup-python@v6"

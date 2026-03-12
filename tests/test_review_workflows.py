"""Regression tests for PR reviewer workflow wiring."""

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


def test_review_workflow_checks_out_bot_repository() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/review-pr.yml")
    on_block = _get_on_block(workflow)
    inputs = on_block["workflow_call"]["inputs"]
    secrets = on_block["workflow_call"]["secrets"]

    assert "bot_repository" in inputs
    assert "bot_ref" in inputs
    assert "aws_region" in inputs
    assert "AWS_ROLE_ARN" in secrets

    checkout_step = next(
        step
        for step in workflow["jobs"]["review"]["steps"]
        if step["name"] == "Checkout bot repository"
    )
    assert checkout_step["with"]["repository"] == "${{ inputs.bot_repository }}"
    assert checkout_step["with"]["ref"] == "${{ inputs.bot_ref }}"
    assert checkout_step["uses"] == "actions/checkout@v6"

    assert workflow["jobs"]["review"]["permissions"]["id-token"] == "write"

    role_step = next(
        step
        for step in workflow["jobs"]["review"]["steps"]
        if step["name"] == "Configure AWS credentials from OIDC role"
    )
    assert role_step["with"]["role-to-assume"] == "${{ secrets.AWS_ROLE_ARN }}"
    assert role_step["with"]["aws-region"] == "${{ inputs.aws_region }}"


def test_example_pr_review_caller_passes_bot_checkout_inputs() -> None:
    workflow = _load_yaml(REPO_ROOT / "examples/pr-review-caller-workflow.yml")

    review_with = workflow["jobs"]["review"]["with"]
    assert review_with["bot_repository"] == "valkey-io/valkey-ci-bot"
    assert review_with["bot_ref"] == "v1"
    assert review_with["aws_region"] == "${{ vars.CI_BOT_AWS_REGION || 'us-east-1' }}"

    review_secrets = workflow["jobs"]["review"]["secrets"]
    assert review_secrets["AWS_ROLE_ARN"] == "${{ secrets.CI_BOT_AWS_ROLE_ARN }}"

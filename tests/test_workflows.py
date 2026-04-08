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

    # The called workflow resolves its own repository/ref from the OIDC
    # job_workflow_ref claim so actions/checkout does not default to the
    # caller repository.
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
    resolve_step = next(
        step
        for step in workflow["jobs"]["run-pipeline"]["steps"]
        if step["name"] == "Resolve called workflow ref"
    )
    assert resolve_step["uses"] == "actions/github-script@v7"
    assert "job_workflow_ref" in resolve_step["with"]["script"]
    assert checkout_step["with"]["repository"] == "${{ steps.called-workflow.outputs.repository }}"
    assert checkout_step["with"]["ref"] == "${{ steps.called-workflow.outputs.ref }}"
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


def test_backport_workflow_contract_and_token_handling() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/backport.yml")
    on_block = _get_on_block(workflow)
    inputs = on_block["workflow_call"]["inputs"]
    secrets = on_block["workflow_call"]["secrets"]

    assert inputs["aws_region"]["default"] == "us-east-1"
    assert inputs["agent_repository"]["default"] == "sarthakaggarwal97/valkey-ci-agent"
    assert inputs["agent_ref"]["default"] == "main"
    assert secrets["AWS_ROLE_ARN"]["required"] is True
    assert secrets["VALKEY_GITHUB_TOKEN"]["required"] is False
    assert workflow["env"]["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] is True
    assert "AWS_REGION" not in workflow["env"]

    job = workflow["jobs"]["backport"]
    assert job["timeout-minutes"] == 60
    assert job["concurrency"]["group"] == (
        "backport-${{ inputs.repo_full_name }}-"
        "${{ inputs.source_pr_number }}-${{ inputs.target_branch }}"
    )
    assert job["concurrency"]["cancel-in-progress"] is False

    role_step = next(
        step
        for step in job["steps"]
        if step["name"] == "Configure AWS credentials from OIDC role"
    )
    checkout_step = next(
        step
        for step in job["steps"]
        if step["name"] == "Checkout agent repository"
    )
    run_step = next(
        step
        for step in job["steps"]
        if step["name"] == "Run backport pipeline"
    )

    assert checkout_step["with"]["repository"] == "${{ inputs.agent_repository }}"
    assert checkout_step["with"]["ref"] == "${{ inputs.agent_ref }}"
    assert role_step["with"]["role-to-assume"] == "${{ secrets.AWS_ROLE_ARN }}"
    assert role_step["with"]["aws-region"] == "${{ inputs.aws_region }}"
    assert run_step["env"]["AWS_DEFAULT_REGION"] == "${{ inputs.aws_region }}"
    assert run_step["env"]["BACKPORT_GITHUB_TOKEN"] == (
        "${{ secrets.VALKEY_GITHUB_TOKEN || github.token }}"
    )
    assert "--token" not in run_step["run"]
    assert '--aws-region "${{ inputs.aws_region }}"' in run_step["run"]


def test_example_caller_passes_bot_checkout_inputs() -> None:
    workflow = _load_yaml(REPO_ROOT / "examples/caller-workflow.yml")

    assert workflow["permissions"]["id-token"] == "write"

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


def test_example_backport_caller_passes_required_contract() -> None:
    workflow = _load_yaml(REPO_ROOT / "examples/backport-caller-workflow.yml")

    extract_script = workflow["jobs"]["preflight"]["steps"][0]["with"]["script"]
    assert "context.payload.label?.name" in extract_script
    assert "pr.labels" not in extract_script

    backport_with = workflow["jobs"]["backport"]["with"]
    assert backport_with["aws_region"] == "${{ vars.CI_BOT_AWS_REGION || 'us-east-1' }}"
    assert backport_with["agent_repository"] == "sarthakaggarwal97/valkey-ci-agent"
    assert backport_with["agent_ref"] == "main"

    backport_secrets = workflow["jobs"]["backport"]["secrets"]
    assert backport_secrets["AWS_ROLE_ARN"] == "${{ secrets.CI_BOT_AWS_ROLE_ARN }}"
    assert "VALKEY_GITHUB_TOKEN" not in backport_secrets


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

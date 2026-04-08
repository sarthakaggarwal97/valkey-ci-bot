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
        for step in workflow["jobs"]["review"]["steps"]
        if step["name"] == "Checkout bot repository"
    )
    resolve_step = next(
        step
        for step in workflow["jobs"]["review"]["steps"]
        if step["name"] == "Resolve called workflow ref"
    )
    assert resolve_step["uses"] == "actions/github-script@v7"
    assert "job_workflow_ref" in resolve_step["with"]["script"]
    assert checkout_step["with"]["repository"] == "${{ steps.called-workflow.outputs.repository }}"
    assert checkout_step["with"]["ref"] == "${{ steps.called-workflow.outputs.ref }}"
    assert checkout_step["uses"] == "actions/checkout@v6"

    assert workflow["jobs"]["review"]["permissions"]["id-token"] == "write"
    assert workflow["jobs"]["review"]["timeout-minutes"] == 45
    assert workflow["jobs"]["review"]["concurrency"]["group"] == (
        "review-pr-${{ github.repository }}-"
        "${{ github.event.pull_request.number || github.event.issue.number || github.run_id }}"
    )
    assert workflow["jobs"]["review"]["concurrency"]["cancel-in-progress"] is False

    role_step = next(
        step
        for step in workflow["jobs"]["review"]["steps"]
        if step["name"] == "Configure AWS credentials from OIDC role"
    )
    setup_step = next(
        step
        for step in workflow["jobs"]["review"]["steps"]
        if step["name"] == "Set up Python 3.11"
    )
    assert setup_step["uses"] == "actions/setup-python@v6"
    assert role_step["uses"] == "aws-actions/configure-aws-credentials@v5"
    assert role_step["with"]["role-to-assume"] == "${{ secrets.AWS_ROLE_ARN }}"
    assert role_step["with"]["aws-region"] == "${{ inputs.aws_region }}"

    review_step = next(
        step
        for step in workflow["jobs"]["review"]["steps"]
        if step["name"] == "Run PR reviewer"
    )
    assert "python -m scripts.pr_review_main" in review_step["run"]


def test_example_pr_review_caller_passes_bot_checkout_inputs() -> None:
    workflow = _load_yaml(REPO_ROOT / "examples/pr-review-caller-workflow.yml")

    assert workflow["permissions"]["id-token"] == "write"

    review_with = workflow["jobs"]["review"]["with"]
    assert "bot_repository" not in review_with
    assert "bot_ref" not in review_with
    assert review_with["aws_region"] == "${{ vars.CI_BOT_AWS_REGION || 'us-east-1' }}"

    review_secrets = workflow["jobs"]["review"]["secrets"]
    assert review_secrets["AWS_ROLE_ARN"] == "${{ secrets.CI_BOT_AWS_ROLE_ARN }}"
    assert "GITHUB_TOKEN" not in review_secrets


def test_external_review_workflow_supports_cross_repo_dispatch() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/review-external-pr.yml")
    on_block = _get_on_block(workflow)
    inputs = on_block["workflow_dispatch"]["inputs"]
    job = workflow["jobs"]["review"]
    job_env = job["env"]

    assert workflow["permissions"] == {"contents": "write", "id-token": "write"}
    assert workflow["env"]["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] is True
    assert inputs["target_repo"]["required"] is True
    assert inputs["pr_number"]["required"] is True
    assert inputs["config_path"]["default"] == ".github/pr-review-bot.yml"
    assert inputs["aws_region"]["default"] == "us-east-1"
    assert job["concurrency"]["group"] == (
        "review-external-pr-${{ github.event.inputs.target_repo }}-"
        "${{ github.event.inputs.pr_number }}"
    )
    assert "TARGET_GITHUB_TOKEN" in job_env
    assert "TARGET_GITHUB_APP_ID" in job_env
    assert "TARGET_GITHUB_APP_PRIVATE_KEY" in job_env

    resolve_step = next(
        step
        for step in job["steps"]
        if step["name"] == "Resolve target repository"
    )
    app_token_step = next(
        step
        for step in job["steps"]
        if step["name"] == "Create target repository GitHub App token"
    )
    configure_step = next(
        step
        for step in job["steps"]
        if step["name"] == "Configure AWS credentials from OIDC role"
    )
    review_step = next(
        step
        for step in job["steps"]
        if step["name"] == "Review target pull request"
    )

    assert "TARGET_REPO" in resolve_step["run"]
    assert app_token_step["uses"] == "actions/create-github-app-token@v2"
    assert app_token_step["with"]["owner"] == "${{ steps.target-repo.outputs.owner }}"
    assert app_token_step["with"]["repositories"] == "${{ steps.target-repo.outputs.repository }}"
    assert app_token_step["with"]["permission-contents"] == "read"
    assert app_token_step["with"]["permission-pull-requests"] == "write"
    assert configure_step["uses"] == "aws-actions/configure-aws-credentials@v5"
    assert configure_step["with"]["role-to-assume"] == "${{ secrets.AWS_ROLE_ARN }}"

    review_script = review_step["run"]
    assert "python -m scripts.pr_review_main" in review_script
    assert '--pr-number "${TARGET_PR_NUMBER}"' in review_script
    assert '--state-token "${{ github.token }}"' in review_script
    assert '--state-repo "${{ github.repository }}"' in review_script

    for step in job["steps"]:
        if "if" in step:
            assert "secrets." not in step["if"]

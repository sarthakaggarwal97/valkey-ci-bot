"""Regression tests for backport workflow wiring."""

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


def test_backport_workflow_checks_out_called_repository() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/backport.yml")
    on_block = _get_on_block(workflow)
    inputs = on_block["workflow_call"]["inputs"]
    secrets = on_block["workflow_call"]["secrets"]

    assert inputs["config_path"]["default"] == ".github/backport-agent.yml"
    assert secrets["AWS_ROLE_ARN"]["required"] is True
    assert secrets["VALKEY_GITHUB_TOKEN"]["required"] is False
    assert workflow["env"]["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] is True

    checkout_step = next(
        step
        for step in workflow["jobs"]["backport"]["steps"]
        if step["name"] == "Checkout bot repository"
    )
    setup_step = next(
        step
        for step in workflow["jobs"]["backport"]["steps"]
        if step["name"] == "Set up Python 3.11"
    )
    configure_step = next(
        step
        for step in workflow["jobs"]["backport"]["steps"]
        if step["name"] == "Configure AWS credentials from OIDC role"
    )
    run_step = next(
        step
        for step in workflow["jobs"]["backport"]["steps"]
        if step["name"] == "Run backport pipeline"
    )

    assert "repository" not in checkout_step.get("with", {})
    assert "ref" not in checkout_step.get("with", {})
    assert checkout_step["uses"] == "actions/checkout@v6"
    assert setup_step["uses"] == "actions/setup-python@v6"
    assert configure_step["uses"] == "aws-actions/configure-aws-credentials@v5"
    assert configure_step["with"]["role-to-assume"] == "${{ secrets.AWS_ROLE_ARN }}"

    script = run_step["run"]
    assert "python -m scripts.backport_main" in script
    assert '--repo "${{ inputs.repo_full_name }}"' in script
    assert '--pr-number "${{ inputs.source_pr_number }}"' in script
    assert '--target-branch "${{ inputs.target_branch }}"' in script

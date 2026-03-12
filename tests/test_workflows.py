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

    assert "bot_repository" in inputs
    assert "bot_ref" in inputs

    checkout_step = next(
        step
        for step in workflow["jobs"]["run-pipeline"]["steps"]
        if step["name"] == "Checkout bot repository"
    )
    assert checkout_step["with"]["repository"] == "${{ inputs.bot_repository }}"
    assert checkout_step["with"]["ref"] == "${{ inputs.bot_ref }}"


def test_example_caller_passes_bot_checkout_inputs() -> None:
    workflow = _load_yaml(REPO_ROOT / "examples/caller-workflow.yml")

    analyze_with = workflow["jobs"]["analyze"]["with"]
    assert analyze_with["bot_repository"] == "valkey-io/valkey-ci-bot"
    assert analyze_with["bot_ref"] == "v1"

    reconcile_with = workflow["jobs"]["reconcile"]["with"]
    assert reconcile_with["bot_repository"] == "valkey-io/valkey-ci-bot"
    assert reconcile_with["bot_ref"] == "v1"

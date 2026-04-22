"""Regression tests for the centralized Valkey CI monitor workflow."""

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


def test_monitor_workflow_uses_oidc_and_matrixed_ci_scope() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/monitor-valkey-daily.yml")
    on_block = _get_on_block(workflow)
    monitor_job = workflow["jobs"]["monitor"]
    job_env = monitor_job["env"]
    matrix_entries = monitor_job["strategy"]["matrix"]["include"]

    assert workflow["permissions"] == {
        "actions": "write",
        "contents": "write",
        "id-token": "write",
    }
    assert on_block["workflow_dispatch"]["inputs"]["workflow_scope"]["default"] == "all"
    assert on_block["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    assert workflow["env"]["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] is True
    assert "concurrency" not in workflow
    assert "github.event_name != 'workflow_dispatch'" in job_env["MONITOR_DRY_RUN"]
    assert "matrix.max_runs" in job_env["MONITOR_MAX_RUNS"]
    assert job_env["MONITOR_WORKFLOW_FILE"] == "${{ matrix.workflow_file }}"
    assert job_env["MONITOR_EVENTS"] == "${{ matrix.monitor_events }}"
    assert "matrix.scope" in job_env["MONITOR_SCOPE_SELECTED"]
    assert job_env["VALKEY_GITHUB_TOKEN"] == "${{ secrets.VALKEY_GITHUB_TOKEN }}"
    assert job_env["VALKEY_GITHUB_APP_ID"] == "${{ vars.VALKEY_GITHUB_APP_ID || '' }}"
    assert job_env["VALKEY_GITHUB_APP_PRIVATE_KEY"] == "${{ secrets.VALKEY_GITHUB_APP_PRIVATE_KEY }}"
    assert job_env["VALKEY_FORK_REPO"] == "${{ vars.VALKEY_FORK_REPO || 'sarthakaggarwal97/valkey' }}"
    assert job_env["VALKEY_FORK_GITHUB_TOKEN"] == "${{ secrets.VALKEY_FORK_GITHUB_TOKEN }}"
    assert monitor_job["concurrency"]["group"] == "monitor-valkey-ci-${{ matrix.scope }}"
    assert "create-draft-prs" not in workflow["jobs"]

    assert {entry["scope"] for entry in matrix_entries} == {
        "ci",
        "daily",
        "external",
        "weekly",
    }
    assert {
        (entry["workflow_file"], entry["monitor_events"], entry["max_runs"])
        for entry in matrix_entries
    } == {
        ("ci.yml", "pull_request,push", "14"),
        ("daily.yml", "schedule", "14"),
        ("external.yml", "schedule,pull_request,push", "14"),
        ("weekly.yml", "schedule", "4"),
    }

    app_token_step = next(
        step
        for step in monitor_job["steps"]
        if step["name"] == "Create target repository GitHub App token"
    )
    setup_step = next(
        step
        for step in monitor_job["steps"]
        if step["name"] == "Set up Python 3.11"
    )
    configure_step = next(
        step
        for step in monitor_job["steps"]
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

    for step in monitor_job["steps"]:
        if "if" in step:
            assert "secrets." not in step["if"]


def test_monitor_workflow_runs_central_monitor_script_for_matrix_entries() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/monitor-valkey-daily.yml")
    monitor_steps = workflow["jobs"]["monitor"]["steps"]

    run_step = next(
        step
        for step in monitor_steps
        if step["name"] == "Monitor and queue Valkey workflow failures"
    )
    checkout_step = next(
        step
        for step in monitor_steps
        if step["name"] == "Check out agent repository"
    )
    capture_step = next(
        step
        for step in monitor_steps
        if step["name"] == "Capture monitor outputs"
    )
    script = run_step["run"]
    assert "-m scripts.monitor_workflow_runs" in script
    assert '--workflow-file "${MONITOR_WORKFLOW_FILE}"' in script
    assert '--config ".github/valkey-daily-bot.yml"' in script
    assert "--queue-only" in script
    assert "read -ra monitor_events" in script
    assert 'args+=(--event "${event_name}")' in script
    assert "raw_result = result_path.read_text(encoding=\"utf-8\").strip()" in capture_step["run"]
    assert "except json.JSONDecodeError" in capture_step["run"]
    assert "env.MONITOR_SCOPE_SELECTED == 'true'" in checkout_step["if"]
    assert "env.MONITOR_SCOPE_SELECTED == 'true'" in run_step["if"]
    assert "env.MONITOR_SCOPE_SELECTED == 'true'" in capture_step["if"]

    preflight_step = next(
        step
        for step in monitor_steps
        if step["name"] == "Validate fork base branches for queued fixes"
    )
    reconcile_step = next(
        step
        for step in monitor_steps
        if step["name"] == "Create draft PRs for queued fixes"
    )
    select_fork_token_step = next(
        step
        for step in monitor_steps
        if step["name"] == "Select fork repository token"
    )
    upload_step = next(
        step
        for step in monitor_steps
        if step["name"] == "Upload monitor result"
    )
    preflight_script = preflight_step["run"]
    assert "-m scripts.preflight_reconciliation" in preflight_script
    assert "--repo \"${VALKEY_FORK_REPO}\"" in preflight_script
    assert "steps.capture-monitor.outputs.has_queued_failures" in preflight_step["if"]
    assert "steps.capture-monitor.outputs.has_queued_failures" in select_fork_token_step["if"]

    reconcile_script = reconcile_step["run"]
    assert "--mode reconcile" in reconcile_script
    assert "--repo \"${VALKEY_FORK_REPO}\"" in reconcile_script
    assert "--draft-prs" in reconcile_script

    assert upload_step["uses"] == "actions/upload-artifact@v4"
    assert "valkey-${{ matrix.scope }}-monitor-result-${{ github.run_id }}" == upload_step["with"]["name"]


def test_monitor_workflow_builds_dashboard_in_separate_job() -> None:
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/monitor-valkey-daily.yml")
    dashboard_job = workflow["jobs"]["dashboard"]
    dashboard_steps = dashboard_job["steps"]

    assert dashboard_job["needs"] == "monitor"
    assert dashboard_job["concurrency"]["group"] == "monitor-valkey-ci-dashboard"
    assert "workflow_scope == 'daily'" in dashboard_job["if"]

    download_step = next(
        step
        for step in dashboard_steps
        if step["name"] == "Download Daily monitor result"
    )
    generate_step = next(
        step
        for step in dashboard_steps
        if step["name"] == "Generate capability dashboard"
    )
    site_step = next(
        step
        for step in dashboard_steps
        if step["name"] == "Generate observability site"
    )
    upload_step = next(
        step
        for step in dashboard_steps
        if step["name"] == "Upload capability dashboard"
    )

    assert download_step["uses"] == "actions/download-artifact@v4"
    assert download_step["with"]["name"] == "valkey-daily-monitor-result-${{ github.run_id }}"
    assert "daily-monitor/monitor-result.json" in generate_step["run"]
    assert "-m scripts.agent_dashboard" in generate_step["run"]
    assert "--event-log bot-data/agent-events.jsonl" in generate_step["run"]
    assert "--output-html agent-dashboard.html" in generate_step["run"]
    assert "-m scripts.agent_dashboard_site" in site_step["run"]
    assert upload_step["uses"] == "actions/upload-artifact@v4"
    assert "agent-dashboard.html" in upload_step["with"]["path"]
    assert "dashboard-site" in upload_step["with"]["path"]

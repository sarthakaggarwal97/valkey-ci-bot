"""Tests for the CI agent capability dashboard."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.agent_dashboard import build_dashboard, main, render_markdown


def _failure_store() -> dict:
    return {
        "entries": {
            "fp-open": {
                "fingerprint": "fp-open",
                "failure_identifier": "test-cache-flush",
                "status": "open",
                "file_path": "tests/unit/cache.tcl",
                "updated_at": "2026-04-07T12:00:00+00:00",
            },
            "fp-queued": {
                "fingerprint": "fp-queued",
                "failure_identifier": "test-replica-sync",
                "status": "processing",
                "file_path": "tests/integration/replica.tcl",
                "updated_at": "2026-04-08T12:00:00+00:00",
            },
        },
        "history": {
            "hist-1": {
                "observations": [
                    {"outcome": "fail"},
                    {"outcome": "pass"},
                    {"outcome": "fail"},
                ]
            }
        },
        "campaigns": {
            "campaign-active": {
                "fingerprint": "campaign-active",
                "failure_identifier": "test-cache-flush",
                "job_name": "daily / linux",
                "branch": "unstable",
                "status": "active",
                "updated_at": "2026-04-08T10:00:00+00:00",
                "total_attempts": 3,
                "consecutive_full_passes": 1,
                "failed_hypotheses": ["timeout theory"],
                "queued_pr_payload": {"title": "Fix flaky cache flush"},
            },
            "campaign-done": {
                "fingerprint": "campaign-done",
                "failure_identifier": "test-old",
                "job_name": "daily / macos",
                "branch": "unstable",
                "status": "validated",
                "updated_at": "2026-04-07T10:00:00+00:00",
                "total_attempts": 1,
                "consecutive_full_passes": 2,
                "failed_hypotheses": [],
            },
        },
    }


def _fuzzer_result() -> dict:
    return {
        "runs": [
            {
                "run_id": 101,
                "conclusion": "failure",
                "issue_action": "created",
                "issue_url": "https://github.com/valkey-io/valkey-fuzzer/issues/1",
                "analysis": {
                    "run_url": "https://github.com/valkey-io/valkey-fuzzer/actions/runs/101",
                    "overall_status": "anomalous",
                    "scenario_id": "reshard",
                    "seed": "123",
                    "root_cause_category": "slot-coverage-drop",
                    "summary": "Slot coverage dropped after migration.",
                    "raw_log_fallback_used": True,
                },
            },
            {
                "run_id": 102,
                "conclusion": "success",
                "analysis": {
                    "overall_status": "normal",
                    "scenario_id": "reshard",
                    "seed": "124",
                    "root_cause_category": None,
                    "summary": "Healthy run.",
                },
            },
        ]
    }


def test_build_dashboard_summarizes_agent_capabilities() -> None:
    dashboard = build_dashboard(
        failure_store=_failure_store(),
        rate_state={
            "queued_failures": ["fp-queued"],
            "token_usage": 42_000,
            "token_window_start": "2026-04-08T00:00:00+00:00",
            "ai_metrics": {
                "bedrock.invoke_schema.calls": 4,
                "bedrock.invoke_schema.success": 3,
                "bedrock.schema_tool_choice_rejected": 1,
                "bedrock.schema_tool_choice_fallback_success": 1,
                "bedrock.tool_loop.terminal_validation_rejections": 2,
                "bedrock.retries": 5,
            },
        },
        monitor_state={
            "valkey-io/valkey:daily.yml:schedule": {
                "last_seen_run_id": 9001,
                "target_repo": "valkey-io/valkey",
                "workflow_file": "daily.yml",
                "event": "schedule",
                "updated_at": "2026-04-08T01:00:00+00:00",
            }
        },
        review_state={
            "valkey-io/valkey#1": {
                "repo": "valkey-io/valkey",
                "pr_number": 1,
                "last_reviewed_head_sha": "abc123",
                "summary_comment_id": 11,
                "review_comment_ids": [21, 22],
                "updated_at": "2026-04-08T02:00:00+00:00",
            }
        },
        daily_results=[
            {
                "runs": [
                    {
                        "run_id": 99,
                        "conclusion": "failure",
                        "action": "processed-failure",
                        "job_outcomes": [{"outcome": "pr-created"}],
                    }
                ]
            }
        ],
        fuzzer_results=[_fuzzer_result()],
        acceptance_results=[
            {
                "passed": False,
                "model_followups": ["review-coverage-incomplete"],
                "findings": [{"title": "bug"}],
                "coverage": {
                    "claimed_without_tool": [],
                    "unaccounted_files": ["src/server.c"],
                    "fetch_limit_hit": False,
                },
            }
        ],
        generated_at="2026-04-08T03:00:00+00:00",
    )

    assert dashboard["snapshot"]["failure_incidents"] == 2
    assert dashboard["snapshot"]["active_flaky_campaigns"] == 1
    assert dashboard["snapshot"]["queued_failures"] == 1
    assert dashboard["snapshot"]["tracked_review_prs"] == 1
    assert dashboard["snapshot"]["fuzzer_runs_analyzed"] == 2
    assert dashboard["snapshot"]["fuzzer_anomalous_runs"] == 1
    assert dashboard["ci_failures"]["history_failures"] == 2
    assert dashboard["ci_failures"]["daily_job_outcome_counts"] == {"pr-created": 1}
    assert dashboard["flaky_tests"]["failed_hypotheses"] == 1
    assert dashboard["pr_reviews"]["coverage_incomplete_cases"] == 1
    assert dashboard["ai_reliability"]["token_usage"] == 42_000
    assert dashboard["ai_reliability"]["schema_calls"] == 4
    assert dashboard["ai_reliability"]["schema_successes"] == 3
    assert dashboard["ai_reliability"]["terminal_validation_rejections"] == 2
    assert dashboard["ai_reliability"]["bedrock_retries"] == 5
    assert dashboard["fuzzer"]["raw_log_fallbacks"] == 1


def test_render_markdown_includes_all_dashboards() -> None:
    dashboard = build_dashboard(
        failure_store=_failure_store(),
        fuzzer_results=[_fuzzer_result()],
        generated_at="2026-04-08T03:00:00+00:00",
    )

    markdown = render_markdown(dashboard)

    assert "## Flaky Test Dashboard" in markdown
    assert "## CI Failure Outcomes" in markdown
    assert "## PR Review Dashboard" in markdown
    assert "## Fuzzer Dashboard" in markdown
    assert "## AI Reliability Dashboard" in markdown
    assert "Schema toolChoice rejections" in markdown
    assert "Instrumentation gaps:" in markdown
    assert "slot-coverage-drop" in markdown


def test_cli_writes_markdown_and_json(tmp_path: Path) -> None:
    failure_store_path = tmp_path / "failure-store.json"
    fuzzer_result_path = tmp_path / "fuzzer-monitor-result.json"
    output_markdown_path = tmp_path / "agent-dashboard.md"
    output_json_path = tmp_path / "agent-dashboard.json"
    failure_store_path.write_text(json.dumps(_failure_store()), encoding="utf-8")
    fuzzer_result_path.write_text(json.dumps(_fuzzer_result()), encoding="utf-8")

    exit_code = main(
        [
            "--failure-store",
            str(failure_store_path),
            "--fuzzer-result",
            str(fuzzer_result_path),
            "--output-markdown",
            str(output_markdown_path),
            "--output-json",
            str(output_json_path),
        ]
    )

    assert exit_code == 0
    assert "CI Agent Capability Dashboard" in output_markdown_path.read_text(
        encoding="utf-8"
    )
    payload = json.loads(output_json_path.read_text(encoding="utf-8"))
    assert payload["snapshot"]["failure_incidents"] == 2
    assert payload["snapshot"]["fuzzer_anomalous_runs"] == 1

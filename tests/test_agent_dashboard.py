"""Tests for the CI agent capability dashboard."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.agent_dashboard import build_dashboard, main, render_html, render_markdown


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
                "failure_identifier": "maxmemory-eviction",
                "job_name": "daily / linux",
                "branch": "unstable",
                "status": "active",
                "updated_at": "2026-04-08T10:00:00+00:00",
                "total_attempts": 3,
                "consecutive_full_passes": 1,
                "failed_hypotheses": ["timeout theory"],
                "queued_pr_payload": {"title": "Fix flaky cache flush"},
                "proof_status": "pending",
                "proof_required_runs": 100,
                "proof_passed_runs": 0,
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
                "proof_status": "passed",
                "proof_required_runs": 100,
                "proof_passed_runs": 100,
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
                    "triage_verdict": "possible-core-valkey-bug",
                    "suggested_labels": ["possible-valkey-bug"],
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
                    "triage_verdict": "expected-chaos-noise",
                    "scenario_id": "reshard",
                    "seed": "124",
                    "root_cause_category": None,
                    "summary": "Healthy run.",
                },
            },
        ]
    }


def _trend_events() -> list[dict]:
    return [
        {
            "event_id": "run-1",
            "event_type": "workflow.run_seen",
            "created_at": "2026-04-06T02:00:00+00:00",
            "subject": "valkey-io/valkey:daily.yml:101",
            "attributes": {"conclusion": "failure"},
        },
        {
            "event_id": "run-2",
            "event_type": "workflow.run_seen",
            "created_at": "2026-04-07T02:00:00+00:00",
            "subject": "valkey-io/valkey:daily.yml:102",
            "attributes": {"conclusion": "success"},
        },
        {
            "event_id": "run-3",
            "event_type": "workflow.run_seen",
            "created_at": "2026-04-08T02:00:00+00:00",
            "subject": "valkey-io/valkey:daily.yml:103",
            "attributes": {"conclusion": "failure"},
        },
        {
            "event_id": "review-1",
            "event_type": "review.comments_posted",
            "created_at": "2026-04-07T03:00:00+00:00",
            "subject": "valkey-io/valkey#1",
            "attributes": {"comments": 2},
        },
        {
            "event_id": "review-2",
            "event_type": "review.note_posted",
            "created_at": "2026-04-08T03:00:00+00:00",
            "subject": "valkey-io/valkey#2",
            "attributes": {"note_kind": "coverage-incomplete"},
        },
        {
            "event_id": "review-3",
            "event_type": "review.state_saved",
            "created_at": "2026-04-08T03:10:00+00:00",
            "subject": "valkey-io/valkey#2",
            "attributes": {"review_completed_for_head": False},
        },
    ]


def _acceptance_payload() -> dict:
    return {
        "scorecard": {
            "review_cases": 2,
            "review_passed": 1,
            "review_failed": 1,
            "workflow_cases": 1,
            "workflow_passed": 1,
            "workflow_failed": 0,
            "ci_replay_cases": 2,
            "backport_replay_cases": 1,
            "readiness": "pilot-ready",
        },
        "results": [
            {
                "name": "docs follow-up PR",
                "pr_number": 101,
                "passed": False,
                "model_followups": ["review-coverage-incomplete"],
                "findings": [{"title": "bug"}],
                "coverage": {
                    "claimed_without_tool": [],
                    "unaccounted_files": ["src/server.c"],
                    "fetch_limit_hit": False,
                },
                "expectation_checks": [
                    {"label": "docs", "expected": True, "actual": True, "passed": True},
                    {"label": "dco", "expected": True, "actual": False, "passed": False},
                ],
            }
        ],
        "workflow_results": [
            {
                "name": "dashboard workflow contract",
                "workflow_path": ".github/workflows/agent-dashboard.yml",
                "passed": True,
                "notes": "all good",
                "checks": [{"label": "artifact", "passed": True, "detail": "present"}],
            }
        ],
        "manifest": {
            "review_cases": [1, 2],
            "workflow_cases": [1],
            "ci_cases": [1, 2],
            "backport_cases": [1],
        },
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
                "bedrock.prompt_safety_guard.checked": 10,
                "bedrock.prompt_safety_guard.present": 9,
                "bedrock.prompt_safety_guard.missing": 1,
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
        acceptance_payloads=[_acceptance_payload()],
        events=[
            {
                "event_id": "evt-1",
                "event_type": "validation.passed",
                "created_at": "2026-04-08T02:30:00+00:00",
                "subject": "fp-queued",
                "attributes": {"job_name": "daily / linux"},
            },
            {
                "event_id": "evt-2",
                "event_type": "pr.created",
                "created_at": "2026-04-08T02:31:00+00:00",
                "subject": "fp-queued",
                "attributes": {"pr_url": "https://github.com/o/r/pull/1"},
            },
            {
                "event_id": "evt-3",
                "event_type": "proof.dispatched",
                "created_at": "2026-04-08T02:35:00+00:00",
                "subject": "fp-queued",
                "attributes": {"pr_url": "https://github.com/o/r/pull/1", "proof_runs": 100},
            },
            {
                "event_id": "evt-4",
                "event_type": "proof.passed",
                "created_at": "2026-04-08T02:38:00+00:00",
                "subject": "fp-queued",
                "attributes": {"pr_url": "https://github.com/o/r/pull/1", "passed_runs": 100},
            },
            {
                "event_id": "evt-5",
                "event_type": "pr.merged",
                "created_at": "2026-04-08T02:40:00+00:00",
                "subject": "fp-queued",
                "attributes": {"pr_url": "https://github.com/o/r/pull/1"},
            },
            *_trend_events(),
        ],
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
    assert dashboard["snapshot"]["agent_events"] == 11
    assert dashboard["ci_failures"]["history_failures"] == 2
    assert dashboard["ci_failures"]["daily_job_outcome_counts"] == {"pr-created": 1}
    assert dashboard["flaky_tests"]["failed_hypotheses"] == 1
    assert dashboard["flaky_tests"]["proof_counts"] == {"pending": 1, "passed": 1}
    assert dashboard["flaky_tests"]["subsystem_counts"] == {"memory": 1}
    assert dashboard["pr_reviews"]["coverage_incomplete_cases"] == 1
    assert dashboard["acceptance"]["readiness"] == "pilot-ready"
    assert dashboard["acceptance"]["review_failed"] == 1
    assert dashboard["acceptance"]["workflow_passed"] == 1
    assert dashboard["ai_reliability"]["token_usage"] == 42_000
    assert dashboard["ai_reliability"]["schema_calls"] == 4
    assert dashboard["ai_reliability"]["schema_successes"] == 3
    assert dashboard["ai_reliability"]["terminal_validation_rejections"] == 2
    assert dashboard["ai_reliability"]["bedrock_retries"] == 5
    assert dashboard["ai_reliability"]["prompt_safety_coverage"] == 0.9
    assert dashboard["fuzzer"]["raw_log_fallbacks"] == 1
    assert dashboard["agent_outcomes"]["prs_created"] == 1
    assert dashboard["agent_outcomes"]["proof_dispatched"] == 1
    assert dashboard["agent_outcomes"]["proof_passed"] == 1
    assert dashboard["agent_outcomes"]["prs_merged"] == 1
    assert dashboard["trends"]["failure_rate"]["totals"][-3:] == [1, 1, 1]
    assert dashboard["trends"]["failure_rate"]["rates"][-3:] == [1.0, 0.0, 1.0]
    assert dashboard["trends"]["review_health"]["degraded_reviews"][-1] == 1
    assert dashboard["trends"]["flaky_subsystems"]["top_subsystems"] == ["memory"]


def test_build_dashboard_exposes_campaign_pr_links_from_failure_store() -> None:
    failure_store = _failure_store()
    failure_store["entries"]["campaign-active"] = {
        "fingerprint": "campaign-active",
        "failure_identifier": "maxmemory-eviction",
        "status": "open",
        "file_path": "tests/unit/maxmemory.tcl",
        "pr_url": "https://github.com/valkey-io/valkey/pull/42",
        "updated_at": "2026-04-08T12:30:00+00:00",
    }

    dashboard = build_dashboard(
        failure_store=failure_store,
        generated_at="2026-04-08T03:00:00+00:00",
    )

    active_campaign = next(
        campaign
        for campaign in dashboard["flaky_tests"]["recent_campaigns"]
        if campaign["fingerprint"] == "campaign-active"
    )

    assert active_campaign["pr_url"] == "https://github.com/valkey-io/valkey/pull/42"


def test_build_dashboard_backfills_daily_health_from_monitor_results() -> None:
    dashboard = build_dashboard(
        daily_results=[
            {
                "target_repo": "valkey-io/valkey",
                "workflow_file": "daily.yml",
                "runs": [
                    {
                        "run_id": 101,
                        "created_at": "2026-04-07T02:00:00+00:00",
                        "conclusion": "failure",
                        "head_sha": "abcd1234ef567890",
                        "html_url": "https://github.com/valkey-io/valkey/actions/runs/101",
                        "job_outcomes": [
                            {
                                "job_name": "daily / linux",
                                "failure_identifier": "cluster slot coverage",
                                "outcome": "queued",
                            }
                        ],
                    },
                    {
                        "run_id": 102,
                        "created_at": "2026-04-08T02:00:00+00:00",
                        "conclusion": "success",
                        "head_sha": "bcde2345fa678901",
                        "html_url": "https://github.com/valkey-io/valkey/actions/runs/102",
                        "job_outcomes": [],
                    },
                ],
            }
        ],
        generated_at="2026-04-08T03:00:00+00:00",
    )

    daily_health = dashboard["daily_health"]

    assert daily_health["repo"] == "valkey-io/valkey"
    assert daily_health["workflow"] == "daily.yml"
    assert daily_health["total_runs"] == 2
    assert daily_health["failed_runs"] == 1
    assert daily_health["unique_failures"] == 1
    assert daily_health["dates"] == ["2026-04-07", "2026-04-08"]
    assert daily_health["runs"][0]["run_url"] == "https://github.com/valkey-io/valkey/actions/runs/102"
    assert daily_health["heatmap"][0]["name"] == "cluster slot coverage"


def test_render_markdown_includes_all_dashboards() -> None:
    dashboard = build_dashboard(
        failure_store=_failure_store(),
        fuzzer_results=[_fuzzer_result()],
        events=_trend_events(),
        generated_at="2026-04-08T03:00:00+00:00",
    )

    markdown = render_markdown(dashboard)

    assert "## Trend Watch" in markdown
    assert "## Flaky Test Dashboard" in markdown
    assert "## CI Failure Outcomes" in markdown
    assert "## Agent Outcome Ledger" in markdown
    assert "## PR Review Dashboard" in markdown
    assert "## Fuzzer Dashboard" in markdown
    assert "## AI Reliability Dashboard" in markdown
    assert "Schema toolChoice rejections" in markdown
    assert "Instrumentation gaps:" in markdown
    assert "Subsystems:" in markdown
    assert "Proof:" in markdown
    assert "slot-coverage-drop" in markdown
    assert "possible-core-valkey-bug" in markdown
    assert "pending" in markdown


def test_render_html_is_polished_static_dashboard() -> None:
    dashboard = build_dashboard(
        failure_store=_failure_store(),
        fuzzer_results=[_fuzzer_result()],
        events=_trend_events(),
        generated_at="2026-04-08T03:00:00+00:00",
    )

    html = render_html(dashboard)

    assert "<!doctype html>" in html
    assert "<title>CI Agent Capability Dashboard</title>" in html
    assert 'class="metrics"' in html
    assert "Trend Watch" in html
    assert "Failure Rate" in html
    assert "Review Health" in html
    assert 'class="sparkline"' in html
    assert "Flaky Test Lab" in html
    assert "Proof" in html
    assert "Fuzzer Watch" in html
    assert "AI Reliability" in html
    assert "memory" in html
    assert "slot-coverage-drop" in html
    assert "possible-core-valkey-bug" in html
    assert "border-radius: 8px" in html


def test_cli_writes_markdown_and_json(tmp_path: Path) -> None:
    failure_store_path = tmp_path / "failure-store.json"
    fuzzer_result_path = tmp_path / "fuzzer-monitor-result.json"
    event_log_path = tmp_path / "agent-events.jsonl"
    output_markdown_path = tmp_path / "agent-dashboard.md"
    output_json_path = tmp_path / "agent-dashboard.json"
    output_html_path = tmp_path / "agent-dashboard.html"
    failure_store_path.write_text(json.dumps(_failure_store()), encoding="utf-8")
    fuzzer_result_path.write_text(json.dumps(_fuzzer_result()), encoding="utf-8")
    event_log_path.write_text(
        json.dumps({
            "event_id": "evt-1",
            "event_type": "fix.dead_lettered",
            "created_at": "2026-04-08T02:30:00+00:00",
            "subject": "fp-queued",
            "attributes": {"attempts": 5},
        }) + "\n",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--failure-store",
            str(failure_store_path),
            "--fuzzer-result",
            str(fuzzer_result_path),
            "--event-log",
            str(event_log_path),
            "--output-markdown",
            str(output_markdown_path),
            "--output-json",
            str(output_json_path),
            "--output-html",
            str(output_html_path),
        ]
    )

    assert exit_code == 0
    assert "CI Agent Capability Dashboard" in output_markdown_path.read_text(
        encoding="utf-8"
    )
    payload = json.loads(output_json_path.read_text(encoding="utf-8"))
    assert payload["snapshot"]["failure_incidents"] == 2
    assert payload["snapshot"]["fuzzer_anomalous_runs"] == 1
    assert payload["agent_outcomes"]["dead_lettered"] == 1
    assert "CI Agent Capability Dashboard" in output_html_path.read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# WoW trends
# ---------------------------------------------------------------------------

def test_wow_trends_basic() -> None:
    """WoW trends compare this week vs last week failure counts."""
    dashboard = build_dashboard(
        failure_store=_failure_store(),
        daily_health_data={
            "runs": [
                {"date": "2026-04-08", "status": "failure", "failure_names": ["test-a", "test-b"]},
                {"date": "2026-04-07", "status": "failure", "failure_names": ["test-a"]},
                {"date": "2026-04-01", "status": "failure", "failure_names": ["test-a", "test-c"]},
                {"date": "2026-03-31", "status": "success", "failure_names": []},
            ],
        },
        generated_at="2026-04-08T12:00:00+00:00",
    )
    wow = dashboard["wow_trends"]
    assert wow["has_data"] is True
    tw = wow["this_week"]
    lw = wow["last_week"]
    # This week (Apr 2-8): test-a x2, test-b x1 = 3 hits, 2 unique
    assert tw["total_failure_hits"] == 3
    assert tw["unique_failures"] == 2
    assert tw["failed_runs"] == 2
    # Last week (Mar 26 - Apr 1): test-a x1, test-c x1 = 2 hits, 2 unique
    assert lw["total_failure_hits"] == 2
    assert lw["unique_failures"] == 2
    assert lw["failed_runs"] == 1
    # Delta
    assert wow["delta"] == 1
    assert "test-b" in wow["new_failures"]
    assert "test-c" in wow["resolved_failures"]


def test_wow_trends_empty_data() -> None:
    """WoW trends handle empty data gracefully."""
    dashboard = build_dashboard(
        failure_store={},
        daily_health_data={"runs": []},
        generated_at="2026-04-08T12:00:00+00:00",
    )
    wow = dashboard["wow_trends"]
    assert wow["has_data"] is False
    assert wow["delta"] == 0
    assert wow["new_failures"] == []
    assert wow["resolved_failures"] == []
    assert wow["top_movers"] == []


def test_wow_trends_top_movers_sorted_by_abs_change() -> None:
    """Top movers are sorted by absolute change descending."""
    dashboard = build_dashboard(
        failure_store={},
        daily_health_data={
            "runs": [
                {"date": "2026-04-08", "status": "failure", "failure_names": ["big-increase"] * 5 + ["small-increase"]},
                {"date": "2026-04-01", "status": "failure", "failure_names": ["big-decrease"] * 4},
            ],
        },
        generated_at="2026-04-08T12:00:00+00:00",
    )
    movers = dashboard["wow_trends"]["top_movers"]
    names = [m["name"] for m in movers]
    # big-increase (+5) and big-decrease (-4) should come before small-increase (+1)
    assert names.index("big-increase") < names.index("small-increase")
    assert names.index("big-decrease") < names.index("small-increase")

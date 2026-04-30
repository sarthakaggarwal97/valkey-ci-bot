"""Generate dashboard JSON fixture files for frontend testing."""
import json
from pathlib import Path

XSS_TITLE = '<script>alert("xss")</script>'
XSS_IMG = '<img onerror=alert(1) src=x>'
XSS_ATTR = '"><script>alert(1)</script>'


def _dates(n=14):
    return ["2026-04-{:02d}".format(16 + i) for i in range(n)]


def _empty_week():
    return {"total_failure_hits": 0, "unique_failures": 0, "failed_runs": 0, "total_runs": 0}


def _heatmap_row(name, days_failed, total_days, dates, hit_dates=None):
    hit_dates = hit_dates or set()
    return {
        "name": name,
        "days_failed": days_failed,
        "total_days": total_days,
        "cells": [
            {"date": d, "count": 1 if d in hit_dates else 0, "has_run": True}
            for d in dates
        ],
    }


def build_full():
    dates = _dates(14)
    heatmap_names = [
        "jemalloc / sanitize",
        "cluster slot migration " + XSS_ATTR,
        "replication backlog timeout",
        "clients state report follows",
        "memory defrag large keys",
        "RDMA basic handshake",
    ]
    hit_map = {
        heatmap_names[0]: {dates[0], dates[2], dates[5], dates[8], dates[10], dates[13]},
        heatmap_names[1]: {dates[1], dates[4], dates[9]},
        heatmap_names[2]: {dates[3], dates[7], dates[11]},
        heatmap_names[3]: {dates[6], dates[12]},
        heatmap_names[4]: {dates[2], dates[5]},
        heatmap_names[5]: {dates[10]},
    }
    heatmap = [
        _heatmap_row(n, len(hit_map[n]), 14, dates, hit_map[n])
        for n in heatmap_names
    ]
    daily_wf = {
        "workflow": "daily.yml", "total_runs": 10, "failed_runs": 6,
        "unique_failures": 4, "dates": dates, "days_with_runs": 14,
        "heatmap": heatmap[:4],
    }
    weekly_wf = {
        "workflow": "weekly.yml", "total_runs": 4, "failed_runs": 2,
        "unique_failures": 2, "dates": dates, "days_with_runs": 4,
        "missing_dates": dates[1:3],
        "heatmap": heatmap[4:],
    }
    runs = [
        {
            "date": dates[-1], "workflow": "daily.yml", "status": "failure",
            "commit_sha": "abc1234", "full_sha": "abc1234def5678901234567890abcdef12345678",
            "commit_message": "Fix memory leak " + XSS_TITLE,
            "unique_failures": 2, "failed_jobs": 3,
            "failed_job_names": ["test-ubuntu-asan " + XSS_ATTR, "test-alpine-valgrind"],
            "run_id": "12345", "run_url": "https://github.com/valkey-io/valkey/actions/runs/12345",
            "failure_names": ["jemalloc / sanitize", "replication backlog timeout"],
            "commits_since_prev": [
                {"sha": "aaa1111", "message": "Refactor allocator", "author": "dev1"},
                {"sha": "bbb2222", "message": "Update tests " + XSS_IMG, "author": "dev2"},
            ],
        },
        {
            "date": dates[-2], "workflow": "daily.yml", "status": "success",
            "commit_sha": "def5678", "full_sha": "def5678abc1234567890abcdef1234567890abcd",
            "commit_message": "Improve cluster stability",
            "unique_failures": 0, "failed_jobs": 0, "failed_job_names": [],
            "run_id": "12344", "run_url": "https://github.com/valkey-io/valkey/actions/runs/12344",
            "failure_names": [], "commits_since_prev": [],
        },
        {
            "date": dates[-3], "workflow": "weekly.yml", "status": "failure",
            "commit_sha": "ghi9012", "full_sha": "ghi9012jkl3456789012345678901234567890ab",
            "commit_message": "Weekly regression check",
            "unique_failures": 1, "failed_jobs": 1,
            "failed_job_names": ["test-weekly-full"],
            "run_id": "12340", "run_url": "https://github.com/valkey-io/valkey/actions/runs/12340",
            "failure_names": ["memory defrag large keys"], "commits_since_prev": [],
        },
    ]
    daily_health = {
        "repo": "valkey-io/valkey", "workflow": "daily.yml, weekly.yml",
        "branch": "unstable", "dates": dates,
        "total_runs": 14, "failed_runs": 8, "unique_failures": 6,
        "days_with_runs": 14, "workflows": ["daily.yml", "weekly.yml"],
        "workflow_reports": [daily_wf, weekly_wf],
        "heatmap": heatmap, "runs": runs,
        "tests": {}, "failure_jobs": {},
    }
    wow_trends = {
        "has_data": True,
        "this_week": {"total_failure_hits": 12, "unique_failures": 4, "failed_runs": 5, "total_runs": 7},
        "last_week": {"total_failure_hits": 8, "unique_failures": 3, "failed_runs": 3, "total_runs": 7},
        "delta": 4, "pct_change": 50.0,
        "new_failures": ["RDMA basic handshake"],
        "resolved_failures": ["sentinel failover timing"],
        "top_movers": [
            {"name": "jemalloc / sanitize", "this_week": 5, "last_week": 2, "change": 3},
            {"name": "sentinel failover timing", "this_week": 0, "last_week": 3, "change": -3},
        ],
    }
    ci_failures = {
        "failure_incidents": 8, "entry_status_counts": {"queued": 2, "abandoned": 3, "merged": 3},
        "history_entries": 6, "history_observations": 24, "history_failures": 10, "history_passes": 14,
        "queued_failures": 2, "queued_failure_fingerprints": ["fp-001", "fp-002"],
        "daily_result_files": 1, "daily_runs_seen": 14,
        "daily_action_counts": {"analyzed": 8, "skipped": 6},
        "daily_conclusion_counts": {"success": 6, "failure": 8},
        "daily_job_outcome_counts": {"pass": 40, "fail": 12},
        "recent_incidents": [
            {"failure_identifier": "jemalloc / sanitize " + XSS_TITLE, "status": "queued",
             "file_path": "src/zmalloc.c", "pr_url": "https://github.com/valkey-io/valkey/pull/99",
             "updated_at": "2026-04-29T10:00:00+00:00"},
            {"failure_identifier": "cluster slot migration", "status": "merged",
             "file_path": "src/cluster.c", "pr_url": "", "updated_at": "2026-04-28T08:00:00+00:00"},
        ],
    }
    flaky_tests = {
        "campaigns": 4, "active_campaigns": 2,
        "status_counts": {"active": 2, "merged": 1, "abandoned": 1},
        "proof_counts": {"passed": 1, "pending": 1},
        "subsystem_counts": {"memory": 1, "cluster": 1},
        "total_attempts": 12, "failed_hypotheses": 3, "consecutive_full_passes": 5,
        "recent_campaigns": [
            {"failure_identifier": "jemalloc / sanitize", "subsystem": "memory",
             "status": "active", "proof_status": "pending", "proof_url": "",
             "pr_url": "", "job_name": "test-ubuntu-asan", "branch": "unstable",
             "total_attempts": 5, "consecutive_full_passes": 2,
             "queued_pr_payload": None, "updated_at": "2026-04-29T09:00:00+00:00",
             "fingerprint": "fp-001"},
            {"failure_identifier": "cluster slot migration", "subsystem": "cluster",
             "status": "merged", "proof_status": "passed",
             "proof_url": "https://github.com/valkey-io/valkey/actions/runs/11111",
             "pr_url": "https://github.com/valkey-io/valkey/pull/100",
             "job_name": "test-cluster", "branch": "unstable",
             "total_attempts": 7, "consecutive_full_passes": 3,
             "queued_pr_payload": None, "updated_at": "2026-04-27T12:00:00+00:00",
             "fingerprint": "fp-003"},
        ],
    }
    pr_reviews = {
        "tracked_prs": 3, "summary_comments": 3, "review_comments": 7,
        "recent_reviews": [
            {"repo": "valkey-io/valkey", "pr_number": 101,
             "last_reviewed_head_sha": "aabbcc1122", "summary_comment_id": 5001,
             "review_comment_ids": [6001, 6002], "updated_at": "2026-04-29T08:00:00+00:00"},
            {"repo": "valkey-io/valkey", "pr_number": 102,
             "last_reviewed_head_sha": "ddeeff3344", "summary_comment_id": 5002,
             "review_comment_ids": [6003], "updated_at": "2026-04-28T14:00:00+00:00"},
            {"repo": "valkey-io/valkey", "pr_number": 103,
             "last_reviewed_head_sha": "112233aabb", "summary_comment_id": 5003,
             "review_comment_ids": [6004, 6005, 6006, 6007],
             "updated_at": "2026-04-27T20:00:00+00:00"},
        ],
        "acceptance_cases": 4, "acceptance_passed": 3, "acceptance_failed": 1,
        "acceptance_findings": 2, "coverage_incomplete_cases": 1,
        "model_followup_counts": {"docs_needed": 1},
    }
    acceptance = {
        "payloads_seen": 1, "readiness": "pilot-ready",
        "review_cases": 3, "review_passed": 2, "review_failed": 1,
        "workflow_cases": 2, "workflow_passed": 2, "workflow_failed": 0,
        "ci_replay_cases": 1, "backport_replay_cases": 1,
        "manifest_review_cases": 3, "manifest_workflow_cases": 2,
        "manifest_ci_cases": 1, "manifest_backport_cases": 1,
        "finding_count": 2, "model_followup_counts": {"docs_needed": 1},
        "recent_review_results": [
            {"name": "DCO enforcement", "repo": "valkey-io/valkey", "pr_number": "101",
             "passed": True, "coverage": {"claimed_without_tool": [], "unaccounted_files": []},
             "findings": [], "model_followups": []},
            {"name": "Security review " + XSS_TITLE, "repo": "valkey-io/valkey", "pr_number": "102",
             "passed": False, "coverage": {"claimed_without_tool": ["src/acl.c"], "unaccounted_files": []},
             "findings": [{"severity": "high", "message": "Missing input validation"}],
             "model_followups": ["docs_needed"]},
        ],
        "recent_workflow_results": [
            {"name": "CI workflow contract", "workflow_path": ".github/workflows/ci.yml",
             "passed": True, "checks": [{"name": "timeout", "passed": True}], "notes": ""},
        ],
    }
    fuzzer = {
        "result_files": 2, "runs_seen": 8, "runs_analyzed": 7,
        "status_counts": {"normal": 5, "anomalous": 2},
        "conclusion_counts": {"success": 6, "failure": 2},
        "issue_action_counts": {"created": 1, "updated": 1},
        "scenario_counts": {"chaos-restart": 3, "network-partition": 2, "oom-pressure": 2},
        "root_cause_counts": {"timeout": 1, "crash": 1},
        "raw_log_fallbacks": 1,
        "recent_anomalies": [
            {"run_id": "fz-001", "run_url": "https://github.com/valkey-io/valkey-fuzzer/actions/runs/501",
             "status": "anomalous", "triage_verdict": "needs-investigation",
             "scenario_id": "chaos-restart", "seed": "42",
             "root_cause_category": "crash", "summary": "Server crash during restart " + XSS_IMG,
             "issue_url": "https://github.com/valkey-io/valkey-fuzzer/issues/10",
             "issue_action": "created"},
            {"run_id": "fz-002", "run_url": "https://github.com/valkey-io/valkey-fuzzer/actions/runs/502",
             "status": "anomalous", "triage_verdict": "expected-chaos",
             "scenario_id": "network-partition", "seed": "99",
             "root_cause_category": "timeout", "summary": "Partition recovery exceeded 30s",
             "issue_url": "https://github.com/valkey-io/valkey-fuzzer/issues/10",
             "issue_action": "updated"},
        ],
    }
    agent_outcomes = {
        "events": 12, "subjects": 6,
        "event_type_counts": {"pr.created": 2, "pr.merged": 1, "validation.passed": 3,
                              "validation.failed": 1, "proof.dispatched": 2,
                              "proof.passed": 1, "proof.failed": 1, "fix.dead_lettered": 1},
        "validation_passed": 3, "validation_failed": 1,
        "proof_dispatched": 2, "proof_passed": 1, "proof_failed": 1,
        "prs_created": 2, "prs_merged": 1, "prs_closed_without_merge": 0, "dead_lettered": 1,
        "recent_events": [
            {"created_at": "2026-04-29T10:30:00+00:00", "event_type": "pr.created",
             "subject": "valkey-io/valkey#99", "attributes": {"branch": "unstable"}},
            {"created_at": "2026-04-29T09:00:00+00:00", "event_type": "validation.passed",
             "subject": "valkey-io/valkey:daily.yml:12345", "attributes": {"job": "test-ubuntu-asan"}},
            {"created_at": "2026-04-28T22:00:00+00:00", "event_type": "proof.passed",
             "subject": "valkey-io/valkey#100",
             "attributes": {"proof_url": "https://github.com/valkey-io/valkey/actions/runs/11111"}},
            {"created_at": "2026-04-28T18:00:00+00:00", "event_type": "fix.dead_lettered",
             "subject": "valkey-io/valkey:daily.yml:12340 " + XSS_TITLE, "attributes": {"reason": "max_retries"}},
            {"created_at": "2026-04-28T12:00:00+00:00", "event_type": "pr.merged",
             "subject": "valkey-io/valkey#100", "attributes": {}},
        ],
    }
    ai_reliability = {
        "token_usage": 185000, "token_window_start": "2026-04-22T00:00:00+00:00",
        "ai_metrics": {"bedrock.invoke_schema.calls": 42, "bedrock.invoke_schema.success": 40,
                       "bedrock.tool_loop.calls": 15, "bedrock.tool_loop.success": 14,
                       "bedrock.retries": 3, "bedrock.errors.retry_exhausted": 1,
                       "bedrock.prompt_safety_guard.checked": 20,
                       "bedrock.prompt_safety_guard.present": 19,
                       "bedrock.prompt_safety_guard.missing": 1},
        "schema_calls": 42, "schema_successes": 40,
        "schema_tool_choice_rejections": 2, "schema_tool_choice_fallback_successes": 1,
        "tool_loop_calls": 15, "tool_loop_successes": 14,
        "terminal_validation_rejections": 1, "bedrock_retries": 3,
        "retry_exhaustions": 1, "non_retryable_errors": 0,
        "prompt_safety_checked": 20, "prompt_safety_present": 19,
        "prompt_safety_missing": 1, "prompt_safety_coverage": 0.95,
        "review_model_followups": {"docs_needed": 1},
        "fuzzer_raw_log_fallbacks": 1, "instrumentation_gaps": [],
    }
    state_health = {
        "monitor_watermarks": 3,
        "recent_watermarks": [
            {"key": "daily:valkey-io/valkey:daily.yml:unstable",
             "last_seen_run_id": "12345", "target_repo": "valkey-io/valkey",
             "workflow_file": "daily.yml", "updated_at": "2026-04-29T10:00:00+00:00"},
            {"key": "weekly:valkey-io/valkey:weekly.yml:unstable",
             "last_seen_run_id": "12340", "target_repo": "valkey-io/valkey",
             "workflow_file": "weekly.yml", "updated_at": "2026-04-27T06:00:00+00:00"},
        ],
        "input_warnings": ["bot-data/review-state.json was not present"],
    }
    labels = [d[-5:].replace("-", "/")[:-3] + "-" + d[-2:] for d in dates[-7:]]
    # Simpler: just use MM-DD format
    labels = ["{}-{}".format(d[5:7], d[8:10]) for d in dates[-7:]]
    trends = {
        "labels": labels, "window_days": 7,
        "failure_rate": {"rates": [0.5, 0.3, 0.6, 0.4, 0.2, 0.5, 0.7], "average_rate": 0.4571},
        "review_health": {"scores": [1.0, 0.8, 1.0, 0.9, 1.0, 0.7, 1.0],
                          "degraded_reviews": [0, 1, 0, 0, 0, 1, 0], "average_score": 0.9143},
        "flaky_subsystems": {"top_subsystems": ["memory", "cluster", "replication"],
                             "series": {"memory": [2, 1, 3, 0, 1, 2, 1],
                                        "cluster": [1, 0, 1, 1, 0, 0, 1],
                                        "replication": [0, 1, 0, 0, 1, 0, 0]}},
    }
    snapshot = {
        "failure_incidents": 8, "queued_failures": 2, "active_flaky_campaigns": 2,
        "tracked_review_prs": 3, "review_comments": 7,
        "fuzzer_runs_analyzed": 7, "fuzzer_anomalous_runs": 2,
        "daily_runs_seen": 14, "ai_token_usage": 185000,
        "agent_events": 12, "instrumentation_gaps": 0,
    }
    return {
        "schema_version": 1, "generated_at": "2026-04-29T12:00:00+00:00",
        "snapshot": snapshot, "ci_failures": ci_failures, "flaky_tests": flaky_tests,
        "pr_reviews": pr_reviews, "acceptance": acceptance, "fuzzer": fuzzer,
        "agent_outcomes": agent_outcomes, "ai_reliability": ai_reliability,
        "state_health": state_health, "trends": trends,
        "daily_health": daily_health, "wow_trends": wow_trends,
    }


def build_empty():
    return {
        "schema_version": 1, "generated_at": "2026-04-29T12:00:00+00:00",
        "snapshot": {k: 0 for k in [
            "failure_incidents", "queued_failures", "active_flaky_campaigns",
            "tracked_review_prs", "review_comments", "fuzzer_runs_analyzed",
            "fuzzer_anomalous_runs", "daily_runs_seen", "ai_token_usage",
            "agent_events", "instrumentation_gaps"]},
        "ci_failures": {
            "failure_incidents": 0, "entry_status_counts": {}, "history_entries": 0,
            "history_observations": 0, "history_failures": 0, "history_passes": 0,
            "queued_failures": 0, "queued_failure_fingerprints": [],
            "daily_result_files": 0, "daily_runs_seen": 0,
            "daily_action_counts": {}, "daily_conclusion_counts": {},
            "daily_job_outcome_counts": {}, "recent_incidents": []},
        "flaky_tests": {
            "campaigns": 0, "active_campaigns": 0, "status_counts": {},
            "proof_counts": {}, "subsystem_counts": {},
            "total_attempts": 0, "failed_hypotheses": 0,
            "consecutive_full_passes": 0, "recent_campaigns": []},
        "pr_reviews": {
            "tracked_prs": 0, "summary_comments": 0, "review_comments": 0,
            "recent_reviews": [], "acceptance_cases": 0, "acceptance_passed": 0,
            "acceptance_failed": 0, "acceptance_findings": 0,
            "coverage_incomplete_cases": 0, "model_followup_counts": {}},
        "acceptance": {
            "payloads_seen": 0, "readiness": "unknown",
            "review_cases": 0, "review_passed": 0, "review_failed": 0,
            "workflow_cases": 0, "workflow_passed": 0, "workflow_failed": 0,
            "ci_replay_cases": 0, "backport_replay_cases": 0,
            "manifest_review_cases": 0, "manifest_workflow_cases": 0,
            "manifest_ci_cases": 0, "manifest_backport_cases": 0,
            "finding_count": 0, "model_followup_counts": {},
            "recent_review_results": [], "recent_workflow_results": []},
        "fuzzer": {
            "result_files": 0, "runs_seen": 0, "runs_analyzed": 0,
            "status_counts": {}, "conclusion_counts": {},
            "issue_action_counts": {}, "scenario_counts": {},
            "root_cause_counts": {}, "raw_log_fallbacks": 0,
            "recent_anomalies": []},
        "agent_outcomes": {
            "events": 0, "event_type_counts": {}, "subjects": 0,
            "validation_passed": 0, "validation_failed": 0,
            "proof_dispatched": 0, "proof_passed": 0, "proof_failed": 0,
            "prs_created": 0, "prs_merged": 0,
            "prs_closed_without_merge": 0, "dead_lettered": 0,
            "recent_events": []},
        "ai_reliability": {
            "token_usage": 0, "token_window_start": "unknown", "ai_metrics": {},
            "schema_calls": 0, "schema_successes": 0,
            "schema_tool_choice_rejections": 0, "schema_tool_choice_fallback_successes": 0,
            "tool_loop_calls": 0, "tool_loop_successes": 0,
            "terminal_validation_rejections": 0, "bedrock_retries": 0,
            "retry_exhaustions": 0, "non_retryable_errors": 0,
            "prompt_safety_checked": 0, "prompt_safety_present": 0,
            "prompt_safety_missing": 0, "prompt_safety_coverage": 0.0,
            "review_model_followups": {}, "fuzzer_raw_log_fallbacks": 0,
            "instrumentation_gaps": []},
        "state_health": {"monitor_watermarks": 0, "recent_watermarks": [], "input_warnings": []},
        "trends": {
            "labels": [], "window_days": 7,
            "failure_rate": {"rates": [], "average_rate": 0.0},
            "review_health": {"scores": [], "degraded_reviews": [], "average_score": 0.0},
            "flaky_subsystems": {"top_subsystems": [], "series": {}}},
        "daily_health": {
            "repo": "", "workflow": "", "branch": "", "dates": [],
            "total_runs": 0, "failed_runs": 0, "unique_failures": 0,
            "days_with_runs": 0, "workflows": [], "workflow_reports": [],
            "heatmap": [], "runs": [], "tests": {}, "failure_jobs": {}},
        "wow_trends": {
            "has_data": False, "this_week": _empty_week(), "last_week": _empty_week(),
            "delta": 0, "pct_change": 0.0,
            "new_failures": [], "resolved_failures": [], "top_movers": []},
    }


def build_partial():
    base = build_empty()
    base["daily_health"] = {
        "repo": "valkey-io/valkey", "workflow": "daily.yml", "branch": "unstable",
        "dates": ["2026-04-28", "2026-04-29"],
        "total_runs": 2, "failed_runs": 1, "unique_failures": 1,
        "days_with_runs": 2, "workflows": ["daily.yml"], "workflow_reports": [],
        "heatmap": [{"name": "timeout test", "days_failed": 1, "total_days": 2,
                     "cells": [{"date": "2026-04-28", "count": 0, "has_run": True},
                               {"date": "2026-04-29", "count": 1, "has_run": True}]}],
        "runs": [{"date": "2026-04-29", "workflow": "daily.yml", "status": "failure",
                  "commit_sha": "abc1234", "full_sha": "abc1234def5678901234567890abcdef12345678",
                  "unique_failures": 1, "failed_jobs": 1, "failed_job_names": ["test-basic"],
                  "run_id": "99999", "run_url": "https://github.com/valkey-io/valkey/actions/runs/99999",
                  "failure_names": ["timeout test"], "commits_since_prev": []}],
        "tests": {}, "failure_jobs": {},
    }
    base["ci_failures"]["failure_incidents"] = 1
    base["ci_failures"]["recent_incidents"] = [
        {"failure_identifier": "timeout test", "status": "queued",
         "file_path": "tests/unit/timeout.tcl", "pr_url": "",
         "updated_at": "2026-04-29T10:00:00+00:00"}]
    base["snapshot"]["failure_incidents"] = 1
    return base


def main():
    out = Path("fixtures/dashboard")
    out.mkdir(parents=True, exist_ok=True)
    for name, builder in [("full", build_full), ("empty", build_empty), ("partial", build_partial)]:
        path = out / "{}.json".format(name)
        path.write_text(json.dumps(builder(), indent=2, sort_keys=False) + "\n", encoding="utf-8")
        print("wrote {} ({:,} bytes)".format(path, path.stat().st_size))


if __name__ == "__main__":
    main()

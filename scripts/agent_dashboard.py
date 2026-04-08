"""Static capability dashboard for the CI agent.

The agent already writes durable JSON state for failure handling, flaky-test
campaigns, PR review state, rate limiting, and monitor watermarks. This module
pulls those snapshots into one Markdown/JSON report so maintainers can see
what the bot is doing instead of inferring it from scattered artifacts.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from scripts.event_ledger import parse_events


JsonObject = dict[str, Any]

_TERMINAL_CAMPAIGN_STATUSES = {"abandoned", "merged", "pr-created", "validated"}
_SNAPSHOT_LABELS = {
    "failure_incidents": "Failure incidents",
    "queued_failures": "Queued failures",
    "active_flaky_campaigns": "Active flaky campaigns",
    "tracked_review_prs": "Tracked review PRs",
    "review_comments": "Review comments",
    "fuzzer_runs_analyzed": "Fuzzer runs analyzed",
    "fuzzer_anomalous_runs": "Fuzzer anomalous runs",
    "daily_runs_seen": "Daily runs seen",
    "ai_token_usage": "AI token usage",
    "agent_events": "Agent events",
    "instrumentation_gaps": "Instrumentation gaps",
}


def _mapping(value: Any) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_text(value: bool) -> str:
    return "yes" if value else "no"


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def _load_json(path: str | None) -> tuple[Any | None, str | None]:
    """Load one JSON file, returning a human-readable warning on failure."""
    if not path:
        return None, None
    file_path = Path(path)
    if not file_path.exists():
        return None, f"{path} was not present"
    try:
        return json.loads(file_path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return None, f"{path} could not be parsed as JSON: {exc}"
    except OSError as exc:
        return None, f"{path} could not be read: {exc}"


def _load_many(paths: list[str]) -> tuple[list[Any], list[str]]:
    payloads: list[Any] = []
    warnings: list[str] = []
    for path in paths:
        payload, warning = _load_json(path)
        if warning:
            warnings.append(warning)
        if payload is not None:
            payloads.append(payload)
    return payloads, warnings


def _load_event_logs(paths: list[str]) -> tuple[list[JsonObject], list[str]]:
    events: list[JsonObject] = []
    warnings: list[str] = []
    for path in paths:
        if not path:
            continue
        file_path = Path(path)
        if not file_path.exists():
            warnings.append(f"{path} was not present")
            continue
        try:
            events.extend(parse_events(file_path.read_text(encoding="utf-8")))
        except OSError as exc:
            warnings.append(f"{path} could not be read: {exc}")
    return events, warnings


def _failure_entries(failure_store: JsonObject) -> list[JsonObject]:
    entries = failure_store.get("entries", {})
    if isinstance(entries, dict):
        return [_mapping(value) for value in entries.values() if isinstance(value, dict)]
    return []


def _failure_history(failure_store: JsonObject) -> list[JsonObject]:
    history = failure_store.get("history", {})
    if isinstance(history, dict):
        return [_mapping(value) for value in history.values() if isinstance(value, dict)]
    return []


def _flaky_campaigns(failure_store: JsonObject) -> list[JsonObject]:
    campaigns = failure_store.get("campaigns", {})
    if isinstance(campaigns, dict):
        return [_mapping(value) for value in campaigns.values() if isinstance(value, dict)]
    return []


def _recent(items: list[JsonObject], timestamp_key: str, *, limit: int) -> list[JsonObject]:
    return sorted(
        items,
        key=lambda item: _str(item.get(timestamp_key)),
        reverse=True,
    )[:limit]


def _daily_runs(daily_results: list[JsonObject]) -> list[JsonObject]:
    runs: list[JsonObject] = []
    for result in daily_results:
        runs.extend(
            _mapping(run)
            for run in _list(result.get("runs"))
            if isinstance(run, dict)
        )
    return runs


def _fuzzer_runs(fuzzer_results: list[JsonObject]) -> list[JsonObject]:
    runs: list[JsonObject] = []
    for result in fuzzer_results:
        runs.extend(
            _mapping(run)
            for run in _list(result.get("runs"))
            if isinstance(run, dict)
        )
    return runs


def _acceptance_results(acceptance_payloads: list[JsonObject]) -> list[JsonObject]:
    results: list[JsonObject] = []
    for payload in acceptance_payloads:
        results.extend(
            _mapping(result)
            for result in _list(payload.get("results"))
            if isinstance(result, dict)
        )
    return results


def _build_ci_failure_metrics(
    failure_store: JsonObject,
    rate_state: JsonObject,
    daily_results: list[JsonObject],
) -> JsonObject:
    entries = _failure_entries(failure_store)
    history_entries = _failure_history(failure_store)
    daily_runs = _daily_runs(daily_results)
    entry_status_counts = Counter(_str(entry.get("status"), "unknown") for entry in entries)
    daily_action_counts = Counter(_str(run.get("action"), "unknown") for run in daily_runs)
    daily_conclusion_counts = Counter(
        _str(run.get("conclusion"), "unknown") for run in daily_runs
    )
    job_outcomes: Counter[str] = Counter()
    for run in daily_runs:
        for outcome in _list(run.get("job_outcomes")):
            if isinstance(outcome, dict):
                job_outcomes[_str(outcome.get("outcome"), "unknown")] += 1

    history_observations = 0
    history_failures = 0
    history_passes = 0
    for history in history_entries:
        observations = [
            _mapping(observation)
            for observation in _list(history.get("observations"))
            if isinstance(observation, dict)
        ]
        history_observations += len(observations)
        history_failures += sum(
            1 for observation in observations if observation.get("outcome") == "fail"
        )
        history_passes += sum(
            1 for observation in observations if observation.get("outcome") == "pass"
        )

    queued_failures = [
        _str(fingerprint)
        for fingerprint in _list(rate_state.get("queued_failures"))
    ]
    return {
        "failure_incidents": len(entries),
        "entry_status_counts": _counter_dict(entry_status_counts),
        "history_entries": len(history_entries),
        "history_observations": history_observations,
        "history_failures": history_failures,
        "history_passes": history_passes,
        "queued_failures": len(queued_failures),
        "queued_failure_fingerprints": queued_failures[:20],
        "daily_result_files": len(daily_results),
        "daily_runs_seen": len(daily_runs),
        "daily_action_counts": _counter_dict(daily_action_counts),
        "daily_conclusion_counts": _counter_dict(daily_conclusion_counts),
        "daily_job_outcome_counts": _counter_dict(job_outcomes),
        "recent_incidents": _recent(entries, "updated_at", limit=10),
    }


def _build_flaky_metrics(failure_store: JsonObject) -> JsonObject:
    campaigns = _flaky_campaigns(failure_store)
    status_counts = Counter(
        _str(campaign.get("status"), "unknown") for campaign in campaigns
    )
    active = [
        campaign
        for campaign in campaigns
        if _str(campaign.get("status"), "active") not in _TERMINAL_CAMPAIGN_STATUSES
    ]
    failed_hypotheses = sum(
        len(_list(campaign.get("failed_hypotheses"))) for campaign in campaigns
    )
    attempts = sum(_int(campaign.get("total_attempts")) for campaign in campaigns)
    validation_passes = sum(
        _int(campaign.get("consecutive_full_passes")) for campaign in campaigns
    )
    return {
        "campaigns": len(campaigns),
        "active_campaigns": len(active),
        "status_counts": _counter_dict(status_counts),
        "total_attempts": attempts,
        "failed_hypotheses": failed_hypotheses,
        "consecutive_full_passes": validation_passes,
        "recent_campaigns": _recent(campaigns, "updated_at", limit=12),
    }


def _build_review_metrics(review_state: JsonObject, acceptance_results: list[JsonObject]) -> JsonObject:
    states = [
        _mapping(value)
        for value in review_state.values()
        if isinstance(value, dict)
    ]
    total_review_comments = sum(
        len(_list(state.get("review_comment_ids"))) for state in states
    )
    summary_comments = sum(
        1 for state in states if state.get("summary_comment_id") is not None
    )
    model_followups: Counter[str] = Counter()
    acceptance_passed = 0
    acceptance_failed = 0
    coverage_incomplete = 0
    findings_seen = 0
    for result in acceptance_results:
        if bool(result.get("passed")):
            acceptance_passed += 1
        else:
            acceptance_failed += 1
        findings_seen += len(_list(result.get("findings")))
        for followup in _list(result.get("model_followups")):
            model_followups[_str(followup, "unknown")] += 1
        coverage = _mapping(result.get("coverage"))
        coverage_complete = (
            not _list(coverage.get("claimed_without_tool"))
            and not _list(coverage.get("unaccounted_files"))
            and not bool(coverage.get("fetch_limit_hit"))
        )
        if coverage and not coverage_complete:
            coverage_incomplete += 1

    return {
        "tracked_prs": len(states),
        "summary_comments": summary_comments,
        "review_comments": total_review_comments,
        "recent_reviews": _recent(states, "updated_at", limit=10),
        "acceptance_cases": len(acceptance_results),
        "acceptance_passed": acceptance_passed,
        "acceptance_failed": acceptance_failed,
        "acceptance_findings": findings_seen,
        "coverage_incomplete_cases": coverage_incomplete,
        "model_followup_counts": _counter_dict(model_followups),
    }


def _build_fuzzer_metrics(fuzzer_results: list[JsonObject]) -> JsonObject:
    runs = _fuzzer_runs(fuzzer_results)
    status_counts: Counter[str] = Counter()
    conclusion_counts = Counter(_str(run.get("conclusion"), "unknown") for run in runs)
    issue_action_counts: Counter[str] = Counter()
    scenario_counts: Counter[str] = Counter()
    root_cause_counts: Counter[str] = Counter()
    raw_log_fallbacks = 0
    analyzed = 0
    recent_anomalies: list[JsonObject] = []
    for run in runs:
        analysis = _mapping(run.get("analysis"))
        if analysis:
            analyzed += 1
            status = _str(analysis.get("overall_status"), "unknown")
            status_counts[status] += 1
            scenario_counts[_str(analysis.get("scenario_id"), "unknown")] += 1
            root_cause_counts[_str(analysis.get("root_cause_category"), "unknown")] += 1
            if bool(analysis.get("raw_log_fallback_used")):
                raw_log_fallbacks += 1
            if status != "normal":
                recent_anomalies.append(
                    {
                        "run_id": run.get("run_id"),
                        "run_url": analysis.get("run_url") or run.get("html_url"),
                        "status": status,
                        "scenario_id": analysis.get("scenario_id"),
                        "seed": analysis.get("seed"),
                        "root_cause_category": analysis.get("root_cause_category"),
                        "summary": analysis.get("summary"),
                        "issue_url": run.get("issue_url"),
                        "issue_action": run.get("issue_action"),
                    }
                )
        if run.get("issue_action"):
            issue_action_counts[_str(run.get("issue_action"), "unknown")] += 1

    return {
        "result_files": len(fuzzer_results),
        "runs_seen": len(runs),
        "runs_analyzed": analyzed,
        "status_counts": _counter_dict(status_counts),
        "conclusion_counts": _counter_dict(conclusion_counts),
        "issue_action_counts": _counter_dict(issue_action_counts),
        "scenario_counts": _counter_dict(scenario_counts),
        "root_cause_counts": _counter_dict(root_cause_counts),
        "raw_log_fallbacks": raw_log_fallbacks,
        "recent_anomalies": recent_anomalies[:10],
    }


def _build_agent_outcome_metrics(events: list[JsonObject]) -> JsonObject:
    """Build outcome-oriented metrics from the append-only event ledger."""
    event_type_counts = Counter(
        _str(event.get("event_type"), "unknown")
        for event in events
    )
    subject_counts = Counter(
        _str(event.get("subject"), "unknown")
        for event in events
    )
    pr_created = event_type_counts.get("pr.created", 0)
    pr_merged = event_type_counts.get("pr.merged", 0)
    pr_closed_without_merge = event_type_counts.get("pr.closed_without_merge", 0)
    validation_passed = event_type_counts.get("validation.passed", 0)
    validation_failed = event_type_counts.get("validation.failed", 0)
    dead_lettered = event_type_counts.get("fix.dead_lettered", 0)
    recent_events = _recent(events, "created_at", limit=15)
    return {
        "events": len(events),
        "event_type_counts": _counter_dict(event_type_counts),
        "subjects": len(subject_counts),
        "validation_passed": validation_passed,
        "validation_failed": validation_failed,
        "prs_created": pr_created,
        "prs_merged": pr_merged,
        "prs_closed_without_merge": pr_closed_without_merge,
        "dead_lettered": dead_lettered,
        "recent_events": recent_events,
    }


def _build_ai_reliability_metrics(
    rate_state: JsonObject,
    review_metrics: JsonObject,
    fuzzer_metrics: JsonObject,
) -> JsonObject:
    raw_ai_metrics = _mapping(rate_state.get("ai_metrics"))
    ai_metrics = {
        str(key): _int(value)
        for key, value in raw_ai_metrics.items()
    }
    prompt_safety_checked = ai_metrics.get("bedrock.prompt_safety_guard.checked", 0)
    prompt_safety_present = ai_metrics.get("bedrock.prompt_safety_guard.present", 0)
    prompt_safety_missing = ai_metrics.get("bedrock.prompt_safety_guard.missing", 0)
    prompt_safety_coverage = (
        round(prompt_safety_present / prompt_safety_checked, 4)
        if prompt_safety_checked
        else 0.0
    )
    return {
        "token_usage": _int(rate_state.get("token_usage")),
        "token_window_start": _str(rate_state.get("token_window_start"), "unknown"),
        "ai_metrics": ai_metrics,
        "schema_calls": ai_metrics.get("bedrock.invoke_schema.calls", 0),
        "schema_successes": ai_metrics.get("bedrock.invoke_schema.success", 0),
        "schema_tool_choice_rejections": ai_metrics.get(
            "bedrock.schema_tool_choice_rejected",
            0,
        ),
        "schema_tool_choice_fallback_successes": ai_metrics.get(
            "bedrock.schema_tool_choice_fallback_success",
            0,
        ),
        "tool_loop_calls": ai_metrics.get("bedrock.tool_loop.calls", 0),
        "tool_loop_successes": ai_metrics.get("bedrock.tool_loop.success", 0),
        "terminal_validation_rejections": ai_metrics.get(
            "bedrock.tool_loop.terminal_validation_rejections",
            0,
        ),
        "bedrock_retries": ai_metrics.get("bedrock.retries", 0),
        "retry_exhaustions": ai_metrics.get("bedrock.errors.retry_exhausted", 0),
        "non_retryable_errors": ai_metrics.get("bedrock.errors.non_retryable", 0),
        "prompt_safety_checked": prompt_safety_checked,
        "prompt_safety_present": prompt_safety_present,
        "prompt_safety_missing": prompt_safety_missing,
        "prompt_safety_coverage": prompt_safety_coverage,
        "review_model_followups": review_metrics["model_followup_counts"],
        "fuzzer_raw_log_fallbacks": fuzzer_metrics["raw_log_fallbacks"],
        "instrumentation_gaps": [],
    }


def _build_state_health(
    monitor_state: JsonObject,
    input_warnings: list[str],
) -> JsonObject:
    entries = [
        {"key": key, **_mapping(value)}
        for key, value in monitor_state.items()
        if isinstance(value, dict)
    ]
    return {
        "monitor_watermarks": len(entries),
        "recent_watermarks": _recent(entries, "updated_at", limit=10),
        "input_warnings": input_warnings,
    }


def build_dashboard(
    *,
    failure_store: JsonObject | None = None,
    rate_state: JsonObject | None = None,
    monitor_state: JsonObject | None = None,
    review_state: JsonObject | None = None,
    daily_results: list[JsonObject] | None = None,
    fuzzer_results: list[JsonObject] | None = None,
    acceptance_results: list[JsonObject] | None = None,
    events: list[JsonObject] | None = None,
    input_warnings: list[str] | None = None,
    generated_at: str | None = None,
) -> JsonObject:
    """Build a structured dashboard payload from state/artifact snapshots."""
    failure_store = failure_store or {}
    rate_state = rate_state or {}
    monitor_state = monitor_state or {}
    review_state = review_state or {}
    daily_results = daily_results or []
    fuzzer_results = fuzzer_results or []
    acceptance_results = acceptance_results or []
    events = events or []
    input_warnings = input_warnings or []

    ci_failures = _build_ci_failure_metrics(
        failure_store,
        rate_state,
        daily_results,
    )
    flaky_tests = _build_flaky_metrics(failure_store)
    review_metrics = _build_review_metrics(review_state, acceptance_results)
    fuzzer_metrics = _build_fuzzer_metrics(fuzzer_results)
    agent_outcomes = _build_agent_outcome_metrics(events)
    ai_reliability = _build_ai_reliability_metrics(
        rate_state,
        review_metrics,
        fuzzer_metrics,
    )
    state_health = _build_state_health(monitor_state, input_warnings)
    snapshot = {
        "failure_incidents": ci_failures["failure_incidents"],
        "queued_failures": ci_failures["queued_failures"],
        "active_flaky_campaigns": flaky_tests["active_campaigns"],
        "tracked_review_prs": review_metrics["tracked_prs"],
        "review_comments": review_metrics["review_comments"],
        "fuzzer_runs_analyzed": fuzzer_metrics["runs_analyzed"],
        "fuzzer_anomalous_runs": fuzzer_metrics["status_counts"].get("anomalous", 0),
        "daily_runs_seen": ci_failures["daily_runs_seen"],
        "ai_token_usage": ai_reliability["token_usage"],
        "agent_events": agent_outcomes["events"],
        "instrumentation_gaps": len(ai_reliability["instrumentation_gaps"]),
    }
    return {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "snapshot": snapshot,
        "ci_failures": ci_failures,
        "flaky_tests": flaky_tests,
        "pr_reviews": review_metrics,
        "fuzzer": fuzzer_metrics,
        "agent_outcomes": agent_outcomes,
        "ai_reliability": ai_reliability,
        "state_health": state_health,
    }


def _escape_cell(value: object) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", "<br>")


def _table(headers: list[str], rows: list[list[object]], *, empty: str) -> str:
    if not rows:
        return empty
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_escape_cell(value) for value in row) + " |")
    return "\n".join(lines)


def _link(label: object, url: object) -> str:
    label_text = _escape_cell(label)
    url_text = _str(url)
    if not url_text:
        return label_text
    return f"[{label_text}]({url_text})"


def _status_counts_text(counts: JsonObject) -> str:
    if not counts:
        return "none"
    return ", ".join(f"`{key}`: {value}" for key, value in sorted(counts.items()))


def render_markdown(dashboard: JsonObject) -> str:
    """Render the dashboard payload as GitHub-flavored Markdown."""
    snapshot = _mapping(dashboard.get("snapshot"))
    ci_failures = _mapping(dashboard.get("ci_failures"))
    flaky_tests = _mapping(dashboard.get("flaky_tests"))
    pr_reviews = _mapping(dashboard.get("pr_reviews"))
    fuzzer = _mapping(dashboard.get("fuzzer"))
    agent_outcomes = _mapping(dashboard.get("agent_outcomes"))
    ai_reliability = _mapping(dashboard.get("ai_reliability"))
    state_health = _mapping(dashboard.get("state_health"))

    lines: list[str] = [
        "# CI Agent Capability Dashboard",
        "",
        f"Generated at: `{_str(dashboard.get('generated_at'), 'unknown')}`",
        "",
        "## Executive Snapshot",
        "",
        _table(
            ["Metric", "Value"],
            [
                [_SNAPSHOT_LABELS.get(key, key.replace("_", " ")), value]
                for key, value in snapshot.items()
            ],
            empty="No snapshot metrics were available.",
        ),
        "",
        "## Flaky Test Dashboard",
        "",
        (
            f"Campaigns: **{flaky_tests.get('campaigns', 0)}** total, "
            f"**{flaky_tests.get('active_campaigns', 0)}** active. "
            f"Status counts: {_status_counts_text(_mapping(flaky_tests.get('status_counts')))}."
        ),
        "",
        _table(
            [
                "Failure",
                "Status",
                "Job",
                "Branch",
                "Attempts",
                "Full Passes",
                "Failed Hypotheses",
                "Queued PR",
                "Updated",
            ],
            [
                [
                    campaign.get("failure_identifier", ""),
                    campaign.get("status", ""),
                    campaign.get("job_name", ""),
                    campaign.get("branch", ""),
                    campaign.get("total_attempts", 0),
                    campaign.get("consecutive_full_passes", 0),
                    len(_list(campaign.get("failed_hypotheses"))),
                    _bool_text(isinstance(campaign.get("queued_pr_payload"), dict)),
                    campaign.get("updated_at", ""),
                ]
                for campaign in _list(flaky_tests.get("recent_campaigns"))
                if isinstance(campaign, dict)
            ],
            empty="No flaky campaigns were present in the supplied failure store.",
        ),
        "",
        "## CI Failure Outcomes",
        "",
        _table(
            ["Signal", "Value"],
            [
                ["Failure incidents", ci_failures.get("failure_incidents", 0)],
                ["Entry status counts", _status_counts_text(_mapping(ci_failures.get("entry_status_counts")))],
                ["History entries", ci_failures.get("history_entries", 0)],
                ["History observations", ci_failures.get("history_observations", 0)],
                ["Pass observations", ci_failures.get("history_passes", 0)],
                ["Fail observations", ci_failures.get("history_failures", 0)],
                ["Queued failures", ci_failures.get("queued_failures", 0)],
                ["Daily runs seen", ci_failures.get("daily_runs_seen", 0)],
                ["Daily actions", _status_counts_text(_mapping(ci_failures.get("daily_action_counts")))],
                ["Daily job outcomes", _status_counts_text(_mapping(ci_failures.get("daily_job_outcome_counts")))],
            ],
            empty="No CI failure data was available.",
        ),
        "",
        "## Agent Outcome Ledger",
        "",
        _table(
            ["Signal", "Value"],
            [
                ["Events", agent_outcomes.get("events", 0)],
                ["Subjects", agent_outcomes.get("subjects", 0)],
                ["Validation passed", agent_outcomes.get("validation_passed", 0)],
                ["Validation failed", agent_outcomes.get("validation_failed", 0)],
                ["PRs created", agent_outcomes.get("prs_created", 0)],
                ["PRs merged", agent_outcomes.get("prs_merged", 0)],
                [
                    "PRs closed without merge",
                    agent_outcomes.get("prs_closed_without_merge", 0),
                ],
                ["Dead-lettered fixes", agent_outcomes.get("dead_lettered", 0)],
                ["Event types", _status_counts_text(_mapping(agent_outcomes.get("event_type_counts")))],
            ],
            empty="No event ledger data was available.",
        ),
        "",
        _table(
            ["Time", "Type", "Subject", "Attributes"],
            [
                [
                    event.get("created_at", ""),
                    event.get("event_type", ""),
                    event.get("subject", ""),
                    json.dumps(_mapping(event.get("attributes")), sort_keys=True)[:240],
                ]
                for event in _list(agent_outcomes.get("recent_events"))
                if isinstance(event, dict)
            ],
            empty="No recent agent events were present.",
        ),
        "",
        "## PR Review Dashboard",
        "",
        _table(
            ["Signal", "Value"],
            [
                ["Tracked PRs", pr_reviews.get("tracked_prs", 0)],
                ["Summary comments", pr_reviews.get("summary_comments", 0)],
                ["Review comments", pr_reviews.get("review_comments", 0)],
                ["Acceptance cases", pr_reviews.get("acceptance_cases", 0)],
                ["Acceptance passed", pr_reviews.get("acceptance_passed", 0)],
                ["Acceptance failed", pr_reviews.get("acceptance_failed", 0)],
                ["Acceptance findings", pr_reviews.get("acceptance_findings", 0)],
                ["Coverage incomplete cases", pr_reviews.get("coverage_incomplete_cases", 0)],
                ["Model followups", _status_counts_text(_mapping(pr_reviews.get("model_followup_counts")))],
            ],
            empty="No PR review data was available.",
        ),
        "",
        _table(
            ["PR", "Head SHA", "Summary Comment", "Review Comments", "Updated"],
            [
                [
                    f"{state.get('repo', '')}#{state.get('pr_number', '')}",
                    state.get("last_reviewed_head_sha", ""),
                    state.get("summary_comment_id", ""),
                    len(_list(state.get("review_comment_ids"))),
                    state.get("updated_at", ""),
                ]
                for state in _list(pr_reviews.get("recent_reviews"))
                if isinstance(state, dict)
            ],
            empty="No tracked PR review states were present.",
        ),
        "",
        "## Fuzzer Dashboard",
        "",
        _table(
            ["Signal", "Value"],
            [
                ["Result files", fuzzer.get("result_files", 0)],
                ["Runs seen", fuzzer.get("runs_seen", 0)],
                ["Runs analyzed", fuzzer.get("runs_analyzed", 0)],
                ["Status counts", _status_counts_text(_mapping(fuzzer.get("status_counts")))],
                ["Issue actions", _status_counts_text(_mapping(fuzzer.get("issue_action_counts")))],
                ["Root cause categories", _status_counts_text(_mapping(fuzzer.get("root_cause_counts")))],
                ["Raw log fallbacks", fuzzer.get("raw_log_fallbacks", 0)],
            ],
            empty="No fuzzer data was available.",
        ),
        "",
        _table(
            ["Run", "Status", "Scenario", "Seed", "Root Cause", "Issue", "Summary"],
            [
                [
                    _link(anomaly.get("run_id", ""), anomaly.get("run_url", "")),
                    anomaly.get("status", ""),
                    anomaly.get("scenario_id", ""),
                    anomaly.get("seed", ""),
                    anomaly.get("root_cause_category", ""),
                    _link(anomaly.get("issue_action", ""), anomaly.get("issue_url", "")),
                    anomaly.get("summary", ""),
                ]
                for anomaly in _list(fuzzer.get("recent_anomalies"))
                if isinstance(anomaly, dict)
            ],
            empty="No warning or anomalous fuzzer runs were present in supplied artifacts.",
        ),
        "",
        "## AI Reliability Dashboard",
        "",
        _table(
            ["Signal", "Value"],
            [
                ["Token usage", ai_reliability.get("token_usage", 0)],
                ["Token window start", ai_reliability.get("token_window_start", "unknown")],
                ["Schema calls", ai_reliability.get("schema_calls", 0)],
                ["Schema successes", ai_reliability.get("schema_successes", 0)],
                ["Schema toolChoice rejections", ai_reliability.get("schema_tool_choice_rejections", 0)],
                ["Schema fallback successes", ai_reliability.get("schema_tool_choice_fallback_successes", 0)],
                ["Tool-loop calls", ai_reliability.get("tool_loop_calls", 0)],
                ["Tool-loop successes", ai_reliability.get("tool_loop_successes", 0)],
                ["Terminal validation rejections", ai_reliability.get("terminal_validation_rejections", 0)],
                ["Bedrock retries", ai_reliability.get("bedrock_retries", 0)],
                ["Retry exhaustions", ai_reliability.get("retry_exhaustions", 0)],
                ["Non-retryable errors", ai_reliability.get("non_retryable_errors", 0)],
                ["Prompt-safety guard checks", ai_reliability.get("prompt_safety_checked", 0)],
                ["Prompt-safety guard present", ai_reliability.get("prompt_safety_present", 0)],
                ["Prompt-safety guard missing", ai_reliability.get("prompt_safety_missing", 0)],
                ["Prompt-safety guard coverage", ai_reliability.get("prompt_safety_coverage", 0.0)],
                ["Review model followups", _status_counts_text(_mapping(ai_reliability.get("review_model_followups")))],
                ["Fuzzer raw log fallbacks", ai_reliability.get("fuzzer_raw_log_fallbacks", 0)],
            ],
            empty="No AI reliability data was available.",
        ),
        "",
        _table(
            ["Measured AI Event", "Count"],
            [
                [name, count]
                for name, count in sorted(
                    _mapping(ai_reliability.get("ai_metrics")).items()
                )
            ],
            empty="No persisted AI event counters were present.",
        ),
        "",
        "Instrumentation gaps:",
    ]
    gaps = [
        _str(gap)
        for gap in _list(ai_reliability.get("instrumentation_gaps"))
    ]
    lines.extend(f"- {gap}" for gap in gaps)
    if not gaps:
        lines.append("- No instrumentation gaps recorded.")

    lines.extend(
        [
            "",
            "## State Health",
            "",
            _table(
                ["Monitor Key", "Last Seen Run", "Target Repo", "Workflow", "Updated"],
                [
                    [
                        watermark.get("key", ""),
                        watermark.get("last_seen_run_id", ""),
                        watermark.get("target_repo", ""),
                        watermark.get("workflow_file", ""),
                        watermark.get("updated_at", ""),
                    ]
                    for watermark in _list(state_health.get("recent_watermarks"))
                    if isinstance(watermark, dict)
                ],
                empty="No monitor watermarks were present.",
            ),
        ]
    )

    warnings = [
        _str(warning)
        for warning in _list(state_health.get("input_warnings"))
    ]
    if warnings:
        lines.extend(["", "Input warnings:"])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.append("")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--failure-store", default="")
    parser.add_argument("--rate-state", default="")
    parser.add_argument("--monitor-state", default="")
    parser.add_argument("--review-state", default="")
    parser.add_argument("--daily-result", action="append", default=[])
    parser.add_argument("--fuzzer-result", action="append", default=[])
    parser.add_argument("--acceptance-result", action="append", default=[])
    parser.add_argument("--event-log", action="append", default=[])
    parser.add_argument("--output-markdown", default="agent-dashboard.md")
    parser.add_argument("--output-json", default="agent-dashboard.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    input_warnings: list[str] = []
    failure_store, warning = _load_json(args.failure_store)
    if warning:
        input_warnings.append(warning)
    rate_state, warning = _load_json(args.rate_state)
    if warning:
        input_warnings.append(warning)
    monitor_state, warning = _load_json(args.monitor_state)
    if warning:
        input_warnings.append(warning)
    review_state, warning = _load_json(args.review_state)
    if warning:
        input_warnings.append(warning)

    daily_results, warnings = _load_many(args.daily_result)
    input_warnings.extend(warnings)
    fuzzer_results, warnings = _load_many(args.fuzzer_result)
    input_warnings.extend(warnings)
    acceptance_payloads, warnings = _load_many(args.acceptance_result)
    input_warnings.extend(warnings)
    events, warnings = _load_event_logs(args.event_log)
    input_warnings.extend(warnings)

    dashboard = build_dashboard(
        failure_store=_mapping(failure_store),
        rate_state=_mapping(rate_state),
        monitor_state=_mapping(monitor_state),
        review_state=_mapping(review_state),
        daily_results=[_mapping(result) for result in daily_results],
        fuzzer_results=[_mapping(result) for result in fuzzer_results],
        acceptance_results=_acceptance_results(
            [_mapping(result) for result in acceptance_payloads]
        ),
        events=events,
        input_warnings=input_warnings,
    )
    Path(args.output_json).write_text(
        json.dumps(dashboard, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    Path(args.output_markdown).write_text(render_markdown(dashboard), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

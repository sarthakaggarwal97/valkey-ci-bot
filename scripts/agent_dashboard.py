"""Static capability dashboard for the CI agent.

The agent already writes durable JSON state for failure handling, flaky-test
campaigns, PR review state, rate limiting, and monitor watermarks. This module
pulls those snapshots into one Markdown/JSON report so maintainers can see
what the agent is doing instead of inferring it from scattered artifacts.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import html as html_lib
import json
from pathlib import Path
from typing import Any

from scripts.event_ledger import parse_events
from scripts.json_helpers import (
    JsonObject,
    bool_text as _bool_text,
    mapping as _mapping,
    safe_int as _int,
    safe_list as _list,
    safe_str as _str,
)
from scripts.valkey_repo_context import infer_valkey_subsystem


_TERMINAL_CAMPAIGN_STATUSES = {"abandoned", "landed", "merged", "pr-created", "validated"}
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


def _parse_datetime(value: object) -> datetime | None:
    text = _str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _trend_window(
    generated_at: str | None,
    *,
    days: int = 7,
) -> tuple[list[str], dict[str, int]]:
    end_dt = _parse_datetime(generated_at) or datetime.now(timezone.utc)
    end_day = end_dt.date()
    day_values = [
        end_day - timedelta(days=offset)
        for offset in reversed(range(max(1, days)))
    ]
    labels = [day.strftime("%m-%d") for day in day_values]
    index = {day.isoformat(): position for position, day in enumerate(day_values)}
    return labels, index


def _index_day(timestamp: object, day_index: dict[str, int]) -> int | None:
    parsed = _parse_datetime(timestamp)
    if parsed is None:
        return None
    return day_index.get(parsed.date().isoformat())


def _empty_series(length: int) -> list[int]:
    return [0 for _ in range(length)]


def _rate_series(numerators: list[int], denominators: list[int]) -> list[float]:
    return [
        round((num / den), 4) if den else 0.0
        for num, den in zip(numerators, denominators)
    ]


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _build_trend_metrics(
    failure_store: JsonObject,
    events: list[JsonObject],
    *,
    generated_at: str | None,
) -> JsonObject:
    labels, day_index = _trend_window(generated_at)
    failure_counts = _empty_series(len(labels))
    total_counts = _empty_series(len(labels))
    for event in events:
        if _str(event.get("event_type")) != "workflow.run_seen":
            continue
        attributes = _mapping(event.get("attributes"))
        conclusion = _str(attributes.get("conclusion")).lower()
        if conclusion not in {"success", "failure"}:
            continue
        position = _index_day(event.get("created_at"), day_index)
        if position is None:
            continue
        total_counts[position] += 1
        if conclusion == "failure":
            failure_counts[position] += 1
    failure_rates = _rate_series(failure_counts, total_counts)

    subsystem_series: dict[str, list[int]] = {}
    for campaign in _flaky_campaigns(failure_store):
        subsystem = infer_valkey_subsystem(
            [],
            [
                _str(campaign.get("failure_identifier")),
                _str(campaign.get("job_name")),
                _str(campaign.get("branch")),
            ],
        )
        if not subsystem:
            continue
        series = subsystem_series.setdefault(subsystem, _empty_series(len(labels)))
        timestamps = [
            attempt.get("created_at")
            for attempt in _list(campaign.get("attempts"))
            if isinstance(attempt, dict) and attempt.get("created_at")
        ]
        if not timestamps:
            fallback = campaign.get("updated_at") or campaign.get("created_at")
            if fallback:
                timestamps = [fallback]
        for timestamp in timestamps:
            position = _index_day(timestamp, day_index)
            if position is not None:
                series[position] += 1
    top_subsystems = sorted(
        subsystem_series,
        key=lambda name: (sum(subsystem_series[name]), name),
        reverse=True,
    )[:3]

    review_runs = _empty_series(len(labels))
    healthy_reviews = _empty_series(len(labels))
    degraded_reviews = _empty_series(len(labels))
    for event in events:
        position = _index_day(event.get("created_at"), day_index)
        if position is None:
            continue
        event_type = _str(event.get("event_type"))
        attributes = _mapping(event.get("attributes"))
        if event_type == "review.state_saved":
            review_runs[position] += 1
        elif event_type in {"review.comments_posted", "review.approved"}:
            healthy_reviews[position] += 1
        elif event_type in {"review.failed", "review.summary_failed"}:
            degraded_reviews[position] += 1
        elif event_type == "review.note_posted" and _str(attributes.get("note_kind")) in {
            "coverage-incomplete",
            "approval-withheld",
        }:
            degraded_reviews[position] += 1
    review_health = _rate_series(
        healthy_reviews,
        [healthy + degraded for healthy, degraded in zip(healthy_reviews, degraded_reviews)],
    )

    return {
        "labels": labels,
        "window_days": len(labels),
        "failure_rate": {
            "failures": failure_counts,
            "totals": total_counts,
            "rates": failure_rates,
            "average_rate": (
                round(sum(failure_counts) / sum(total_counts), 4)
                if sum(total_counts)
                else 0.0
            ),
        },
        "flaky_subsystems": {
            "top_subsystems": top_subsystems,
            "series": {
                name: subsystem_series[name]
                for name in top_subsystems
            },
        },
        "review_health": {
            "review_runs": review_runs,
            "healthy_reviews": healthy_reviews,
            "degraded_reviews": degraded_reviews,
            "scores": review_health,
            "average_score": _average(review_health),
        },
    }


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
        _str(entry.get("fingerprint"))
        for entry in entries
        if _str(entry.get("status")) in {"queued", "queued-pr-retry"}
        and isinstance(entry.get("queued_pr_payload"), dict)
    ]
    if not queued_failures:
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
    entry_pr_urls = {
        _str(entry.get("fingerprint")): _str(entry.get("pr_url"))
        for entry in _failure_entries(failure_store)
        if _str(entry.get("fingerprint")) and _str(entry.get("pr_url"))
    }
    status_counts = Counter(
        _str(campaign.get("status"), "unknown") for campaign in campaigns
    )
    proof_counts = Counter(
        _str(campaign.get("proof_status"), "none") for campaign in campaigns
        if _str(campaign.get("proof_status"))
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
    subsystem_counts: Counter[str] = Counter()
    recent_campaigns = _recent(campaigns, "updated_at", limit=12)
    for campaign in recent_campaigns:
        subsystem = infer_valkey_subsystem(
            [],
            [
                _str(campaign.get("failure_identifier")),
                _str(campaign.get("job_name")),
                _str(campaign.get("branch")),
            ],
        )
        if subsystem:
            campaign["subsystem"] = subsystem
            subsystem_counts[subsystem] += 1
        pr_url = entry_pr_urls.get(_str(campaign.get("fingerprint")))
        if pr_url:
            campaign["pr_url"] = pr_url
    return {
        "campaigns": len(campaigns),
        "active_campaigns": len(active),
        "status_counts": _counter_dict(status_counts),
        "proof_counts": _counter_dict(proof_counts),
        "subsystem_counts": _counter_dict(subsystem_counts),
        "total_attempts": attempts,
        "failed_hypotheses": failed_hypotheses,
        "consecutive_full_passes": validation_passes,
        "recent_campaigns": recent_campaigns,
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


def _build_acceptance_metrics(acceptance_payloads: list[JsonObject]) -> JsonObject:
    """Build replay-lab and workflow-acceptance metrics from payload snapshots."""
    latest_payload = _mapping(acceptance_payloads[-1]) if acceptance_payloads else {}
    scorecard = _mapping(latest_payload.get("scorecard"))
    manifest = _mapping(latest_payload.get("manifest"))
    workflow_results = [
        _mapping(result)
        for result in _list(latest_payload.get("workflow_results"))
        if isinstance(result, dict)
    ]
    review_results = [
        _mapping(result)
        for result in _list(latest_payload.get("results"))
        if isinstance(result, dict)
    ]
    readiness = _str(scorecard.get("readiness"), "unknown")
    followup_counts: Counter[str] = Counter()
    finding_count = 0
    for result in review_results:
        finding_count += len(_list(result.get("findings")))
        for followup in _list(result.get("model_followups")):
            followup_counts[_str(followup, "unknown")] += 1

    return {
        "payloads_seen": len(acceptance_payloads),
        "readiness": readiness,
        "review_cases": _int(scorecard.get("review_cases")),
        "review_passed": _int(scorecard.get("review_passed")),
        "review_failed": _int(scorecard.get("review_failed")),
        "workflow_cases": _int(scorecard.get("workflow_cases")),
        "workflow_passed": _int(scorecard.get("workflow_passed")),
        "workflow_failed": _int(scorecard.get("workflow_failed")),
        "ci_replay_cases": _int(scorecard.get("ci_replay_cases")),
        "backport_replay_cases": _int(scorecard.get("backport_replay_cases")),
        "manifest_review_cases": len(_list(manifest.get("review_cases"))),
        "manifest_workflow_cases": len(_list(manifest.get("workflow_cases"))),
        "manifest_ci_cases": len(_list(manifest.get("ci_cases"))),
        "manifest_backport_cases": len(_list(manifest.get("backport_cases"))),
        "finding_count": finding_count,
        "model_followup_counts": _counter_dict(followup_counts),
        "recent_review_results": review_results[:12],
        "recent_workflow_results": workflow_results[:12],
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
                        "triage_verdict": analysis.get("triage_verdict"),
                        "suggested_labels": analysis.get("suggested_labels"),
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
    proof_passed = event_type_counts.get("proof.passed", 0)
    proof_failed = event_type_counts.get("proof.failed", 0)
    proof_dispatched = event_type_counts.get("proof.dispatched", 0)
    dead_lettered = event_type_counts.get("fix.dead_lettered", 0)
    recent_events = _recent(events, "created_at", limit=15)
    return {
        "events": len(events),
        "event_type_counts": _counter_dict(event_type_counts),
        "subjects": len(subject_counts),
        "validation_passed": validation_passed,
        "validation_failed": validation_failed,
        "proof_dispatched": proof_dispatched,
        "proof_passed": proof_passed,
        "proof_failed": proof_failed,
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


def _daily_failure_name(job_outcome: JsonObject) -> str:
    failure_identifier = _str(job_outcome.get("failure_identifier")).strip()
    if failure_identifier:
        return failure_identifier
    job_name = _str(job_outcome.get("job_name")).strip()
    if job_name:
        return f"Process error: {job_name}"
    outcome = _str(job_outcome.get("outcome")).strip()
    return outcome or "workflow failure"


def _build_daily_health_fallback(
    daily_results: list[JsonObject],
) -> JsonObject:
    from scripts.daily_health_report import build_report_data

    normalized_runs: list[JsonObject] = []
    repo_full_name = ""
    workflow_files: list[str] = []

    for result in daily_results:
        repo_full_name = repo_full_name or _str(result.get("target_repo"))
        workflow_file = _str(result.get("workflow_file"))
        if workflow_file and workflow_file not in workflow_files:
            workflow_files.append(workflow_file)
        for run in _list(result.get("runs")):
            if not isinstance(run, dict):
                continue
            run_data = _mapping(run)
            created_at = _parse_datetime(run_data.get("created_at"))
            date = (
                created_at.date().isoformat()
                if created_at is not None
                else _str(run_data.get("date"))
            )
            if not date:
                continue

            status = _str(run_data.get("conclusion") or run_data.get("status"), "unknown")
            full_sha = _str(run_data.get("head_sha"))
            job_outcomes = [
                _mapping(item)
                for item in _list(run_data.get("job_outcomes"))
                if isinstance(item, dict)
            ]
            failure_names = sorted({_daily_failure_name(item) for item in job_outcomes if _daily_failure_name(item)})
            failed_job_names = sorted(
                {
                    _str(item.get("job_name")).strip()
                    for item in job_outcomes
                    if _str(item.get("job_name")).strip()
                }
            )
            failed_jobs = len(failed_job_names) or (len(failure_names) if status == "failure" else 0)

            normalized_runs.append(
                {
                    "run_id": run_data.get("run_id"),
                    "date": date,
                    "status": status,
                    "workflow": workflow_file,
                    "commit_sha": full_sha[:7] if full_sha else "",
                    "full_sha": full_sha,
                    "run_url": run_data.get("html_url") or run_data.get("run_url") or "",
                    "total_jobs": failed_jobs,
                    "failed_jobs": failed_jobs,
                    "failed_job_names": failed_job_names,
                    "unique_failures": len(failure_names),
                    "failure_names": failure_names,
                    "failure_jobs": {},
                }
            )

    if not normalized_runs:
        return {}

    run_dates = sorted(
        {
            _str(run.get("date"))
            for run in normalized_runs
            if _str(run.get("date"))
        }
    )
    expected_dates: list[str] | None = None
    if run_dates:
        start_day = datetime.fromisoformat(run_dates[0]).date()
        end_day = datetime.fromisoformat(run_dates[-1]).date()
        span = (end_day - start_day).days + 1
        expected_dates = [
            (start_day + timedelta(days=offset)).isoformat()
            for offset in range(span)
        ]

    return build_report_data(
        normalized_runs,
        repo_full_name=repo_full_name or "valkey-io/valkey",
        workflow_file=", ".join(workflow_files) or "daily monitor",
        branch="unstable",
        expected_dates=expected_dates,
    )


def _coalesce_daily_health(
    daily_health_data: JsonObject,
    daily_results: list[JsonObject],
) -> JsonObject:
    reported = _mapping(daily_health_data)
    derived = _build_daily_health_fallback(daily_results)
    if not reported:
        return derived
    if not derived:
        return reported

    merged = dict(reported)
    for key in ("repo", "workflow", "branch", "generated_at"):
        if not _str(merged.get(key)):
            merged[key] = derived.get(key, "")
    for key in ("dates", "heatmap", "runs"):
        if not _list(merged.get(key)):
            merged[key] = derived.get(key, [])
    for key in ("missing_dates",):
        if not _list(merged.get(key)):
            merged[key] = derived.get(key, [])
    for key in ("workflows", "workflow_reports"):
        if not _list(merged.get(key)):
            merged[key] = derived.get(key, [])
    for key in ("total_runs", "failed_runs", "unique_failures", "days_with_runs"):
        if _int(merged.get(key)) == 0 and _int(derived.get(key)) > 0:
            merged[key] = derived.get(key, 0)

    # Enrich reported runs with monitor's test-level failure names and job data.
    # The reported data (from daily_health_report.py) only has step-level names
    # like "test", "unittest". The derived data (from monitor) has actual test
    # case names like "clients state report follows."
    derived_runs: dict[tuple[str, str], Any] = {
        (_str(r.get("date")), _str(r.get("workflow"))): r
        for r in _list(derived.get("runs"))
    }
    for run in _list(merged.get("runs")):
        key = (_str(run.get("date")), _str(run.get("workflow")))
        dr = derived_runs.get(key)
        if not dr:
            continue
        # Prefer monitor's failure_names if they are more specific
        d_names = _list(dr.get("failure_names"))
        r_names = _list(run.get("failure_names"))
        if d_names and (not r_names or len(d_names) > len(r_names)):
            run["failure_names"] = d_names
            run["unique_failures"] = len(d_names)
        # Merge failure_jobs from monitor
        d_jobs = _mapping(dr.get("failure_jobs"))
        if d_jobs:
            existing = _mapping(run.get("failure_jobs"))
            existing.update(d_jobs)
            run["failure_jobs"] = existing
        # Carry over commits_since_prev and commit_message if available
        for field in ("commits_since_prev", "commit_message"):
            if dr.get(field) and not run.get(field):
                run[field] = dr[field]

    # Rebuild heatmap and workflow_reports from enriched runs when derived
    # data has more specific failure names.
    d_heatmap = _list(derived.get("heatmap"))
    r_heatmap = _list(merged.get("heatmap"))
    if d_heatmap and r_heatmap:
        d_names_set = {_str(row.get("name")) for row in d_heatmap}
        r_names_set = {_str(row.get("name")) for row in r_heatmap}
        # If derived has more rows (more specific names), prefer it
        if len(d_names_set) > len(r_names_set):
            merged["heatmap"] = d_heatmap
    d_reports = _list(derived.get("workflow_reports"))
    r_reports = _list(merged.get("workflow_reports"))
    if d_reports and r_reports:
        d_total = sum(len(_list(wr.get("heatmap"))) for wr in d_reports)
        r_total = sum(len(_list(wr.get("heatmap"))) for wr in r_reports)
        if d_total > r_total:
            merged["workflow_reports"] = d_reports

    return merged


def build_dashboard(
    *,
    failure_store: JsonObject | None = None,
    rate_state: JsonObject | None = None,
    monitor_state: JsonObject | None = None,
    review_state: JsonObject | None = None,
    daily_results: list[JsonObject] | None = None,
    fuzzer_results: list[JsonObject] | None = None,
    acceptance_results: list[JsonObject] | None = None,
    acceptance_payloads: list[JsonObject] | None = None,
    events: list[JsonObject] | None = None,
    input_warnings: list[str] | None = None,
    generated_at: str | None = None,
    daily_health_data: JsonObject | None = None,
) -> JsonObject:
    """Build a structured dashboard payload from state/artifact snapshots."""
    failure_store = failure_store or {}
    rate_state = rate_state or {}
    monitor_state = monitor_state or {}
    review_state = review_state or {}
    daily_results = daily_results or []
    fuzzer_results = fuzzer_results or []
    acceptance_results = acceptance_results or []
    acceptance_payloads = acceptance_payloads or []
    events = events or []
    input_warnings = input_warnings or []
    resolved_generated_at = generated_at or datetime.now(timezone.utc).isoformat()

    ci_failures = _build_ci_failure_metrics(
        failure_store,
        rate_state,
        daily_results,
    )
    flaky_tests = _build_flaky_metrics(failure_store)
    review_metrics = _build_review_metrics(review_state, acceptance_results)
    acceptance_metrics = _build_acceptance_metrics(acceptance_payloads)
    fuzzer_metrics = _build_fuzzer_metrics(fuzzer_results)
    agent_outcomes = _build_agent_outcome_metrics(events)
    ai_reliability = _build_ai_reliability_metrics(
        rate_state,
        review_metrics,
        fuzzer_metrics,
    )
    state_health = _build_state_health(monitor_state, input_warnings)
    trend_metrics = _build_trend_metrics(
        failure_store,
        events,
        generated_at=resolved_generated_at,
    )
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
        "generated_at": resolved_generated_at,
        "snapshot": snapshot,
        "ci_failures": ci_failures,
        "flaky_tests": flaky_tests,
        "pr_reviews": review_metrics,
        "acceptance": acceptance_metrics,
        "fuzzer": fuzzer_metrics,
        "agent_outcomes": agent_outcomes,
        "ai_reliability": ai_reliability,
        "state_health": state_health,
        "trends": trend_metrics,
        "daily_health": _coalesce_daily_health(_mapping(daily_health_data), daily_results),
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


def _format_series(values: list[object], *, percent: bool = False) -> str:
    if not values:
        return "n/a"
    if percent:
        return " | ".join(f"{float(_str(value, '0')) * 100:.0f}%" for value in values)
    return " | ".join(str(value) for value in values)


def render_markdown(dashboard: JsonObject) -> str:
    """Render the dashboard payload as GitHub-flavored Markdown."""
    snapshot = _mapping(dashboard.get("snapshot"))
    ci_failures = _mapping(dashboard.get("ci_failures"))
    flaky_tests = _mapping(dashboard.get("flaky_tests"))
    trends = _mapping(dashboard.get("trends"))
    pr_reviews = _mapping(dashboard.get("pr_reviews"))
    fuzzer = _mapping(dashboard.get("fuzzer"))
    agent_outcomes = _mapping(dashboard.get("agent_outcomes"))
    ai_reliability = _mapping(dashboard.get("ai_reliability"))
    state_health = _mapping(dashboard.get("state_health"))
    trend_labels = _list(trends.get("labels"))

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
        "## Trend Watch",
        "",
        _table(
            ["Trend", "Window", "Values"],
            [
                [
                    "Failure rate",
                    " | ".join(_str(label) for label in trend_labels),
                    _format_series(
                        _list(_mapping(trends.get("failure_rate")).get("rates")),
                        percent=True,
                    ),
                ],
                [
                    "Review health",
                    " | ".join(_str(label) for label in trend_labels),
                    _format_series(
                        _list(_mapping(trends.get("review_health")).get("scores")),
                        percent=True,
                    ),
                ],
                [
                    "Flaky subsystems",
                    ", ".join(_list(_mapping(trends.get("flaky_subsystems")).get("top_subsystems"))) or "none",
                    "<br>".join(
                        f"{name}: {_format_series(_list(series))}"
                        for name, series in _mapping(
                            _mapping(trends.get("flaky_subsystems")).get("series")
                        ).items()
                    ) or "n/a",
                ],
            ],
            empty="No trend data was available.",
        ),
        "",
        "## Flaky Test Dashboard",
        "",
        (
            f"Campaigns: **{flaky_tests.get('campaigns', 0)}** total, "
            f"**{flaky_tests.get('active_campaigns', 0)}** active. "
            f"Status counts: {_status_counts_text(_mapping(flaky_tests.get('status_counts')))}. "
            f"Subsystems: {_status_counts_text(_mapping(flaky_tests.get('subsystem_counts')))}. "
            f"Proof: {_status_counts_text(_mapping(flaky_tests.get('proof_counts')))}."
        ),
        "",
        _table(
            [
                "Failure",
                "Subsystem",
                "Status",
                "Proof",
                "Job",
                "Branch",
                "Attempts",
                "Full Passes",
                "Proof Runs",
                "Failed Hypotheses",
                "Queued PR",
                "Updated",
            ],
            [
                [
                    campaign.get("failure_identifier", ""),
                    campaign.get("subsystem", ""),
                    campaign.get("status", ""),
                    campaign.get("proof_status", ""),
                    campaign.get("job_name", ""),
                    campaign.get("branch", ""),
                    campaign.get("total_attempts", 0),
                    campaign.get("consecutive_full_passes", 0),
                    (
                        f"{campaign.get('proof_passed_runs', 0)}/"
                        f"{campaign.get('proof_required_runs', 0)}"
                        if _int(campaign.get("proof_required_runs"))
                        else ""
                    ),
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
                ["Proof campaigns dispatched", agent_outcomes.get("proof_dispatched", 0)],
                ["Proof passed", agent_outcomes.get("proof_passed", 0)],
                ["Proof failed", agent_outcomes.get("proof_failed", 0)],
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
            ["Run", "Status", "Triage", "Scenario", "Seed", "Root Cause", "Issue", "Summary"],
            [
                [
                    _link(anomaly.get("run_id", ""), anomaly.get("run_url", "")),
                    anomaly.get("status", ""),
                    anomaly.get("triage_verdict", ""),
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


class _Html(str):
    """Marker for trusted HTML assembled by this module."""


def _safe_html(value: str) -> _Html:
    return _Html(value)


def _html(value: object) -> str:
    return html_lib.escape(_str(value), quote=False)


def _html_attr(value: object) -> str:
    return html_lib.escape(_str(value), quote=True)


def _html_cell(value: object) -> str:
    if isinstance(value, _Html):
        return str(value)
    return _html(value)


def _format_number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return _html(value)


def _format_percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return _html(value)


def _chip(value: object) -> _Html:
    label = _str(value, "unknown") or "unknown"
    normalized = label.lower()
    tone = "neutral"
    if any(word in normalized for word in ["passed", "success", "merged", "normal", "ready"]):
        tone = "good"
    elif any(word in normalized for word in ["failed", "dead", "abandoned", "anomalous", "missing"]):
        tone = "bad"
    elif any(word in normalized for word in ["queued", "retry", "warning", "incomplete", "pending", "running"]):
        tone = "warn"
    return _safe_html(
        f'<span class="chip chip-{tone}">{_html(label)}</span>'
    )


def _html_link(label: object, url: object) -> _Html:
    url_text = _str(url)
    if not url_text:
        return _safe_html(_html(label))
    return _safe_html(
        f'<a href="{_html_attr(url_text)}">{_html(label)}</a>'
    )


def _html_status_counts(counts: JsonObject) -> _Html:
    if not counts:
        return _safe_html('<span class="muted">none</span>')
    chips = [
        f'{_chip(key)} <span class="count">{_format_number(value)}</span>'
        for key, value in sorted(counts.items())
    ]
    return _safe_html('<span class="chip-list">' + "".join(chips) + "</span>")


def _html_table(headers: list[str], rows: list[list[object]], *, empty: str) -> str:
    if not rows:
        return f'<p class="empty">{_html(empty)}</p>'
    head = "".join(f"<th>{_html(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        body_rows.append(
            "<tr>"
            + "".join(f"<td>{_html_cell(value)}</td>" for value in row)
            + "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
    )


def _metric_card(label: str, value: object, *, accent: str = "blue") -> str:
    return (
        f'<article class="metric metric-{_html_attr(accent)}">'
        f'<p>{_html(label)}</p>'
        f'<strong>{_format_number(value)}</strong>'
        "</article>"
    )


def _summary_grid(rows: list[tuple[str, object]]) -> str:
    return (
        '<div class="summary-grid">'
        + "".join(
            f'<div><span>{_html(label)}</span><strong>{_html_cell(value)}</strong></div>'
            for label, value in rows
        )
        + "</div>"
    )


def _panel(title: str, body: str, *, wide: bool = False) -> str:
    class_name = "panel panel-wide" if wide else "panel"
    return (
        f'<section class="{class_name}">'
        f"<h2>{_html(title)}</h2>"
        f"{body}"
        "</section>"
    )


def _sparkline_svg(
    values: list[float],
    *,
    color: str,
    width: int = 220,
    height: int = 54,
) -> _Html:
    if not values:
        return _safe_html('<p class="empty">Not enough history.</p>')
    if len(values) == 1:
        values = [values[0], values[0]]
    max_value = max(values) if values else 0.0
    min_value = min(values) if values else 0.0
    spread = max(max_value - min_value, 0.0001)
    x_step = width / max(len(values) - 1, 1)
    points = []
    for index, value in enumerate(values):
        x = round(index * x_step, 2)
        y = round(height - (((value - min_value) / spread) * (height - 10)) - 5, 2)
        points.append((x, y))
    point_text = " ".join(f"{x},{y}" for x, y in points)
    area_points = f"0,{height} " + point_text + f" {width},{height}"
    circles = "".join(
        f'<circle cx="{x}" cy="{y}" r="2.5" fill="{_html_attr(color)}"></circle>'
        for x, y in points
    )
    return _safe_html(
        '<svg class="sparkline" viewBox="0 0 '
        + f'{width} {height}" preserveAspectRatio="none" aria-hidden="true">'
        + f'<polygon points="{area_points}" fill="{_html_attr(color)}" opacity="0.12"></polygon>'
        + f'<polyline points="{point_text}" fill="none" stroke="{_html_attr(color)}" '
        + 'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></polyline>'
        + circles
        + "</svg>"
    )


def _trend_block(title: str, subtitle: str, chart: _Html, footer: str) -> str:
    return (
        '<div class="trend-block">'
        f'<h3>{_html(title)}</h3>'
        f'<p class="muted">{_html(subtitle)}</p>'
        f"{chart}"
        f'<p class="trend-footer">{_html(footer)}</p>'
        "</div>"
    )


def render_html(dashboard: JsonObject) -> str:
    """Render a polished self-contained HTML dashboard artifact."""
    snapshot = _mapping(dashboard.get("snapshot"))
    ci_failures = _mapping(dashboard.get("ci_failures"))
    flaky_tests = _mapping(dashboard.get("flaky_tests"))
    trends = _mapping(dashboard.get("trends"))
    pr_reviews = _mapping(dashboard.get("pr_reviews"))
    fuzzer = _mapping(dashboard.get("fuzzer"))
    agent_outcomes = _mapping(dashboard.get("agent_outcomes"))
    ai_reliability = _mapping(dashboard.get("ai_reliability"))
    state_health = _mapping(dashboard.get("state_health"))
    generated_at = _str(dashboard.get("generated_at"), "unknown")

    metric_keys = [
        ("failure_incidents", "Failure Incidents", "blue"),
        ("queued_failures", "Queued Failures", "amber"),
        ("active_flaky_campaigns", "Active Flaky Campaigns", "amber"),
        ("tracked_review_prs", "Tracked Review PRs", "blue"),
        ("fuzzer_anomalous_runs", "Fuzzer Anomalies", "red"),
        ("agent_events", "Agent Events", "green"),
        ("ai_token_usage", "AI Token Usage", "blue"),
        ("instrumentation_gaps", "Instrumentation Gaps", "red"),
    ]
    metrics = "".join(
        _metric_card(label, snapshot.get(key, 0), accent=accent)
        for key, label, accent in metric_keys
    )

    trend_labels = _list(trends.get("labels"))
    failure_trend = _mapping(trends.get("failure_rate"))
    review_trend = _mapping(trends.get("review_health"))
    subsystem_trend = _mapping(trends.get("flaky_subsystems"))
    subsystem_series = _mapping(subsystem_trend.get("series"))
    subsystem_palette = ["#38bdf8", "#34d399", "#f59e0b"]
    subsystem_rows = []
    for index, name in enumerate(_list(subsystem_trend.get("top_subsystems"))):
        series = _list(subsystem_series.get(name))
        subsystem_rows.append(
            '<div class="trend-series">'
            f'<span class="legend-dot" style="background:{subsystem_palette[index % len(subsystem_palette)]}"></span>'
            f'<strong>{_html(name)}</strong>'
            f'<span>{_format_number(sum(_int(value) for value in series))} attempts</span>'
            f'{_sparkline_svg([float(_int(value)) for value in series], color=subsystem_palette[index % len(subsystem_palette)])}'
            "</div>"
        )
    trend_panel = _panel(
        "Trend Watch",
        '<div class="trend-grid">'
        + _trend_block(
            "Failure Rate",
            f"Last {len(trend_labels)} days",
            _sparkline_svg(
                [float(value) for value in _list(failure_trend.get("rates"))],
                color="#f87171",
            ),
            (
                f"Average {float(failure_trend.get('average_rate', 0.0)) * 100:.0f}% "
                f"across {sum(_int(value) for value in _list(failure_trend.get('totals')))} observed runs."
            ),
        )
        + _trend_block(
            "Flaky Subsystems",
            ", ".join(_list(subsystem_trend.get("top_subsystems"))) or "No subsystem history yet",
            _safe_html(
                '<div class="trend-series-list">'
                + ("".join(subsystem_rows) if subsystem_rows else '<p class="empty">Not enough history.</p>')
                + "</div>"
            ),
            f"Window labels: {' | '.join(_str(label) for label in trend_labels)}",
        )
        + _trend_block(
            "Review Health",
            "Healthy review outcomes vs degraded ones",
            _sparkline_svg(
                [float(value) for value in _list(review_trend.get("scores"))],
                color="#34d399",
            ),
            (
                f"Average {float(review_trend.get('average_score', 0.0)) * 100:.0f}% healthy "
                f"from {sum(_int(value) for value in _list(review_trend.get('review_runs')))} saved review states."
            ),
        )
        + "</div>",
        wide=True,
    )

    flaky_panel = _panel(
        "Flaky Test Lab",
        _summary_grid([
            ("Campaigns", flaky_tests.get("campaigns", 0)),
            ("Active", flaky_tests.get("active_campaigns", 0)),
            ("Attempts", flaky_tests.get("total_attempts", 0)),
            ("Failed Hypotheses", flaky_tests.get("failed_hypotheses", 0)),
            ("Full Passes", flaky_tests.get("consecutive_full_passes", 0)),
            ("Status", _html_status_counts(_mapping(flaky_tests.get("status_counts")))),
            ("Proof", _html_status_counts(_mapping(flaky_tests.get("proof_counts")))),
            ("Subsystems", _html_status_counts(_mapping(flaky_tests.get("subsystem_counts")))),
        ])
        + _html_table(
            [
                "Failure",
                "Subsystem",
                "Status",
                "Proof",
                "Job",
                "Branch",
                "Attempts",
                "Full Passes",
                "Proof Runs",
                "Failed Hypotheses",
                "Queued PR",
                "Updated",
            ],
            [
                [
                    campaign.get("failure_identifier", ""),
                    campaign.get("subsystem", ""),
                    _chip(campaign.get("status", "")),
                    _chip(campaign.get("proof_status", "")),
                    campaign.get("job_name", ""),
                    campaign.get("branch", ""),
                    campaign.get("total_attempts", 0),
                    campaign.get("consecutive_full_passes", 0),
                    (
                        f"{campaign.get('proof_passed_runs', 0)}/"
                        f"{campaign.get('proof_required_runs', 0)}"
                        if _int(campaign.get("proof_required_runs"))
                        else ""
                    ),
                    len(_list(campaign.get("failed_hypotheses"))),
                    _chip("queued" if isinstance(campaign.get("queued_pr_payload"), dict) else "none"),
                    campaign.get("updated_at", ""),
                ]
                for campaign in _list(flaky_tests.get("recent_campaigns"))
                if isinstance(campaign, dict)
            ],
            empty="No flaky campaigns were present.",
        ),
        wide=True,
    )

    ci_panel = _panel(
        "CI Failure Outcomes",
        _summary_grid([
            ("Incidents", ci_failures.get("failure_incidents", 0)),
            ("Queued", ci_failures.get("queued_failures", 0)),
            ("Daily Runs", ci_failures.get("daily_runs_seen", 0)),
            ("History Entries", ci_failures.get("history_entries", 0)),
            ("Pass Observations", ci_failures.get("history_passes", 0)),
            ("Fail Observations", ci_failures.get("history_failures", 0)),
        ])
        + _html_table(
            ["Signal", "Value"],
            [
                ["Entry Status", _html_status_counts(_mapping(ci_failures.get("entry_status_counts")))],
                ["Daily Actions", _html_status_counts(_mapping(ci_failures.get("daily_action_counts")))],
                ["Daily Conclusions", _html_status_counts(_mapping(ci_failures.get("daily_conclusion_counts")))],
                ["Job Outcomes", _html_status_counts(_mapping(ci_failures.get("daily_job_outcome_counts")))],
            ],
            empty="No CI failure data was available.",
        ),
    )

    # Daily CI Health heatmap panel (from daily_health_report)
    daily_health = _mapping(dashboard.get("daily_health"))
    if daily_health and daily_health.get("heatmap"):
        from scripts.daily_health_report import render_heatmap_panel
        daily_health_panel = _panel(
            "Daily CI Health Trends",
            render_heatmap_panel(daily_health),
            wide=True,
        )
    else:
        daily_health_panel = ""

    outcome_panel = _panel(
        "Agent Outcome Ledger",
        _summary_grid([
            ("Events", agent_outcomes.get("events", 0)),
            ("Subjects", agent_outcomes.get("subjects", 0)),
            ("Validation Passed", agent_outcomes.get("validation_passed", 0)),
            ("Validation Failed", agent_outcomes.get("validation_failed", 0)),
            ("Proof Dispatched", agent_outcomes.get("proof_dispatched", 0)),
            ("Proof Passed", agent_outcomes.get("proof_passed", 0)),
            ("Proof Failed", agent_outcomes.get("proof_failed", 0)),
            ("PRs Created", agent_outcomes.get("prs_created", 0)),
            ("PRs Merged", agent_outcomes.get("prs_merged", 0)),
            ("Closed Without Merge", agent_outcomes.get("prs_closed_without_merge", 0)),
            ("Dead Lettered", agent_outcomes.get("dead_lettered", 0)),
        ])
        + _html_table(
            ["Time", "Type", "Subject", "Attributes"],
            [
                [
                    event.get("created_at", ""),
                    _chip(event.get("event_type", "")),
                    event.get("subject", ""),
                    json.dumps(_mapping(event.get("attributes")), sort_keys=True)[:240],
                ]
                for event in _list(agent_outcomes.get("recent_events"))
                if isinstance(event, dict)
            ],
            empty="No recent agent events were present.",
        ),
        wide=True,
    )

    review_panel = _panel(
        "PR Review Quality",
        _summary_grid([
            ("Tracked PRs", pr_reviews.get("tracked_prs", 0)),
            ("Summary Comments", pr_reviews.get("summary_comments", 0)),
            ("Review Comments", pr_reviews.get("review_comments", 0)),
            ("Acceptance Cases", pr_reviews.get("acceptance_cases", 0)),
            ("Acceptance Passed", pr_reviews.get("acceptance_passed", 0)),
            ("Acceptance Failed", pr_reviews.get("acceptance_failed", 0)),
            ("Coverage Incomplete", pr_reviews.get("coverage_incomplete_cases", 0)),
            ("Model Followups", _html_status_counts(_mapping(pr_reviews.get("model_followup_counts")))),
        ])
        + _html_table(
            ["PR", "Head SHA", "Summary", "Review Comments", "Updated"],
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
    )

    fuzzer_panel = _panel(
        "Fuzzer Watch",
        _summary_grid([
            ("Runs Seen", fuzzer.get("runs_seen", 0)),
            ("Runs Analyzed", fuzzer.get("runs_analyzed", 0)),
            ("Raw Log Fallbacks", fuzzer.get("raw_log_fallbacks", 0)),
            ("Status", _html_status_counts(_mapping(fuzzer.get("status_counts")))),
            ("Issues", _html_status_counts(_mapping(fuzzer.get("issue_action_counts")))),
            ("Root Causes", _html_status_counts(_mapping(fuzzer.get("root_cause_counts")))),
        ])
        + _html_table(
            ["Run", "Status", "Triage", "Scenario", "Seed", "Root Cause", "Issue", "Summary"],
            [
                [
                    _html_link(anomaly.get("run_id", ""), anomaly.get("run_url", "")),
                    _chip(anomaly.get("status", "")),
                    _chip(anomaly.get("triage_verdict", "")),
                    anomaly.get("scenario_id", ""),
                    anomaly.get("seed", ""),
                    anomaly.get("root_cause_category", ""),
                    _html_link(anomaly.get("issue_action", ""), anomaly.get("issue_url", "")),
                    anomaly.get("summary", ""),
                ]
                for anomaly in _list(fuzzer.get("recent_anomalies"))
                if isinstance(anomaly, dict)
            ],
            empty="No warning or anomalous fuzzer runs were present.",
        ),
        wide=True,
    )

    ai_panel = _panel(
        "AI Reliability",
        _summary_grid([
            ("Token Usage", ai_reliability.get("token_usage", 0)),
            ("Schema Calls", ai_reliability.get("schema_calls", 0)),
            ("Schema Successes", ai_reliability.get("schema_successes", 0)),
            ("Tool Loop Calls", ai_reliability.get("tool_loop_calls", 0)),
            ("Tool Loop Successes", ai_reliability.get("tool_loop_successes", 0)),
            ("Terminal Rejections", ai_reliability.get("terminal_validation_rejections", 0)),
            ("Bedrock Retries", ai_reliability.get("bedrock_retries", 0)),
            ("Prompt Safety", _format_percent(ai_reliability.get("prompt_safety_coverage", 0.0))),
        ])
        + _html_table(
            ["Measured AI Event", "Count"],
            [
                [name, count]
                for name, count in sorted(
                    _mapping(ai_reliability.get("ai_metrics")).items()
                )
            ],
            empty="No persisted AI event counters were present.",
        ),
        wide=True,
    )

    state_panel = _panel(
        "State Health",
        _html_table(
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
        )
        + _html_table(
            ["Input Warning"],
            [[warning] for warning in _list(state_health.get("input_warnings"))],
            empty="No input warnings.",
        ),
    )

    css = """
:root {
  color-scheme: dark;
  --bg: #07111f;
  --panel: #0f1b2d;
  --line: #26364f;
  --text: #e7edf7;
  --muted: #95a5bb;
  --blue: #38bdf8;
  --green: #34d399;
  --amber: #f59e0b;
  --red: #f87171;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }
.shell { width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 48px; }
.hero {
  border: 1px solid var(--line);
  background: #0d1a2b;
  padding: 28px;
  border-radius: 8px;
  margin-bottom: 18px;
}
.eyebrow { color: var(--green); font-size: 12px; font-weight: 700; text-transform: uppercase; }
h1, h2 { margin: 0; line-height: 1.1; }
h1 { font-size: 38px; margin-top: 8px; max-width: 780px; }
h2 { font-size: 20px; margin-bottom: 16px; }
.hero p, .muted, .empty { color: var(--muted); }
.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}
.metric {
  background: var(--panel);
  border: 1px solid var(--line);
  border-top: 3px solid var(--blue);
  border-radius: 8px;
  padding: 16px;
}
.metric-green { border-top-color: var(--green); }
.metric-amber { border-top-color: var(--amber); }
.metric-red { border-top-color: var(--red); }
.metric p { margin: 0 0 10px; color: var(--muted); font-size: 12px; text-transform: uppercase; }
.metric strong { display: block; font-size: 28px; }
.layout { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px;
  min-width: 0;
}
.panel-wide { grid-column: 1 / -1; }
.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}
.summary-grid div {
  border-left: 2px solid var(--line);
  padding: 2px 0 2px 10px;
}
.summary-grid span { display: block; color: var(--muted); font-size: 12px; }
.summary-grid strong { display: block; margin-top: 5px; font-size: 18px; overflow-wrap: anywhere; }
.trend-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}
.trend-block {
  min-width: 0;
}
.trend-block h3 {
  margin: 0 0 6px;
  font-size: 16px;
}
.trend-footer {
  margin: 10px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.sparkline {
  width: 100%;
  height: 64px;
  margin-top: 8px;
  display: block;
}
.trend-series-list {
  display: grid;
  gap: 10px;
  margin-top: 10px;
}
.trend-series {
  display: grid;
  grid-template-columns: auto auto 1fr;
  gap: 8px;
  align-items: center;
}
.trend-series strong,
.trend-series span {
  font-size: 12px;
}
.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  display: inline-block;
}
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
table { width: 100%; border-collapse: collapse; min-width: 680px; }
th, td { padding: 11px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { color: #c6d2e2; background: #101f33; font-size: 12px; text-transform: uppercase; }
tr:last-child td { border-bottom: 0; }
td { color: #dce6f3; }
.chip-list { display: inline-flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.chip {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 2px 8px;
  font-size: 12px;
  color: #dbe7f5;
  background: #15243a;
}
.chip-good { color: #bbf7d0; border-color: #166534; background: #052e1a; }
.chip-warn { color: #fde68a; border-color: #92400e; background: #422006; }
.chip-bad { color: #fecaca; border-color: #991b1b; background: #450a0a; }
.count { color: var(--muted); margin-right: 8px; }
@media (max-width: 900px) {
  .metrics, .layout, .summary-grid, .trend-grid { grid-template-columns: 1fr; }
  .panel-wide { grid-column: auto; }
  h1 { font-size: 30px; }
}
"""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CI Agent Capability Dashboard</title>
  <style>{css}</style>
</head>
<body>
  <main class="shell">
    <header class="hero">
      <div class="eyebrow">CI Agent</div>
      <h1>Capability Dashboard</h1>
      <p>Generated at {_html(generated_at)}</p>
    </header>
    <section class="metrics" aria-label="Executive snapshot">{metrics}</section>
    <div class="layout">
      {trend_panel}
      {flaky_panel}
      {daily_health_panel}
      {ci_panel}
      {outcome_panel}
      {review_panel}
      {fuzzer_panel}
      {ai_panel}
      {state_panel}
    </div>
  </main>
</body>
</html>
"""


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
    parser.add_argument("--output-html", default="agent-dashboard.html")
    parser.add_argument("--daily-health", default="", help="Path to daily health report JSON")
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

    daily_health_data, warning = _load_json(args.daily_health)
    if warning:
        input_warnings.append(warning)

    dashboard = build_dashboard(
        failure_store=_mapping(failure_store),
        rate_state=_mapping(rate_state),
        monitor_state=_mapping(monitor_state),
        review_state=_mapping(review_state),
        daily_results=[_mapping(result) for result in daily_results],
        fuzzer_results=[_mapping(result) for result in fuzzer_results],
        acceptance_payloads=[_mapping(result) for result in acceptance_payloads],
        acceptance_results=_acceptance_results(
            [_mapping(result) for result in acceptance_payloads]
        ),
        events=events,
        input_warnings=input_warnings,
        daily_health_data=_mapping(daily_health_data),
    )
    Path(args.output_json).write_text(
        json.dumps(dashboard, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    Path(args.output_markdown).write_text(render_markdown(dashboard), encoding="utf-8")
    Path(args.output_html).write_text(render_html(dashboard), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

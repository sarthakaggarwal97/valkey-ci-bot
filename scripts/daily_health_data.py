"""Pure-data helpers for the daily CI health report.

This module contains the data-transformation functions used to build the daily
health report. It deliberately has no GitHub API imports so it can be used by
``scripts.agent_dashboard`` without pulling in the ``github`` package.

``scripts.daily_health_report`` re-exports these functions for backward
compatibility.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict

JsonObject = Dict[str, Any]


def _workflow_name(value: object) -> str:
    text = str(value or "").strip()
    return text


def _build_report_snapshot(
    runs: list[JsonObject],
    *,
    repo_full_name: str = "",
    workflow_file: str = "",
    branch: str = "",
    expected_dates: list[str] | None = None,
) -> JsonObject:
    """Build one report view from a set of workflow runs."""
    run_dates = sorted(
        {
            str(r.get("date", "")).strip()
            for r in runs
            if str(r.get("date", "")).strip()
            and str(r.get("status", "")).lower() not in ("skipped", "cancelled")
        }
    )
    all_dates = sorted(
        {
            str(r.get("date", "")).strip()
            for r in runs
            if str(r.get("date", "")).strip()
        }
    )
    dates = expected_dates or all_dates

    failure_dates: dict[str, Counter] = defaultdict(Counter)
    failure_total_days: Counter = Counter()
    failure_total_jobs: Counter = Counter()

    for run in runs:
        for name in run["failure_names"]:
            job_ids = run.get("failure_jobs", {}).get(name, [])
            failure_dates[name][run["date"]] += len(job_ids) or 1
            failure_total_days[name] += 1
            failure_total_jobs[name] += len(job_ids) or 1

    sorted_failures = sorted(
        failure_dates.keys(),
        key=lambda n: (-failure_total_days[n], -failure_total_jobs[n], n),
    )

    heatmap: list[JsonObject] = []
    for name in sorted_failures:
        row: JsonObject = {
            "name": name,
            "days_failed": failure_total_days[name],
            "total_days": len(run_dates),
            "cells": [],
        }
        for date in dates:
            count = failure_dates[name].get(date, 0)
            row["cells"].append({
                "date": date,
                "count": count,
                "has_run": date in run_dates,
            })
        heatmap.append(row)

    unique_failures = set()
    for run in runs:
        unique_failures.update(run["failure_names"])

    total_runs = len(runs)
    failed_runs = sum(1 for r in runs if r["status"] != "success")

    return {
        "repo": repo_full_name,
        "workflow": workflow_file,
        "branch": branch,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "dates": dates,
        "days_with_runs": len(run_dates),
        "missing_dates": [date for date in dates if date not in run_dates],
        "total_runs": total_runs,
        "failed_runs": failed_runs,
        "unique_failures": len(unique_failures),
        "heatmap": heatmap,
        "runs": sorted(runs, key=lambda r: r["date"], reverse=True),
    }


def build_report_data(
    runs: list[JsonObject],
    *,
    repo_full_name: str = "",
    workflow_file: str = "",
    branch: str = "",
    expected_dates: list[str] | None = None,
) -> JsonObject:
    """Build the structured report payload from collected runs.

    Returns a dict with the combined report plus workflow-specific views when
    multiple run types are present.
    """
    report = _build_report_snapshot(
        runs,
        repo_full_name=repo_full_name,
        workflow_file=workflow_file,
        branch=branch,
        expected_dates=expected_dates,
    )

    workflow_runs: dict[str, list[JsonObject]] = defaultdict(list)
    for run in runs:
        workflow = _workflow_name(run.get("workflow"))
        if workflow:
            workflow_runs[workflow].append(run)

    workflow_reports = [
        _build_report_snapshot(
            workflow_runs[name],
            repo_full_name=repo_full_name,
            workflow_file=name,
            branch=branch,
            expected_dates=expected_dates,
        )
        for name in sorted(workflow_runs)
    ]
    if workflow_reports:
        report["workflows"] = [item["workflow"] for item in workflow_reports]
        report["workflow_reports"] = workflow_reports
    return report

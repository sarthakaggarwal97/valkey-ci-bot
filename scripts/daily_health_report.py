"""Daily CI health report — test failure trend dashboard.

Fetches recent daily CI workflow runs from the GitHub API, collects
per-job failure information, and renders a self-contained HTML report
with a failure heatmap and per-run detail table.

Can be used standalone or imported by the agent dashboard.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from github import Auth, Github

from scripts.daily_health_history import load_history_runs, merge_runs
from scripts.github_client import retry_github_call

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]

# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------


def _workflow_name(value: object) -> str:
    text = str(value or "").strip()
    return text


def _workflow_label(value: object) -> str:
    text = _workflow_name(value)
    if text.endswith((".yml", ".yaml")):
        text = text.rsplit(".", 1)[0]
    text = text.replace("-", " ").replace("_", " ").strip()
    return text.title() or "Unknown"


def _expected_dates(days: int, *, end_at: datetime | None = None) -> list[str]:
    if days <= 0:
        return []
    end_dt = end_at or datetime.now(timezone.utc)
    end_day = end_dt.date()
    start_day = end_day - timedelta(days=days - 1)
    return [
        (start_day + timedelta(days=offset)).isoformat()
        for offset in range(days)
    ]


def _job_failure_name(job: Any) -> str | None:
    """Extract a failure name from a job's conclusion and annotations."""
    if getattr(job, "conclusion", None) != "failure":
        return None
    # Use the job name as fallback
    return getattr(job, "name", None) or "unknown job"


def _parse_failure_annotations(job: Any) -> list[str]:
    """Extract failure annotations from job steps."""
    failures: list[str] = []
    for step in getattr(job, "steps", []) or []:
        if getattr(step, "conclusion", None) == "failure":
            name = getattr(step, "name", "") or ""
            if name and name not in ("Post actions/checkout@v6",):
                failures.append(name)
    return failures


def fetch_daily_runs(
    github_client: Github,
    repo_full_name: str,
    workflow_file: str,
    branch: str,
    days: int,
) -> list[JsonObject]:
    """Fetch recent workflow runs and their per-job failure details.

    Returns a list of run dicts sorted newest-first, each containing:
    - run_id, date, status, commit_sha, total_jobs, failed_jobs,
      failed_job_names, unique_failures, failure_names, failure_jobs
    """
    repo = retry_github_call(
        lambda: github_client.get_repo(repo_full_name),
        retries=3,
        description=f"get repo {repo_full_name}",
    )

    workflow = retry_github_call(
        lambda: repo.get_workflow(workflow_file),
        retries=3,
        description=f"get workflow {workflow_file}",
    )

    # Fetch both completed and in-progress runs so recent days are never blank.
    completed_runs = retry_github_call(
        lambda: workflow.get_runs(branch=branch, status="completed"),
        retries=3,
        description=f"get completed runs for {workflow_file}",
    )
    in_progress_runs = retry_github_call(
        lambda: workflow.get_runs(branch=branch, status="in_progress"),
        retries=3,
        description=f"get in-progress runs for {workflow_file}",
    )

    collected: list[JsonObject] = []
    seen_dates: set[str] = set()
    now = datetime.now(timezone.utc)
    expected_dates = _expected_dates(days, end_at=now)
    expected_date_set = set(expected_dates)

    # Process in-progress runs first so today's run is never missing.
    # Then completed runs fill in the rest.
    all_runs = itertools.chain(in_progress_runs, completed_runs)

    for run in all_runs:
        run_date = run.created_at.strftime("%Y-%m-%d")
        if expected_date_set and run_date not in expected_date_set:
            continue

        # One run per date (prefer in-progress for today, completed otherwise)
        if run_date in seen_dates:
            continue
        seen_dates.add(run_date)

        # Fetch jobs for this run
        try:
            def _list_jobs() -> list[Any]:
                return list(run.jobs())

            jobs = retry_github_call(
                _list_jobs,
                retries=2,
                description=f"get jobs for run {run.id}",
            )
        except Exception:
            logger.warning("Could not fetch jobs for run %d", run.id)
            jobs = []

        total_jobs = len(jobs)
        failed_job_names: list[str] = []
        # Map: failure_name -> list of job IDs
        failure_jobs: dict[str, list[int]] = defaultdict(list)

        for job in jobs:
            if getattr(job, "conclusion", None) != "failure":
                continue
            failed_job_names.append(getattr(job, "name", "unknown"))
            # Try to get specific test failure names from step annotations
            step_failures = _parse_failure_annotations(job)
            if step_failures:
                for failure in step_failures:
                    failure_jobs[failure].append(job.id)
            else:
                # Use a generic name based on the job
                name = f"Process error: {getattr(job, 'name', 'unknown')}"
                failure_jobs[name].append(job.id)

        failure_names = sorted(failure_jobs.keys())

        # Get commit info
        commit_sha = run.head_sha[:7] if run.head_sha else ""

        collected.append({
            "run_id": run.id,
            "date": run_date,
            "status": run.conclusion or run.status or "unknown",
            "workflow": workflow_file,
            "commit_sha": commit_sha,
            "full_sha": run.head_sha or "",
            "run_url": run.html_url,
            "total_jobs": total_jobs,
            "failed_jobs": len(failed_job_names),
            "failed_job_names": sorted(set(failed_job_names)),
            "unique_failures": len(failure_names),
            "failure_names": failure_names,
            "failure_jobs": {k: v for k, v in failure_jobs.items()},
        })

        if expected_dates and len(seen_dates) >= len(expected_dates):
            break

    # Sort newest first
    collected.sort(key=lambda r: r["date"], reverse=True)
    return collected


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
        }
    )
    dates = expected_dates or run_dates

    # Build heatmap: for each failure name, count occurrences per date
    failure_dates: dict[str, Counter[str]] = defaultdict(Counter)
    failure_total_days: Counter[str] = Counter()
    failure_total_jobs: Counter[str] = Counter()

    for run in runs:
        for name in run["failure_names"]:
            job_ids = run.get("failure_jobs", {}).get(name, [])
            failure_dates[name][run["date"]] += len(job_ids) or 1
            failure_total_days[name] += 1
            failure_total_jobs[name] += len(job_ids) or 1

    # Sort failures by frequency (most frequent first)
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
    failed_runs = sum(1 for r in runs if r["status"] == "failure")

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


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_CSS = """
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
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif;
}
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }
.shell { max-width: 1400px; margin: 0 auto; padding: 24px 16px 48px; }
header {
  border: 1px solid var(--line);
  background: #0d1a2b;
  padding: 24px;
  border-radius: 8px;
  margin-bottom: 16px;
}
header h1 { font-size: 28px; margin-bottom: 4px; }
header .sub { color: var(--muted); font-size: 13px; }
header .desc { color: var(--muted); font-size: 13px; margin-top: 8px; }
.metrics {
  display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap;
}
.metric {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 14px 18px;
  min-width: 140px;
  flex: 1;
}
.metric .label { color: var(--muted); font-size: 11px; text-transform: uppercase; }
.metric .value { font-size: 26px; font-weight: 700; }
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  margin-bottom: 16px;
  overflow: hidden;
}
.panel h2 {
  font-size: 16px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--line);
  background: #101f33;
}
.panel-body { padding: 0; overflow-x: auto; }
.panel-copy {
  padding: 14px 18px 0;
  color: var(--muted);
  font-size: 13px;
}
table { width: 100%; border-collapse: collapse; }
th, td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
  white-space: nowrap;
}
th {
  background: #101f33;
  color: #c6d2e2;
  font-size: 11px;
  text-transform: uppercase;
  position: sticky;
  top: 0;
  z-index: 1;
}
tr:last-child td { border-bottom: 0; }
.test-name {
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}
.freq { color: var(--muted); font-size: 12px; text-align: center; }
.cell {
  text-align: center;
  font-size: 12px;
  font-weight: 600;
  min-width: 52px;
}
.cell-0 { color: #334155; }
.cell-missing {
  color: #64748b;
  background: rgba(100, 116, 139, 0.08);
}
.cell-1 { color: var(--amber); }
.cell-2 { color: #fb923c; }
.cell-3 { color: var(--red); }
.cell-high { color: #ff4444; font-weight: 800; }
.run-status { font-weight: 600; }
.run-fail { color: var(--red); }
.run-pass { color: var(--green); }
.run-missing { color: var(--muted); }
.failures-cell {
  max-width: 500px;
  white-space: normal;
  font-size: 12px;
  line-height: 1.6;
}
.failure-entry { margin-bottom: 2px; }
.job-link {
  font-size: 11px;
  color: var(--blue);
  margin-left: 2px;
}
.workflow-grid {
  display: grid;
  gap: 16px;
  padding: 16px;
}
.workflow-panel {
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  background: #0d1828;
}
.workflow-panel h3 {
  font-size: 15px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--line);
  background: #101f33;
}
.workflow-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 12px;
}
.workflow-meta strong {
  color: var(--text);
}
.jobs-cell {
  max-width: 300px;
  white-space: normal;
  font-size: 12px;
  color: var(--muted);
}
.sha {
  font-family: ui-monospace, monospace;
  font-size: 12px;
  color: var(--blue);
}
details summary { cursor: pointer; color: var(--muted); font-size: 12px; }
details summary:hover { color: var(--text); }
@media (max-width: 900px) {
  .metrics { flex-direction: column; }
  .test-name { max-width: 200px; }
}
"""


def _h(text: str) -> str:
    """HTML-escape a string."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _truncate(text: str, length: int = 60) -> str:
    if len(text) <= length:
        return text
    return text[: length - 3] + "..."


def _render_heatmap(data: JsonObject) -> str:
    """Render the failure heatmap table."""
    dates = data.get("dates", [])
    heatmap = data.get("heatmap", [])
    if not dates or not heatmap:
        return '<p style="padding:18px;color:var(--muted)">No failure data available.</p>'

    # Date headers — show month/day only
    date_headers = []
    for d in dates:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            date_headers.append(dt.strftime("%m/%d"))
        except ValueError:
            date_headers.append(d[-5:])

    lines = ['<table>', '<thead><tr>']
    lines.append('<th class="test-name">Test Failure</th>')
    lines.append('<th class="freq">Freq</th>')
    for dh in date_headers:
        lines.append(f'<th class="cell">{_h(dh)}</th>')
    lines.append('</tr></thead><tbody>')

    for row in heatmap:
        name = row.get("name", "")
        days_failed = row.get("days_failed", 0)
        total_days = row.get("total_days", 0)
        cells = row.get("cells", [])

        lines.append("<tr>")
        lines.append(
            f'<td class="test-name" title="{_h(name)}">{_h(_truncate(name))}</td>'
        )
        lines.append(f'<td class="freq">{days_failed}/{total_days}d</td>')

        for cell in cells:
            count = cell.get("count", 0)
            if not cell.get("has_run", True):
                css = "cell cell-missing"
                label = "—"
            elif count == 0:
                css = "cell cell-0"
                label = "·"
            elif count == 1:
                css = "cell cell-1"
                label = str(count)
            elif count == 2:
                css = "cell cell-2"
                label = str(count)
            elif count <= 5:
                css = "cell cell-3"
                label = str(count)
            else:
                css = "cell cell-high"
                label = str(count)
            lines.append(f'<td class="{css}">{label}</td>')

        lines.append("</tr>")

    lines.append("</tbody></table>")
    return "\n".join(lines)


def _render_run_details(data: JsonObject) -> str:
    """Render the per-run details table."""
    runs = data.get("runs", [])
    expected_dates = [str(item) for item in data.get("dates", []) if str(item)]
    if not runs and not expected_dates:
        return '<p style="padding:18px;color:var(--muted)">No runs available.</p>'

    show_workflow = any(_workflow_name(run.get("workflow")) for run in runs if isinstance(run, dict))
    runs_by_date: dict[str, list[JsonObject]] = defaultdict(list)
    for run in runs:
        if isinstance(run, dict):
            date = str(run.get("date", "")).strip()
            if date:
                runs_by_date[date].append(run)
    ordered_dates = sorted(expected_dates or runs_by_date.keys(), reverse=True)

    lines = ['<table>', '<thead><tr>']
    lines.append("<th>Date</th>")
    if show_workflow:
        lines.append("<th>Run Type</th>")
    lines.append("<th>Status</th>")
    lines.append("<th>Commit</th>")
    lines.append("<th>Failures</th>")
    lines.append("<th>Test Failures</th>")
    lines.append("<th>Failed Jobs</th>")
    lines.append("</tr></thead><tbody>")

    for date in ordered_dates:
        date_runs = runs_by_date.get(date, [])
        if not date_runs:
            lines.append("<tr>")
            lines.append(f"<td>{_h(date)}</td>")
            if show_workflow:
                lines.append("<td>—</td>")
            lines.append('<td class="run-status run-missing">NO DATA</td>')
            lines.append("<td>—</td>")
            lines.append("<td>—</td>")
            lines.append('<td class="failures-cell">No run data was available for this date.</td>')
            lines.append("<td>—</td>")
            lines.append("</tr>")
            continue

        for run in date_runs:
            status = run.get("status", "unknown")
            sha = run.get("commit_sha", "")
            run_url = run.get("run_url", "")
            total_jobs = run.get("total_jobs", 0)
            failed_jobs = run.get("failed_jobs", 0)
            failure_names = run.get("failure_names", [])
            failure_jobs_map = run.get("failure_jobs", {})
            failed_job_names = run.get("failed_job_names", [])

            status_css = "run-fail" if status == "failure" else "run-pass"
            status_label = f"FAIL ({failed_jobs}/{total_jobs})" if status == "failure" else "PASS"
            workflow_label = _workflow_label(run.get("workflow"))

            # Render failure names with job links
            failure_parts = []
            for fname in failure_names[:15]:
                job_ids = failure_jobs_map.get(fname, [])
                job_links = " ".join(
                    f'<a class="job-link" href="https://github.com/valkey-io/valkey/actions/runs/{run.get("run_id", "")}/job/{jid}" '
                    f'target="_blank">[{i + 1}]</a>'
                    for i, jid in enumerate(job_ids[:5])
                )
                failure_parts.append(
                    f'<span class="failure-entry">{_h(_truncate(fname, 70))} {job_links}</span>'
                )
            if len(failure_names) > 15:
                failure_parts.append(
                    f'<span class="failure-entry" style="color:var(--muted)">+{len(failure_names) - 15} more</span>'
                )
            failures_html = "<br>".join(failure_parts) if failure_parts else "—"

            # Failed job names
            shown_jobs = failed_job_names[:5]
            jobs_html = " ".join(_h(j) for j in shown_jobs)
            if len(failed_job_names) > 5:
                jobs_html += f' <span style="color:var(--muted)">+{len(failed_job_names) - 5} more</span>'
            if not jobs_html:
                jobs_html = "—"

            sha_html = f'<a class="sha" href="{_h(run_url)}" target="_blank">{_h(sha)}</a>' if run_url else _h(sha)

            lines.append("<tr>")
            lines.append(f"<td>{_h(date)}</td>")
            if show_workflow:
                lines.append(f"<td>{_h(workflow_label)}</td>")
            lines.append(f'<td class="run-status {status_css}">{status_label}</td>')
            lines.append(f"<td>{sha_html}</td>")
            lines.append(f"<td>{len(failure_names)}</td>")
            lines.append(f'<td class="failures-cell">{failures_html}</td>')
            lines.append(f'<td class="jobs-cell">{jobs_html}</td>')
            lines.append("</tr>")

    lines.append("</tbody></table>")
    return "\n".join(lines)


def _render_workflow_panels(data: JsonObject) -> str:
    workflow_reports = [
        report
        for report in data.get("workflow_reports", [])
        if isinstance(report, dict)
    ]
    if len(workflow_reports) <= 1:
        return ""

    panels: list[str] = []
    for report in workflow_reports:
        workflow = _workflow_label(report.get("workflow"))
        panels.append(
            '<section class="workflow-panel">'
            f"<h3>{_h(workflow)}</h3>"
            '<div class="workflow-meta">'
            f"<span><strong>{report.get('total_runs', 0)}</strong> runs</span>"
            f"<span><strong>{report.get('failed_runs', 0)}</strong> failed</span>"
            f"<span><strong>{report.get('unique_failures', 0)}</strong> unique failures</span>"
            "</div>"
            f'<div class="panel-body">{_render_heatmap(report)}</div>'
            "</section>"
        )
    return '<div class="workflow-grid">' + "".join(panels) + "</div>"


def render_html(data: JsonObject) -> str:
    """Render the full standalone HTML report."""
    repo = data.get("repo", "")
    workflow = data.get("workflow", "")
    branch = data.get("branch", "")
    generated_at = data.get("generated_at", "")
    total_runs = data.get("total_runs", 0)
    failed_runs = data.get("failed_runs", 0)
    days_with_runs = data.get("days_with_runs", 0)
    unique_failures = data.get("unique_failures", 0)

    heatmap_html = _render_heatmap(data)
    workflow_heatmaps_html = _render_workflow_panels(data)
    details_html = _render_run_details(data)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Valkey CI Failure Report — {_h(branch)}</title>
  <style>{_CSS}</style>
</head>
<body>
  <main class="shell">
    <header>
      <h1>Valkey CI Failure Report</h1>
      <div class="sub">{_h(workflow)} · {_h(branch)} · {_h(repo)} · generated {_h(generated_at)}</div>
      <div class="desc">Daily CI failure trends. Tracks which tests fail, how often, and whether they are getting better or worse.</div>
    </header>

    <div class="metrics">
      <div class="metric">
        <div class="label">Runs</div>
        <div class="value">{total_runs}</div>
      </div>
      <div class="metric">
        <div class="label">Failed</div>
        <div class="value" style="color:var(--red)">{failed_runs}</div>
      </div>
      <div class="metric">
        <div class="label">Unique Failures</div>
        <div class="value">{unique_failures}</div>
      </div>
      <div class="metric">
        <div class="label">Days With Data</div>
        <div class="value">{days_with_runs}/{len(data.get("dates", []))}</div>
      </div>
    </div>

    <div class="panel">
      <h2>Failure Heatmap</h2>
      <p class="panel-copy">A dash means no run data was available for that date. A dot means the run was present and that failure did not occur.</p>
      <div class="panel-body">{heatmap_html}</div>
    </div>

    {f'''<div class="panel">
      <h2>Failure Heatmap By Run Type</h2>
      <p class="panel-copy">Failures stay separated by workflow so weekly-only regressions do not blur into daily trends.</p>
      {workflow_heatmaps_html}
    </div>''' if workflow_heatmaps_html else ""}

    <div class="panel">
      <h2>Run Details (newest first)</h2>
      <div class="panel-body">{details_html}</div>
    </div>

    <details style="margin-top:12px">
      <summary>Raw JSON data</summary>
      <pre style="background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;overflow-x:auto;font-size:12px;margin-top:8px">{_h(json.dumps(data, indent=2))}</pre>
    </details>
  </main>
</body>
</html>
"""


def render_heatmap_panel(data: JsonObject) -> str:
    """Render a compact heatmap panel suitable for embedding in the agent dashboard.

    Returns an HTML fragment (no <html>/<body> wrapper).
    """
    dates = data.get("dates", [])
    heatmap = data.get("heatmap", [])
    total_runs = data.get("total_runs", 0)
    failed_runs = data.get("failed_runs", 0)
    unique_failures = data.get("unique_failures", 0)
    branch = data.get("branch", "unstable")

    summary = (
        f'<div style="display:flex;gap:24px;padding:12px 0;font-size:13px;color:var(--muted)">'
        f"<span>{total_runs} runs</span>"
        f'<span style="color:var(--red)">{failed_runs} failed</span>'
        f"<span>{unique_failures} unique failures</span>"
        f"<span>branch: {_h(branch)}</span>"
        f"</div>"
    )

    if not dates or not heatmap:
        return summary + '<p style="color:var(--muted)">No daily CI failure data available.</p>'

    return summary + _render_heatmap(data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a daily CI health report for Valkey.",
    )
    parser.add_argument(
        "--repo",
        default="valkey-io/valkey",
        help="Repository full name (default: valkey-io/valkey)",
    )
    parser.add_argument(
        "--workflow",
        default=["daily.yml"],
        nargs="+",
        help="Workflow file name(s) (default: daily.yml). Pass multiple to combine.",
    )
    parser.add_argument(
        "--branch",
        default="unstable",
        help="Branch to report on (default: unstable)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Number of days to look back (default: 14)",
    )
    parser.add_argument(
        "--token",
        default="",
        help="GitHub token (or set GITHUB_TOKEN env var)",
    )
    parser.add_argument(
        "--output",
        default="daily-health-report.html",
        help="Output HTML file path",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="Optional: also write JSON data to this path",
    )
    parser.add_argument(
        "--history-dir",
        default="",
        help="Optional local checkout of durable daily-health history snapshots.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    import os
    token = args.token or os.environ.get("GITHUB_TOKEN", "")
    expected_dates = _expected_dates(args.days)
    history_runs = load_history_runs(
        args.history_dir,
        workflows=args.workflow,
        expected_dates=expected_dates,
    )
    if not token and not history_runs:
        logger.error("GitHub token required via --token or GITHUB_TOKEN env var.")
        return 1

    all_runs: list[dict[str, Any]] = []
    if token:
        gh = Github(auth=Auth.Token(token))
        logger.info(
            "Fetching %d days of runs for %s (workflows: %s, branch: %s)...",
            args.days, args.repo, ", ".join(args.workflow), args.branch,
        )
        for wf in args.workflow:
            wf_runs = fetch_daily_runs(gh, args.repo, wf, args.branch, args.days)
            for run in wf_runs:
                run["workflow"] = wf
            logger.info("Collected %d live runs from %s.", len(wf_runs), wf)
            all_runs.extend(wf_runs)
    if history_runs:
        logger.info(
            "Loaded %d stored run snapshot(s) from %s.",
            len(history_runs),
            args.history_dir,
        )
    all_runs = merge_runs(all_runs, history_runs)

    data = build_report_data(
        all_runs,
        repo_full_name=args.repo,
        workflow_file=", ".join(args.workflow),
        branch=args.branch,
        expected_dates=expected_dates,
    )

    html = render_html(data)
    Path(args.output).write_text(html, encoding="utf-8")
    logger.info("Wrote HTML report to %s", args.output)

    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )
        logger.info("Wrote JSON data to %s", args.output_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Static operator dashboard site for the Valkey CI agent.

The dashboard JSON already captures the right operational state. This module
turns that payload into a tighter multi-page console: fewer pages, clearer
signal hierarchy, explicit data-coverage reporting, and direct links back to
GitHub wherever the data model gives us enough context to build them.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
from pathlib import Path
from typing import Any


JsonObject = dict[str, Any]

_VISIBLE_PAGES: list[tuple[str, str, str]] = [
    ("index.html", "Daily CI", "Runs and failures"),
    ("review.html", "PRs", "Review state"),
    ("fuzzer.html", "Fuzzer", "Anomalies"),
]

_ALIAS_PAGES: dict[str, tuple[str, str]] = {
    "daily.html": ("index.html", "Daily CI is now the homepage."),
    "flaky.html": ("index.html#campaigns", "Flaky campaigns moved into the Daily page."),
    "acceptance.html": ("review.html#replay", "Replay proof moved into the PRs page."),
    "ops.html": ("diagnostics.html", "Diagnostics moved out of the main navigation."),
    "ai.html": ("diagnostics.html#ai-reliability", "AI reliability moved into Diagnostics."),
}

_VALKEY_LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 187.9 63.5" role="img" aria-labelledby="valkey-logo-title">
<title id="valkey-logo-title">Valkey</title>
<style>
.word{fill:#1a2026}
.mark{fill:#6983ff;fill-rule:evenodd}
</style>
<path class="mark" d="M15.2 50 5.8 44.1v-25L28.8 6l22.3 13.1v26.3L28.4 58.2l-7.9-4.9v-12l-4.3-2.7V25l12.4-7.1 12.1 7.1v14.2l-9.6 5.4v-5.7c2.9-1.1 4.9-3.9 4.9-7.3s-3.4-7.8-7.6-7.8-7.6 3.5-7.6 7.8 2.1 6.2 4.9 7.3v10.9l2.7 1.7 16.8-9.5V24.3l-16.6-9.8-17.1 9.8v18.5l3.6 2.3Zm13.3-21.9c1.9 0 3.4 1.6 3.4 3.6s-1.5 3.6-3.4 3.6-3.4-1.6-3.4-3.6 1.5-3.6 3.4-3.6Z"/>
<path class="word" d="m85.2 11.4-12.1 33.8h-4L57 11.4h4.1L69 33.7c.3.9.6 1.8.9 2.6.3.8.5 1.6.7 2.4.2.8.4 1.5.5 2.2.2-.7.3-1.4.5-2.2.2-.8.4-1.6.7-2.4.3-.8.6-1.7.9-2.6l7.9-22.2h4.2Z"/>
<path class="word" d="M94 19.5c3 0 5.3.7 6.7 2 1.5 1.4 2.2 3.5 2.2 6.5v17.2h-2.8l-.7-3.7h-.2c-.7.9-1.4 1.7-2.2 2.3-.8.6-1.7 1.1-2.7 1.4-1 .3-2.2.5-3.7.5s-2.9-.3-4.1-.8c-1.2-.5-2.1-1.4-2.8-2.5-.7-1.1-1-2.5-1-4.2 0-2.5 1-4.5 3-5.8 2-1.4 5.1-2.1 9.2-2.2l4.4-.2v-1.5c0-2.2-.5-3.7-1.4-4.6-.9-.9-2.3-1.3-4-1.3-1.3 0-2.6.2-3.8.6-1.2.4-2.3.8-3.4 1.4l-1.2-2.9c1.1-.6 2.5-1.1 3.9-1.5 1.5-.4 3-.6 4.7-.6Zm5.1 13.3-3.9.2c-3.2.1-5.4.6-6.7 1.5s-1.9 2.2-1.9 3.9.4 2.5 1.3 3.2c.9.7 2 1 3.5 1 2.3 0 4.1-.6 5.5-1.9s2.2-3.1 2.2-5.6v-2.3Z"/>
<path class="word" d="M112.1 45.3h-3.9v-36h3.9v36Z"/>
<path class="word" d="M121.3 9.3V28c0 .6 0 1.4 0 2.3 0 .9 0 1.7-.1 2.3h.2c.3-.4.8-1 1.4-1.8.6-.8 1.2-1.4 1.6-1.9l8.4-9h4.5l-10.2 10.8 10.9 14.5h-4.6l-9-12-3.1 2.8v9.2h-3.8V9.3h3.8Z"/>
<path class="word" d="M148.2 19.4c2.2 0 4 .5 5.6 1.4 1.5 1 2.7 2.3 3.5 4 .8 1.7 1.2 3.7 1.2 6v2.4H141c0 3 .8 5.2 2.2 6.8s3.5 2.3 6.1 2.3 3-.1 4.3-.4c1.2-.3 2.5-.7 3.8-1.3v3.4c-1.3.6-2.6 1-3.8 1.2-1.2.3-2.7.4-4.4.4-2.4 0-4.6-.5-6.4-1.5-1.8-1-3.2-2.5-4.2-4.4-1-1.9-1.5-4.3-1.5-7.1 0-2.7.5-5.1 1.4-7.1.9-2 2.2-3.5 3.9-4.6 1.7-1.1 3.7-1.6 5.9-1.6Zm0 3.2c-2.1 0-3.7.7-4.9 2-1.2 1.3-1.9 3.2-2.2 5.6h13.4c0-1.5-.3-2.8-.7-4-.4-1.2-1.1-2.1-2.1-2.7-.9-.6-2.1-1-3.6-1Z"/>
<path class="word" d="M158.4 19.9h4.1l5.6 14.7c.3.9.6 1.7.9 2.5.3.8.5 1.5.7 2.3.2.7.4 1.4.5 2.1h.2c.2-.8.5-1.8.9-3 .4-1.3.8-2.6 1.3-3.9l5.3-14.7h4.1l-11 29.1c-.6 1.6-1.3 2.9-2.1 4.1-.8 1.2-1.7 2-2.8 2.7-1.1.6-2.5.9-4 .9s-1.4 0-1.9-.1c-.6 0-1-.2-1.4-.3v-3.1c.3 0 .7.1 1.2.2.5 0 1 0 1.5 0 1 0 1.8-.2 2.5-.6.7-.4 1.3-.9 1.8-1.6.5-.7.9-1.5 1.3-2.5l1.4-3.6-10.2-25.4Z"/>
</svg>
"""


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


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class _Html(str):
    """Marker for trusted HTML assembled in this module."""


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
        number = float(value)
    except (TypeError, ValueError):
        return _html(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.1f}"


def _format_percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return _html(value)


def _format_rate(numerator: Any, denominator: Any) -> str:
    den = _int(denominator)
    if den <= 0:
        return "n/a"
    num = _int(numerator)
    return f"{num}/{den} ({(num / den) * 100:.0f}%)"


def _short_sha(value: object) -> str:
    text = _str(value)
    return text[:7] if text else ""


def _tone_for_status(label: str) -> str:
    normalized = label.lower()
    if any(word in normalized for word in ("success", "pass", "ready", "merged", "normal", "available", "covered")):
        return "good"
    if any(word in normalized for word in ("fail", "error", "dead", "abandoned", "anomalous", "missing", "critical", "blocked", "degraded")):
        return "bad"
    if any(word in normalized for word in ("warning", "queued", "retry", "incomplete", "needs", "pending", "processing", "partial", "sparse")):
        return "warn"
    return "info"


def _chip(value: object, *, tone: str | None = None) -> _Html:
    label = _str(value, "unknown") or "unknown"
    resolved_tone = tone or _tone_for_status(label)
    return _safe_html(
        f'<span class="chip chip-{_html_attr(resolved_tone)}">{_html(label)}</span>'
    )


def _link(label: object, url: object, *, compact: bool = False) -> _Html:
    url_text = _str(url)
    if not url_text:
        return _safe_html(_html(label))
    classes = "link link-compact" if compact else "link"
    return _safe_html(
        f'<a class="{classes}" href="{_html_attr(url_text)}">{_html(label)}</a>'
    )


def _link_external(label: object, url: object) -> _Html:
    url_text = _str(url)
    if not url_text:
        return _safe_html(_html(label))
    return _safe_html(
        f'<a class="link" href="{_html_attr(url_text)}" target="_blank" rel="noreferrer">{_html(label)}</a>'
    )


def _table(
    headers: list[str],
    rows: list[list[object]],
    *,
    empty: str,
    row_attrs: list[str] | None = None,
) -> str:
    if not rows:
        return f'<p class="empty">{_html(empty)}</p>'
    attrs = row_attrs or []
    head = "".join(f"<th>{_html(header)}</th>" for header in headers)
    rendered_rows: list[str] = []
    for index, row in enumerate(rows):
        attr = f" {attrs[index]}" if index < len(attrs) and attrs[index] else ""
        rendered_rows.append(
            "<tr"
            + attr
            + ">"
            + "".join(f"<td>{_html_cell(value)}</td>" for value in row)
            + "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(rendered_rows)
        + "</tbody></table></div>"
    )


def _panel(
    title: str,
    body: str,
    *,
    subtitle: str = "",
    wide: bool = False,
    anchor: str = "",
) -> str:
    classes = "panel panel-wide" if wide else "panel"
    anchor_attr = f' id="{_html_attr(anchor)}"' if anchor else ""
    subtitle_html = (
        f'<p class="panel-subtitle">{_html(subtitle)}</p>' if subtitle else ""
    )
    return (
        f'<section class="{classes}"{anchor_attr}>'
        f'<div class="panel-head"><h2>{_html(title)}</h2>{subtitle_html}</div>'
        f"{body}</section>"
    )


def _metric(label: str, value: object, *, note: str = "", tone: str = "accent") -> str:
    note_html = f"<span>{_html(note)}</span>" if note else ""
    return (
        f'<article class="metric metric-{_html_attr(tone)}">'
        f"<p>{_html(label)}</p>"
        f"<strong>{_html_cell(value)}</strong>"
        f"{note_html}"
        "</article>"
    )


def _stat_grid(rows: list[tuple[str, object]]) -> str:
    return (
        '<div class="summary-grid">'
        + "".join(
            f'<div><span>{_html(label)}</span><strong>{_html_cell(value)}</strong></div>'
            for label, value in rows
        )
        + "</div>"
    )


def _page_card(
    title: str,
    href: str,
    body: str,
    stats: list[tuple[str, object]],
) -> str:
    stats_html = "".join(
        f'<li><span>{_html(label)}</span><strong>{_html_cell(value)}</strong></li>'
        for label, value in stats
    )
    return (
        '<a class="page-card" href="'
        + _html_attr(href)
        + '"><div class="page-card-head"><h3>'
        + _html(title)
        + "</h3><span>Open</span></div><p>"
        + _html(body)
        + '</p><ul class="mini-stats">'
        + stats_html
        + "</ul></a>"
    )


def _meta_pill(label: str, value: object) -> _Html:
    return _safe_html(
        '<span class="meta-pill">'
        f"<strong>{_html(label)}</strong>"
        f"<span>{_html_cell(value)}</span>"
        "</span>"
    )


def _sparkline_svg(
    values: list[float],
    *,
    color: str,
    width: int = 220,
    height: int = 56,
) -> _Html:
    if not values:
        return _safe_html('<p class="empty">Not enough history.</p>')
    if len(values) == 1:
        values = [values[0], values[0]]
    minimum = min(values)
    maximum = max(values)
    spread = max(maximum - minimum, 0.0001)
    step = width / max(len(values) - 1, 1)
    points: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        x = round(index * step, 2)
        y = round(height - (((value - minimum) / spread) * (height - 12)) - 6, 2)
        points.append((x, y))
    point_text = " ".join(f"{x},{y}" for x, y in points)
    area = f"0,{height} " + point_text + f" {width},{height}"
    circles = "".join(
        f'<circle cx="{x}" cy="{y}" r="2.5" fill="{_html_attr(color)}"></circle>'
        for x, y in points
    )
    return _safe_html(
        '<svg class="sparkline" viewBox="0 0 '
        + f'{width} {height}" preserveAspectRatio="none" aria-hidden="true">'
        f'<polygon points="{area}" fill="{_html_attr(color)}" opacity="0.14"></polygon>'
        f'<polyline points="{point_text}" fill="none" stroke="{_html_attr(color)}" '
        'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></polyline>'
        f"{circles}</svg>"
    )


def _top_repo_label(dashboard: JsonObject) -> str:
    daily_health = _mapping(dashboard.get("daily_health"))
    if daily_health.get("repo"):
        return _str(daily_health.get("repo"))
    pr_reviews = _mapping(dashboard.get("pr_reviews"))
    recent_reviews = _list(pr_reviews.get("recent_reviews"))
    for review in recent_reviews:
        if isinstance(review, dict) and review.get("repo"):
            return _str(review.get("repo"))
    flaky_tests = _mapping(dashboard.get("flaky_tests"))
    recent_campaigns = _list(flaky_tests.get("recent_campaigns"))
    for campaign in recent_campaigns:
        if isinstance(campaign, dict) and campaign.get("repo_full_name"):
            return _str(campaign.get("repo_full_name"))
    state_health = _mapping(dashboard.get("state_health"))
    recent_watermarks = _list(state_health.get("recent_watermarks"))
    if recent_watermarks:
        return _str(_mapping(recent_watermarks[0]).get("target_repo"), "valkey-io/valkey")
    return "valkey-io/valkey"


def _repo_url(repo: object) -> str:
    repo_text = _str(repo)
    if not repo_text or "/" not in repo_text:
        return ""
    return f"https://github.com/{repo_text}"


def _commit_url(repo: object, sha: object) -> str:
    repo_text = _str(repo)
    sha_text = _str(sha)
    if not repo_text or not sha_text:
        return ""
    return f"https://github.com/{repo_text}/commit/{sha_text}"


def _pull_url(repo: object, pr_number: object) -> str:
    repo_text = _str(repo)
    number = _str(pr_number)
    if not repo_text or not number:
        return ""
    return f"https://github.com/{repo_text}/pull/{number}"


def _issue_comment_url(review: JsonObject) -> str:
    repo = _str(review.get("repo"))
    pr_number = _str(review.get("pr_number"))
    comment_id = _str(review.get("summary_comment_id"))
    if not repo or not pr_number or not comment_id:
        return ""
    return f"https://github.com/{repo}/pull/{pr_number}#issuecomment-{comment_id}"


def _review_comment_url(review: JsonObject) -> str:
    repo = _str(review.get("repo"))
    pr_number = _str(review.get("pr_number"))
    comment_ids = _list(review.get("review_comment_ids"))
    if not repo or not pr_number or not comment_ids:
        return ""
    return f"https://github.com/{repo}/pull/{pr_number}"


def _run_url(repo: object, run_id: object) -> str:
    repo_text = _str(repo)
    run_text = _str(run_id)
    if not repo_text or not run_text:
        return ""
    return f"https://github.com/{repo_text}/actions/runs/{run_text}"


def _truncate(value: object, *, limit: int = 96) -> str:
    text = _str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _workflow_label(value: object) -> str:
    text = _str(value)
    if text.endswith((".yml", ".yaml")):
        text = text.rsplit(".", 1)[0]
    text = text.replace("-", " ").replace("_", " ").strip()
    return text.title() or "Unknown"


def _workflow_slug(value: object) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _str(value).lower()).strip("-")
    return slug or "workflow"


def _workflow_reports(daily_health: JsonObject) -> list[JsonObject]:
    reports = [
        _mapping(report)
        for report in _list(daily_health.get("workflow_reports"))
        if isinstance(report, dict) and _list(_mapping(report).get("heatmap"))
    ]
    if reports:
        return reports
    if _list(daily_health.get("heatmap")):
        return [daily_health]
    return []


def _event_subject_url(event: JsonObject, repo_fallback: str) -> str:
    attributes = _mapping(event.get("attributes"))
    for key in ("pr_url", "issue_url", "run_url", "proof_url", "url"):
        url = _str(attributes.get(key))
        if url:
            return url
    subject = _str(event.get("subject"))
    if subject.startswith("http://") or subject.startswith("https://"):
        return subject
    pr_match = re.fullmatch(r"([^#\s]+/[^#\s]+)#(\d+)", subject)
    if pr_match:
        return _pull_url(pr_match.group(1), pr_match.group(2))
    run_match = re.fullmatch(r"([^:\s]+/[^:\s]+):[^:]+:(\d+)", subject)
    if run_match:
        return _run_url(run_match.group(1), run_match.group(2))
    commit_match = re.fullmatch(r"([^@\s]+/[^@\s]+)@([0-9a-fA-F]{7,40})", subject)
    if commit_match:
        return _commit_url(commit_match.group(1), commit_match.group(2))
    if re.fullmatch(r"#(\d+)", subject):
        return _pull_url(repo_fallback, subject[1:])
    return ""


def _event_subject_cell(event: JsonObject, repo_fallback: str) -> _Html:
    subject = _str(event.get("subject"))
    return _link_external(subject, _event_subject_url(event, repo_fallback))


def _event_attributes_summary(attributes: JsonObject) -> str:
    if not attributes:
        return "n/a"
    parts: list[str] = []
    for key, value in sorted(attributes.items()):
        if key.endswith("_url"):
            continue
        if isinstance(value, (str, int, float, bool)):
            parts.append(f"{key}={value}")
        elif isinstance(value, list):
            rendered = ", ".join(_str(item) for item in value[:3])
            if len(value) > 3:
                rendered += ", …"
            parts.append(f"{key}=[{rendered}]")
        elif isinstance(value, dict):
            parts.append(f"{key}=…")
    if not parts:
        return "link-only"
    return _truncate(", ".join(parts), limit=140)


def _site_nav(current_page: str) -> str:
    links: list[str] = []
    for href, title, description in _VISIBLE_PAGES:
        current = ' aria-current="page"' if href == current_page else ""
        classes = "nav-link nav-link-current" if href == current_page else "nav-link"
        links.append(
            f'<a class="{classes}" href="{_html_attr(href)}"{current}>'
            f'<strong>{_html(title)}</strong><span>{_html(description)}</span></a>'
        )
    return "".join(links)


def _input_warnings(dashboard: JsonObject) -> list[str]:
    state_health = _mapping(dashboard.get("state_health"))
    return [_str(item) for item in _list(state_health.get("input_warnings")) if _str(item)]


def _coverage_items(dashboard: JsonObject) -> list[dict[str, str]]:
    daily_health = _mapping(dashboard.get("daily_health"))
    flaky_tests = _mapping(dashboard.get("flaky_tests"))
    pr_reviews = _mapping(dashboard.get("pr_reviews"))
    acceptance = _mapping(dashboard.get("acceptance"))
    fuzzer = _mapping(dashboard.get("fuzzer"))
    ai = _mapping(dashboard.get("ai_reliability"))
    agent_outcomes = _mapping(dashboard.get("agent_outcomes"))
    state_health = _mapping(dashboard.get("state_health"))
    warnings = _input_warnings(dashboard)

    def resolve(
        *,
        label: str,
        href: str,
        present: bool,
        partial: bool,
        detail: str,
    ) -> dict[str, str]:
        if present and not partial:
            status = "available"
            tone = "good"
        elif present and partial:
            status = "partial"
            tone = "warn"
        else:
            status = "missing"
            tone = "bad"
        return {
            "label": label,
            "href": href,
            "status": status,
            "tone": tone,
            "detail": detail,
        }

    items = [
        resolve(
            label="Daily health",
            href="daily.html",
            present=bool(daily_health) and bool(_list(daily_health.get("runs")) or _list(daily_health.get("dates"))),
            partial=bool(daily_health) and not bool(_list(daily_health.get("heatmap"))),
            detail=(
                f"{_format_number(daily_health.get('total_runs', 0))} runs, "
                f"{_format_number(daily_health.get('failed_runs', 0))} failed"
                if daily_health
                else "No Daily artifact supplied."
            ),
        ),
        resolve(
            label="Flaky campaigns",
            href="daily.html#campaigns",
            present=bool(flaky_tests) and bool(
                _list(flaky_tests.get("recent_campaigns")) or _mapping(flaky_tests.get("status_counts"))
            ),
            partial=False,
            detail=(
                f"{_format_number(flaky_tests.get('active_campaigns', 0))} active, "
                f"{_format_number(flaky_tests.get('campaigns', 0))} total"
                if flaky_tests
                else "Failure-store campaign data missing."
            ),
        ),
        resolve(
            label="PR review state",
            href="review.html",
            present=bool(pr_reviews) and bool(
                pr_reviews.get("tracked_prs", 0) or _list(pr_reviews.get("recent_reviews"))
            ),
            partial=bool(pr_reviews) and not bool(pr_reviews.get("review_comments", 0)),
            detail=(
                f"{_format_number(pr_reviews.get('tracked_prs', 0))} PRs, "
                f"{_format_number(pr_reviews.get('review_comments', 0))} comments"
                if pr_reviews
                else "No review-state snapshot supplied."
            ),
        ),
        resolve(
            label="Replay acceptance",
            href="review.html#replay",
            present=bool(acceptance) and bool(
                acceptance.get("payloads_seen", 0)
                or _list(acceptance.get("recent_review_results"))
                or _list(acceptance.get("recent_workflow_results"))
            ),
            partial=bool(acceptance) and not bool(_list(acceptance.get("recent_review_results"))),
            detail=(
                f"{_format_number(acceptance.get('review_cases', 0))} review cases, "
                f"{_format_number(acceptance.get('workflow_cases', 0))} workflow cases"
                if acceptance
                else "No acceptance payload supplied."
            ),
        ),
        resolve(
            label="Fuzzer analysis",
            href="fuzzer.html",
            present=bool(fuzzer) and bool(fuzzer.get("runs_seen", 0) or fuzzer.get("result_files", 0)),
            partial=bool(fuzzer) and _int(fuzzer.get("runs_analyzed")) < _int(fuzzer.get("runs_seen")),
            detail=(
                f"{_format_number(fuzzer.get('runs_analyzed', 0))}/"
                f"{_format_number(fuzzer.get('runs_seen', 0))} runs analyzed"
                if fuzzer
                else "No fuzzer-monitor payload supplied."
            ),
        ),
        resolve(
            label="Event ledger",
            href="ops.html#event-stream",
            present=bool(agent_outcomes) and bool(agent_outcomes.get("events", 0)),
            partial=False,
            detail=(
                f"{_format_number(agent_outcomes.get('events', 0))} events recorded"
                if agent_outcomes
                else "No event log supplied."
            ),
        ),
        resolve(
            label="Monitor state",
            href="ops.html#watermarks",
            present=bool(state_health) and bool(
                state_health.get("monitor_watermarks", 0) or warnings
            ),
            partial=bool(warnings),
            detail=(
                f"{_format_number(state_health.get('monitor_watermarks', 0))} watermarks"
                + (f", {_format_number(len(warnings))} warnings" if warnings else "")
                if state_health or warnings
                else "No monitor-state snapshot supplied."
            ),
        ),
        resolve(
            label="AI reliability",
            href="ops.html#ai-reliability",
            present=bool(ai) and bool(_mapping(ai.get("ai_metrics")) or ai.get("token_usage", 0)),
            partial=bool(ai) and not bool(ai.get("prompt_safety_checked", 0)),
            detail=(
                f"{_format_number(ai.get('schema_calls', 0))} schema calls, "
                f"{_format_percent(ai.get('prompt_safety_coverage', 0.0))} safety coverage"
                if ai
                else "No rate-state AI metrics supplied."
            ),
        ),
    ]
    return items


def _coverage_table(dashboard: JsonObject) -> str:
    items = _coverage_items(dashboard)
    rows: list[list[object]] = []
    attrs: list[str] = []
    for item in items:
        rows.append(
            [
                item["label"],
                _chip(item["status"], tone=item["tone"]),
                item["detail"],
                _link(item["href"].split("#", 1)[0].replace(".html", ""), item["href"], compact=True),
            ]
        )
        attrs.append(f'class="row-tone-{_html_attr(item["tone"])}"')
    return _table(
        ["Source", "Status", "Detail", "Page"],
        rows,
        empty="No source-coverage details available.",
        row_attrs=attrs,
    )


def _missing_count(dashboard: JsonObject) -> int:
    return sum(1 for item in _coverage_items(dashboard) if item["status"] == "missing")


def _render_trends(dashboard: JsonObject) -> str:
    trends = _mapping(dashboard.get("trends"))
    failure_rate = _mapping(trends.get("failure_rate"))
    review_health = _mapping(trends.get("review_health"))
    flaky_subsystems = _mapping(trends.get("flaky_subsystems"))
    top_subsystems = [
        _str(item)
        for item in _list(flaky_subsystems.get("top_subsystems"))
        if _str(item)
    ]
    subsystem_series = _mapping(flaky_subsystems.get("series"))
    subsystem_rows = "".join(
        '<li><span>'
        + _html(name)
        + "</span><strong>"
        + _format_number(sum(_int(value) for value in _list(subsystem_series.get(name))))
        + "</strong></li>"
        for name in top_subsystems
    ) or '<li><span>No subsystem trend yet</span><strong>0</strong></li>'
    return _panel(
        "Trend watch",
        '<div class="trend-grid">'
        '<article class="trend-block"><h3>Daily failure rate</h3>'
        + str(
            _sparkline_svg(
                [_float(value) for value in _list(failure_rate.get("rates"))],
                color="#89a0ff",
            )
        )
        + '<p class="trend-note">'
        + _html(
            f"{_format_percent(failure_rate.get('average_rate', 0.0))} average over "
            f"{_format_number(trends.get('window_days', 0))} days"
        )
        + "</p></article>"
        '<article class="trend-block"><h3>Review degradation</h3>'
        + str(
            _sparkline_svg(
                [_float(value) for value in _list(review_health.get("degraded_reviews"))],
                color="#f75f63",
            )
        )
        + '<p class="trend-note">'
        + _html(
            f"{_format_percent(review_health.get('average_score', 0.0))} healthy-review score"
        )
        + "</p></article>"
        '<article class="trend-block"><h3>Flaky subsystem pressure</h3><ul class="trend-list">'
        + subsystem_rows
        + "</ul></article></div>",
        wide=True,
    )


def _layout(
    dashboard: JsonObject,
    *,
    current_page: str,
    page_title: str,
    eyebrow: str,
    intro: str,
    body: str,
    header_metrics: list[str],
) -> str:
    repo_label = _top_repo_label(dashboard)
    generated_at = _str(dashboard.get("generated_at"), "unknown")
    acceptance = _mapping(dashboard.get("acceptance"))
    readiness = acceptance.get("readiness", "unknown")
    snapshot = _mapping(dashboard.get("snapshot"))
    repo_link = _repo_url(repo_label)
    meta = "".join(
        [
            str(_meta_pill("Repo", _link_external(repo_label, repo_link) if repo_link else repo_label)),
            str(_meta_pill("Generated", generated_at)),
            str(_meta_pill("Readiness", _chip(readiness))),
            str(_meta_pill("Raw JSON", _link("dashboard.json", "data/dashboard.json"))),
        ]
    )
    posture = (
        f"{_format_number(snapshot.get('failure_incidents', 0))} incidents, "
        f"{_format_number(snapshot.get('active_flaky_campaigns', 0))} active campaigns, "
        f"{_format_number(snapshot.get('tracked_review_prs', 0))} tracked PRs."
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html(page_title)} · Valkey CI Agent Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fira+Mono:wght@400;500&family=Open+Sans:wght@400;600;700;800&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="assets/site.css">
</head>
<body>
  <div class="site-shell">
    <aside class="sidebar">
      <section class="brand">
        <div class="brand-logo"><img src="assets/valkey-horizontal.svg" alt="Valkey logo"></div>
        <div class="brand-copy">
          <p>Valkey CI</p>
          <h1>Operator Console</h1>
          <span>{_html(repo_label)}</span>
        </div>
      </section>
      <nav class="nav">{_site_nav(current_page)}</nav>
      <section class="sidebar-card">
        <p>Status</p>
        <strong>{_html_cell(_chip(readiness))}</strong>
        <span>{_html(posture)}</span>
      </section>
    </aside>
    <main class="page">
      <header class="hero">
        <div class="hero-copy">
          <div class="eyebrow-row">
            <div class="eyebrow">{_html(eyebrow)}</div>
            <div class="hero-meta">{meta}</div>
          </div>
          <h2>{_html(page_title)}</h2>
          <p>{_html(intro)}</p>
        </div>
      </header>
      <section class="hero-metrics">{''.join(header_metrics)}</section>
      {body}
    </main>
  </div>
  <script src="assets/site.js"></script>
</body>
</html>"""


def _overview_metrics(dashboard: JsonObject) -> list[str]:
    snapshot = _mapping(dashboard.get("snapshot"))
    daily_health = _mapping(dashboard.get("daily_health"))
    return [
        _metric(
            "Daily failed runs",
            daily_health.get("failed_runs", 0),
            note="Latest Daily window",
            tone="bad",
        ),
        _metric(
            "Active campaigns",
            snapshot.get("active_flaky_campaigns", 0),
            note="Open remediation loops",
            tone="accent",
        ),
        _metric(
            "Tracked PRs",
            snapshot.get("tracked_review_prs", 0),
            note="With durable review state",
            tone="accent",
        ),
        _metric(
            "Fuzzer anomalies",
            snapshot.get("fuzzer_anomalous_runs", 0),
            note="Non-normal analyzed runs",
            tone="warn",
        ),
        _metric(
            "Missing data",
            _missing_count(dashboard),
            note="Artifacts or state still absent",
            tone="bad" if _missing_count(dashboard) else "good",
        ),
    ]


def _render_overview(dashboard: JsonObject) -> str:
    snapshot = _mapping(dashboard.get("snapshot"))
    acceptance = _mapping(dashboard.get("acceptance"))
    pr_reviews = _mapping(dashboard.get("pr_reviews"))
    fuzzer = _mapping(dashboard.get("fuzzer"))
    daily_health = _mapping(dashboard.get("daily_health"))
    repo_fallback = _top_repo_label(dashboard)
    recent_events = [
        _mapping(event)
        for event in _list(_mapping(dashboard.get("agent_outcomes")).get("recent_events"))
        if isinstance(event, dict)
    ]
    event_rows = [
        [
            event.get("created_at", ""),
            _chip(event.get("event_type", "")),
            _event_subject_cell(event, repo_fallback),
            _event_attributes_summary(_mapping(event.get("attributes"))),
        ]
        for event in recent_events[:10]
    ]

    page_cards = [
        _page_card(
            "Daily CI",
            "daily.html",
            "Daily failures, red heatmap intensity, recent runs, and active remediation campaigns.",
            [
                ("Runs", daily_health.get("total_runs", 0)),
                ("Failures", daily_health.get("failed_runs", 0)),
            ],
        ),
        _page_card(
            "PRs",
            "review.html",
            "Tracked pull requests, replay cases, workflow contracts, and review coverage gaps.",
            [
                ("Tracked", pr_reviews.get("tracked_prs", 0)),
                ("Replay", acceptance.get("review_cases", 0)),
            ],
        ),
        _page_card(
            "Fuzzer",
            "fuzzer.html",
            "Recent anomalies with scenario, seed, issue action, and root-cause classification.",
            [
                ("Analyzed", snapshot.get("fuzzer_runs_analyzed", 0)),
                ("Anomalous", fuzzer.get("status_counts", {}).get("anomalous", 0)),
            ],
        ),
        _page_card(
            "Ops",
            "ops.html",
            "Incident queue, event stream, watermarks, AI reliability counters, and data coverage.",
            [
                ("Events", snapshot.get("agent_events", 0)),
                ("Warnings", len(_input_warnings(dashboard))),
            ],
        ),
    ]

    body = (
        '<section class="page-grid page-grid-wide">'
        + _panel(
            "Signal map",
            '<div class="card-grid">' + "".join(page_cards) + "</div>",
            subtitle="The operator view is now centered on four durable workflows instead of a large set of loosely related pages.",
            wide=True,
        )
        + _render_trends(dashboard)
        + _panel(
            "Data coverage",
            _coverage_table(dashboard),
            subtitle="Missing artifacts are surfaced explicitly so empty panels do not look healthy by accident.",
            wide=True,
        )
        + _panel(
            "Recent event stream",
            _table(
                ["Time", "Event", "Subject", "Detail"],
                event_rows,
                empty="No recent event-ledger entries were available.",
            ),
            subtitle="Recent PRs, proof events, and review activity from the append-only ledger.",
            wide=True,
            anchor="event-stream",
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="index.html",
        page_title="Overview",
        eyebrow="Control Room",
        intro="Daily failures, review state, fuzzer anomalies, and source coverage in one console.",
        body=body,
        header_metrics=_overview_metrics(dashboard),
    )


def _daily_metrics(dashboard: JsonObject) -> list[str]:
    daily_health = _mapping(dashboard.get("daily_health"))
    flaky_tests = _mapping(dashboard.get("flaky_tests"))
    total_runs = _int(daily_health.get("total_runs"))
    failed_runs = _int(daily_health.get("failed_runs"))
    total_days = len(_list(daily_health.get("dates")))
    days_with_runs = _int(daily_health.get("days_with_runs"))
    reports = _workflow_reports(daily_health)
    metrics = [
        _metric("Tracked days", total_days, note=f"{days_with_runs}/{total_days or 0} with run data"),
        _metric("Total runs", total_runs, note="Latest monitored samples"),
        _metric("Failed runs", failed_runs, note=_format_rate(failed_runs, total_runs), tone="bad"),
    ]
    if len(reports) > 1:
        for report in reports:
            wf_label = _workflow_label(report.get("workflow"))
            wf_total = _int(report.get("total_runs"))
            wf_failed = _int(report.get("failed_runs"))
            tone = "bad" if wf_failed else "good"
            metrics.append(
                _metric(
                    f"{wf_label} failures",
                    wf_failed,
                    note=_format_rate(wf_failed, wf_total),
                    tone=tone,
                )
            )
    else:
        metrics.append(
            _metric("Unique failures", daily_health.get("unique_failures", 0), note="Across current window", tone="warn"),
        )
    metrics.append(
        _metric("Active campaigns", flaky_tests.get("active_campaigns", 0), note="Validation loops in progress"),
    )
    return metrics


def _daily_heatmap_table(report: JsonObject, *, filter_id: str) -> str:
    heatmap_rows = [
        _mapping(row)
        for row in _list(report.get("heatmap"))
        if isinstance(row, dict)
    ]
    dates = [_str(date) for date in _list(report.get("dates"))]
    if not heatmap_rows or not dates:
        return '<p class="empty">No Daily heatmap is available in the supplied payload.</p>'
    max_count = max(
        (
            _int(_mapping(cell).get("count"))
            for row in heatmap_rows
            for cell in _list(row.get("cells"))
            if isinstance(cell, dict)
        ),
        default=1,
    )
    head = "".join(f"<th>{_html(date[-2:])}</th>" for date in dates)
    body_rows: list[str] = []
    for row in heatmap_rows[:28]:
        days_failed = _int(row.get("days_failed"))
        total_days = max(_int(row.get("total_days")), 1)
        daily_badge = (
            str(_chip("daily", tone="bad")) if days_failed >= total_days and total_days else ""
        )
        name_cell = _safe_html(
            '<div class="heat-row-name"><span>'
            + _html(_str(row.get("name")))
            + "</span>"
            + daily_badge
            + "</div>"
        )
        cells: list[str] = []
        for cell in _list(row.get("cells")):
            data = _mapping(cell)
            count = _int(data.get("count"))
            has_run = bool(data.get("has_run", True))
            alpha = 0.20 + (count / max_count) * 0.80 if count else 0.0
            if not has_run:
                text = "—"
                style = ""
                classes = "heat-cell heat-cell-missing"
                title = f"{_html_attr(data.get('date'))}: no run data"
            else:
                text = str(count) if count else ""
                style = f' style="--heat-alpha:{alpha:.2f}"' if count else ""
                classes = "heat-cell heat-cell-hit" if count else "heat-cell"
                title = f"{_html_attr(data.get('date'))}: {count}"
            cells.append(
                f'<td class="{classes}"{style} title="{title}">'
                f"{_html(text)}</td>"
            )
        body_rows.append(
            '<tr data-filter-item="'
            + _html_attr(_str(row.get("name")))
            + '"><th class="sticky-col">'
            + _html_cell(name_cell)
            + '</th><td class="sticky-col secondary-col">'
            + _html(f"{days_failed}/{total_days}d")
            + "</td>"
            + "".join(cells)
            + "</tr>"
        )
    return (
        '<div class="toolbar"><label class="search"><span>Filter failures</span>'
        '<input type="search" placeholder="jemalloc, cluster, replication..." '
        f'data-filter-target="{_html_attr(filter_id)}"></label></div>'
        f'<div class="heatmap-wrap" id="{_html_attr(filter_id)}"><table class="heatmap-table"><thead><tr>'
        '<th class="sticky-col">Failure</th><th class="sticky-col secondary-col">Freq</th>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
    )


def _heatmap_missing_count(report: JsonObject) -> int:
    """Count dates with no run data in a heatmap report."""
    missing = _list(report.get("missing_dates"))
    if missing:
        return len(missing)
    dates = _list(report.get("dates"))
    days_with_runs = _int(report.get("days_with_runs"))
    if dates and days_with_runs:
        return len(dates) - days_with_runs
    # Fall back to scanning cells for has_run=false
    seen_missing: set[str] = set()
    for row in _list(report.get("heatmap")):
        for cell in _list(_mapping(row).get("cells")):
            data = _mapping(cell)
            if not data.get("has_run", True):
                seen_missing.add(_str(data.get("date")))
    return len(seen_missing)


def _daily_heatmap(daily_health: JsonObject) -> str:
    reports = _workflow_reports(daily_health)
    if not reports:
        return '<p class="empty">No Daily heatmap is available in the supplied payload.</p>'
    if len(reports) == 1:
        missing = _heatmap_missing_count(reports[0])
        missing_note = (
            f" <strong>{missing}</strong> of {len(_list(reports[0].get('dates')))} dates had no run data."
            if missing
            else ""
        )
        return (
            f'<p class="split-note">A dash means no run data was available for that date.{missing_note}</p>'
            + _daily_heatmap_table(reports[0], filter_id="daily-heatmap")
        )

    blocks: list[str] = []
    for report in reports:
        workflow = _workflow_label(report.get("workflow"))
        slug = _workflow_slug(report.get("workflow"))
        missing = _heatmap_missing_count(report)
        missing_pill = (
            _meta_pill("Missing days", _format_number(missing))
            if missing
            else ""
        )
        blocks.append(
            '<section class="workflow-heatmap-block">'
            '<div class="workflow-heatmap-head">'
            f"<h3>{_html(workflow)}</h3>"
            '<div class="workflow-heatmap-meta">'
            f"{_meta_pill('Runs', _format_number(report.get('total_runs', 0)))}"
            f"{_meta_pill('Failed', _format_number(report.get('failed_runs', 0)))}"
            f"{_meta_pill('Unique failures', _format_number(report.get('unique_failures', 0)))}"
            f"{missing_pill}"
            "</div></div>"
            + _daily_heatmap_table(report, filter_id=f"daily-heatmap-{slug}")
            + "</section>"
        )
    return (
        '<p class="split-note">Failures stay separated by run type so weekly-only regressions do not distort the Daily view. A dash means no run data was available for that date.</p>'
        '<div class="workflow-heatmap-stack">'
        + "".join(blocks)
        + "</div>"
    )


def _daily_run_rows(dashboard: JsonObject) -> list[list[object]]:
    daily_health = _mapping(dashboard.get("daily_health"))
    repo = _str(daily_health.get("repo"), _top_repo_label(dashboard))
    runs_by_date: dict[str, list[JsonObject]] = {}
    for run in _list(daily_health.get("runs")):
        if not isinstance(run, dict):
            continue
        run_data = _mapping(run)
        date = _str(run_data.get("date"))
        if not date:
            continue
        runs_by_date.setdefault(date, []).append(run_data)

    # Determine which workflows were expected so missing-date rows are specific.
    expected_workflows = [
        _str(item) for item in _list(daily_health.get("workflows")) if _str(item)
    ]
    if not expected_workflows:
        reports = _workflow_reports(daily_health)
        expected_workflows = [
            _str(report.get("workflow"))
            for report in reports
            if _str(report.get("workflow"))
        ]

    rows: list[list[object]] = []
    ordered_dates = sorted(
        [_str(item) for item in _list(daily_health.get("dates")) if _str(item)]
        or list(runs_by_date.keys()),
        reverse=True,
    )
    for date in ordered_dates:
        date_runs = runs_by_date.get(date, [])
        if not date_runs:
            if expected_workflows:
                for workflow in expected_workflows:
                    rows.append(
                        [
                            date,
                            _chip(_workflow_label(workflow), tone="info"),
                            _chip("no data", tone="warn"),
                            "—",
                            "—",
                            "—",
                            "—",
                        ]
                    )
            else:
                rows.append(
                    [
                        date,
                        "—",
                        _chip("no data", tone="warn"),
                        "—",
                        "—",
                        "—",
                        "—",
                    ]
                )
            continue
        for run_data in date_runs:
            sha = _str(run_data.get("full_sha") or run_data.get("commit_sha"))
            rows.append(
                [
                    date,
                    _chip(_workflow_label(run_data.get("workflow")), tone="info"),
                    _chip(run_data.get("status", "")),
                    _link_external(_short_sha(sha), _commit_url(repo, sha)),
                    run_data.get("unique_failures", 0),
                    run_data.get("failed_jobs", 0),
                    _link_external("run", run_data.get("run_url", "")),
                ]
            )
    return rows


def _campaign_rows(dashboard: JsonObject) -> tuple[list[list[object]], list[str]]:
    flaky_tests = _mapping(dashboard.get("flaky_tests"))
    campaigns = [
        _mapping(campaign)
        for campaign in _list(flaky_tests.get("recent_campaigns"))
        if isinstance(campaign, dict)
    ]
    rows: list[list[object]] = []
    attrs: list[str] = []
    for campaign in campaigns:
        proof_url = _str(campaign.get("proof_url"))
        pr_url = _str(campaign.get("pr_url"))
        queued = isinstance(campaign.get("queued_pr_payload"), dict)
        pr_cell: object
        if pr_url:
            pr_cell = _link_external("PR", pr_url)
        elif queued:
            pr_cell = _chip("queued", tone="warn")
        else:
            pr_cell = "n/a"
        proof_cell: object
        if proof_url:
            proof_label = _str(campaign.get("proof_status"), "proof")
            proof_cell = _link_external(proof_label, proof_url)
        elif _str(campaign.get("proof_status")):
            proof_cell = _chip(campaign.get("proof_status", ""))
        else:
            proof_cell = "n/a"
        rows.append(
            [
                campaign.get("failure_identifier", ""),
                campaign.get("subsystem", ""),
                _chip(campaign.get("status", "")),
                proof_cell,
                campaign.get("total_attempts", 0),
                campaign.get("consecutive_full_passes", 0),
                pr_cell,
                campaign.get("updated_at", ""),
            ]
        )
        attrs.append(
            'data-filter-item="'
            + _html_attr(
                " ".join(
                    [
                        _str(campaign.get("failure_identifier")),
                        _str(campaign.get("subsystem")),
                        _str(campaign.get("job_name")),
                        _str(campaign.get("branch")),
                    ]
                )
            )
            + '"'
        )
    return rows, attrs


def _render_daily(dashboard: JsonObject) -> str:
    daily_health = _mapping(dashboard.get("daily_health"))
    ci_failures = _mapping(dashboard.get("ci_failures"))
    campaign_rows, campaign_attrs = _campaign_rows(dashboard)
    body = (
        '<section class="page-grid page-grid-wide">'
        + _panel(
            "Failure heatmap",
            _daily_heatmap(daily_health),
            subtitle="Recurring failures stay split by workflow so the run type is visible before you decide what is noise and what is signal.",
            wide=True,
        )
        + _panel(
            "Recent monitored runs",
            _table(
                ["Date", "Run type", "Status", "Commit", "Unique failures", "Failed jobs", "Run"],
                _daily_run_rows(dashboard),
                empty="No monitored run records were supplied.",
            ),
            subtitle=(
                f"Queued failures: {_format_number(ci_failures.get('queued_failures', 0))}. "
                "Workflow type, commits, and runs resolve back to GitHub. Dates with no run data stay visible."
            ),
            wide=True,
        )
        + _panel(
            "Active remediation campaigns",
            '<div class="toolbar"><label class="search"><span>Filter campaigns</span>'
            '<input type="search" placeholder="memory, timeout, replication..." '
            'data-filter-target="campaign-table"></label></div>'
            + '<div id="campaign-table">'
            + _table(
                ["Failure", "Subsystem", "Status", "Proof", "Attempts", "Pass streak", "Draft/PR", "Updated"],
                campaign_rows,
                empty="No flaky remediation campaigns were available.",
                row_attrs=campaign_attrs,
            )
            + "</div>",
            subtitle="Flaky campaigns are folded into Daily because they are the same operator problem: repeated failure pressure plus the remediation loop around it.",
            wide=True,
            anchor="campaigns",
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="daily.html",
        page_title="Daily CI",
        eyebrow="Failure Surface",
        intro="Recurring failures, recent commits, and active remediation loops.",
        body=body,
        header_metrics=_daily_metrics(dashboard),
    )


def _render_daily_home(dashboard: JsonObject) -> str:
    daily_health = _mapping(dashboard.get("daily_health"))
    ci_failures = _mapping(dashboard.get("ci_failures"))
    campaign_rows, campaign_attrs = _campaign_rows(dashboard)
    body = (
        '<section class="page-grid page-grid-wide">'
        + _panel(
            "Failure heatmap",
            _daily_heatmap(daily_health),
            subtitle="Recurring failures stay split by workflow so the run type is visible before you decide what is noise and what is signal.",
            wide=True,
        )
        + _panel(
            "Recent monitored runs",
            _table(
                ["Date", "Run type", "Status", "Commit", "Unique failures", "Failed jobs", "Run"],
                _daily_run_rows(dashboard),
                empty="No monitored run records were supplied.",
            ),
            subtitle=(
                f"Queued failures: {_format_number(ci_failures.get('queued_failures', 0))}. "
                "Workflow type, commits, and runs resolve back to GitHub. Dates with no run data stay visible."
            ),
            wide=True,
        )
        + _panel(
            "Active remediation campaigns",
            '<div class="toolbar"><label class="search"><span>Filter campaigns</span>'
            '<input type="search" placeholder="memory, timeout, replication..." '
            'data-filter-target="campaign-table"></label></div>'
            + '<div id="campaign-table">'
            + _table(
                ["Failure", "Subsystem", "Status", "Proof", "Attempts", "Pass streak", "Draft/PR", "Updated"],
                campaign_rows,
                empty="No flaky remediation campaigns were available.",
                row_attrs=campaign_attrs,
            )
            + "</div>",
            subtitle="Repeated failures and the remediation loop around them stay together on the primary operator page.",
            wide=True,
            anchor="campaigns",
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="index.html",
        page_title="Daily CI",
        eyebrow="Failure Surface",
        intro="Recurring failures, recent commits, and active remediation loops.",
        body=body,
        header_metrics=_daily_metrics(dashboard),
    )


def _review_metrics(dashboard: JsonObject) -> list[str]:
    pr_reviews = _mapping(dashboard.get("pr_reviews"))
    acceptance = _mapping(dashboard.get("acceptance"))
    return [
        _metric("Tracked PRs", pr_reviews.get("tracked_prs", 0), note="Durable review state"),
        _metric("Review comments", pr_reviews.get("review_comments", 0), note="Persisted comment ids"),
        _metric(
            "Coverage gaps",
            pr_reviews.get("coverage_incomplete_cases", 0),
            note="Acceptance cases with incomplete coverage",
            tone="warn",
        ),
        _metric(
            "Replay failures",
            acceptance.get("review_failed", 0),
            note=f"{_format_number(acceptance.get('review_cases', 0))} review replay cases",
            tone="bad" if _int(acceptance.get("review_failed")) else "good",
        ),
        _metric("Findings", acceptance.get("finding_count", 0), note="Replay findings recorded"),
    ]


def _review_rows(dashboard: JsonObject) -> list[list[object]]:
    pr_reviews = _mapping(dashboard.get("pr_reviews"))
    rows: list[list[object]] = []
    for item in _list(pr_reviews.get("recent_reviews")):
        if not isinstance(item, dict):
            continue
        review = _mapping(item)
        repo = _str(review.get("repo"), _top_repo_label(dashboard))
        pr_number = review.get("pr_number", "")
        sha = _str(review.get("last_reviewed_head_sha"))
        rows.append(
            [
                _link_external(f"{repo}#{pr_number}", _pull_url(repo, pr_number)),
                _link_external(_short_sha(sha), _commit_url(repo, sha)),
                _link_external(
                    _str(review.get("summary_comment_id")) or "n/a",
                    _issue_comment_url(review),
                ),
                _link_external(
                    _format_number(len(_list(review.get("review_comment_ids")))),
                    _review_comment_url(review),
                ),
                review.get("updated_at", ""),
            ]
        )
    return rows


def _coverage_status(result: JsonObject) -> _Html:
    coverage = _mapping(result.get("coverage"))
    if not coverage:
        return _chip("missing", tone="bad")
    complete = (
        not _list(coverage.get("claimed_without_tool"))
        and not _list(coverage.get("unaccounted_files"))
        and not bool(coverage.get("fetch_limit_hit"))
    )
    return _chip("covered" if complete else "incomplete", tone="good" if complete else "warn")


def _acceptance_review_rows(dashboard: JsonObject) -> tuple[list[list[object]], list[str]]:
    acceptance = _mapping(dashboard.get("acceptance"))
    repo_fallback = _top_repo_label(dashboard)
    rows: list[list[object]] = []
    attrs: list[str] = []
    for item in _list(acceptance.get("recent_review_results")):
        if not isinstance(item, dict):
            continue
        result = _mapping(item)
        repo = _str(result.get("repo"), repo_fallback)
        pr_number = _str(result.get("pr_number"))
        followups = ", ".join(_str(value) for value in _list(result.get("model_followups"))) or "none"
        rows.append(
            [
                result.get("name", ""),
                _link_external(pr_number or "n/a", _pull_url(repo, pr_number)),
                _chip("pass" if bool(result.get("passed")) else "needs follow-up"),
                _coverage_status(result),
                len(_list(result.get("findings"))),
                followups,
            ]
        )
        attrs.append(
            'data-filter-item="'
            + _html_attr(
                " ".join(
                    [
                        _str(result.get("name")),
                        _str(pr_number),
                        followups,
                    ]
                )
            )
            + '"'
        )
    return rows, attrs


def _workflow_case_rows(dashboard: JsonObject) -> list[list[object]]:
    acceptance = _mapping(dashboard.get("acceptance"))
    rows: list[list[object]] = []
    for item in _list(acceptance.get("recent_workflow_results")):
        if not isinstance(item, dict):
            continue
        result = _mapping(item)
        rows.append(
            [
                result.get("name", ""),
                result.get("workflow_path", ""),
                _chip("pass" if bool(result.get("passed")) else "needs follow-up"),
                len(_list(result.get("checks"))),
                _truncate(result.get("notes", ""), limit=100),
            ]
        )
    return rows


def _render_review(dashboard: JsonObject) -> str:
    acceptance = _mapping(dashboard.get("acceptance"))
    review_rows, review_attrs = _acceptance_review_rows(dashboard)
    body = (
        '<section class="page-grid page-grid-wide">'
        + _panel(
            "Tracked pull requests",
            _table(
                ["PR", "Head", "Summary", "Review notes", "Updated"],
                _review_rows(dashboard),
                empty="No tracked review-state rows were available.",
            ),
            subtitle="PRs, commits, summary comments, and review counts now resolve to GitHub instead of remaining plain text.",
            wide=True,
        )
        + _panel(
            "Replay review cases",
            '<div class="toolbar"><label class="search"><span>Filter replay cases</span>'
            '<input type="search" placeholder="docs, DCO, policy..." '
            'data-filter-target="replay-table"></label></div>'
            + '<div id="replay-table">'
            + _table(
                ["Case", "PR", "Verdict", "Coverage", "Findings", "Follow-ups"],
                review_rows,
                empty="No replay review results were supplied.",
                row_attrs=review_attrs,
            )
            + "</div>",
            subtitle=(
                f"Readiness: {_str(acceptance.get('readiness'), 'unknown')}. "
                "Replay proof now lives next to the PR review surface it validates."
            ),
            wide=True,
            anchor="replay",
        )
        + _panel(
            "Workflow contract cases",
            _table(
                ["Case", "Workflow", "Verdict", "Checks", "Notes"],
                _workflow_case_rows(dashboard),
                empty="No workflow contract cases were supplied.",
            ),
            subtitle="Workflow-level acceptance remains visible, but no longer needs its own standalone page.",
            wide=True,
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="review.html",
        page_title="PRs",
        eyebrow="Review Surface",
        intro="Tracked review state, replay evidence, and workflow contract checks.",
        body=body,
        header_metrics=_review_metrics(dashboard),
    )


def _fuzzer_metrics(dashboard: JsonObject) -> list[str]:
    fuzzer = _mapping(dashboard.get("fuzzer"))
    return [
        _metric("Runs seen", fuzzer.get("runs_seen", 0), note="Current payload"),
        _metric("Runs analyzed", fuzzer.get("runs_analyzed", 0), note="With classifier output"),
        _metric(
            "Anomalies",
            _mapping(fuzzer.get("status_counts")).get("anomalous", 0),
            note="Non-normal analyzed runs",
            tone="bad" if _mapping(fuzzer.get("status_counts")).get("anomalous", 0) else "good",
        ),
        _metric("Scenarios", len(_mapping(fuzzer.get("scenario_counts"))), note="Distinct affected paths"),
        _metric("Issues updated", _mapping(fuzzer.get("issue_action_counts")).get("updated", 0), note="GitHub issue actions"),
    ]


def _root_cause_summary(fuzzer: JsonObject) -> str:
    counts = _mapping(fuzzer.get("root_cause_counts"))
    if not counts:
        return '<p class="empty">No root-cause buckets recorded.</p>'
    rows = sorted(counts.items(), key=lambda item: (-_int(item[1]), item[0]))[:6]
    return (
        '<ul class="trend-list">'
        + "".join(
            f"<li><span>{_html(name)}</span><strong>{_format_number(value)}</strong></li>"
            for name, value in rows
        )
        + "</ul>"
    )


def _fuzzer_rows(dashboard: JsonObject) -> list[list[object]]:
    fuzzer = _mapping(dashboard.get("fuzzer"))
    rows: list[list[object]] = []
    for item in _list(fuzzer.get("recent_anomalies")):
        if not isinstance(item, dict):
            continue
        anomaly = _mapping(item)
        rows.append(
            [
                _link_external(anomaly.get("run_id", ""), anomaly.get("run_url", "")),
                _chip(anomaly.get("status", "")),
                _chip(anomaly.get("triage_verdict", "")),
                anomaly.get("scenario_id", ""),
                anomaly.get("seed", ""),
                anomaly.get("root_cause_category", ""),
                _link_external(anomaly.get("issue_action", "") or "n/a", anomaly.get("issue_url", "")),
                _truncate(anomaly.get("summary", ""), limit=110),
            ]
        )
    return rows


def _render_fuzzer(dashboard: JsonObject) -> str:
    fuzzer = _mapping(dashboard.get("fuzzer"))
    body = (
        '<section class="page-grid">'
        + _panel(
            "Root-cause mix",
            _root_cause_summary(fuzzer),
            subtitle="Top anomalous root-cause categories across the current payload.",
        )
        + _panel(
            "Status and issue actions",
            _stat_grid(
                [
                    ("Statuses", _chip(", ".join(sorted(_mapping(fuzzer.get("status_counts")).keys())) or "none", tone="info")),
                    ("Issue actions", _chip(", ".join(sorted(_mapping(fuzzer.get("issue_action_counts")).keys())) or "none", tone="info")),
                    ("Scenarios", _format_number(len(_mapping(fuzzer.get("scenario_counts"))))),
                    ("Runs seen", _format_number(fuzzer.get("runs_seen", 0))),
                    ("Analyzed", _format_number(fuzzer.get("runs_analyzed", 0))),
                    ("Anomalies", _format_number(_mapping(fuzzer.get("status_counts")).get("anomalous", 0))),
                ]
            ),
            subtitle="Fuzzer operators usually need the live anomaly mix before they need the full table.",
        )
        + _panel(
            "Recent anomalies",
            _table(
                ["Run", "Status", "Triage", "Scenario", "Seed", "Root cause", "Issue", "Summary"],
                _fuzzer_rows(dashboard),
                empty="No anomalous or warning fuzzer runs were supplied.",
            ),
            subtitle="Run ids and issue actions resolve to GitHub when URLs are present.",
            wide=True,
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="fuzzer.html",
        page_title="Fuzzer",
        eyebrow="Anomaly Watch",
        intro="Fuzzer anomalies, seeds, issue actions, and root-cause classifications.",
        body=body,
        header_metrics=_fuzzer_metrics(dashboard),
    )


def _ops_metrics(dashboard: JsonObject) -> list[str]:
    ci_failures = _mapping(dashboard.get("ci_failures"))
    agent_outcomes = _mapping(dashboard.get("agent_outcomes"))
    state_health = _mapping(dashboard.get("state_health"))
    ai = _mapping(dashboard.get("ai_reliability"))
    return [
        _metric("Incidents", ci_failures.get("failure_incidents", 0), note="Recent incident records"),
        _metric("Queued failures", ci_failures.get("queued_failures", 0), note="Awaiting next action", tone="warn"),
        _metric("Ledger events", agent_outcomes.get("events", 0), note="Append-only stream"),
        _metric("Watermarks", state_health.get("monitor_watermarks", 0), note="Monitor checkpoints"),
        _metric("Schema success", _format_rate(ai.get("schema_successes", 0), ai.get("schema_calls", 0)), note="AI reliability"),
    ]


def _incident_rows(dashboard: JsonObject) -> list[list[object]]:
    ci_failures = _mapping(dashboard.get("ci_failures"))
    rows: list[list[object]] = []
    for item in _list(ci_failures.get("recent_incidents")):
        if not isinstance(item, dict):
            continue
        incident = _mapping(item)
        rows.append(
            [
                incident.get("failure_identifier", ""),
                _chip(incident.get("status", "")),
                incident.get("file_path", ""),
                _link_external("PR", incident.get("pr_url", "")),
                incident.get("updated_at", ""),
            ]
        )
    return rows


def _event_rows(dashboard: JsonObject, *, limit: int = 15) -> list[list[object]]:
    agent_outcomes = _mapping(dashboard.get("agent_outcomes"))
    repo_fallback = _top_repo_label(dashboard)
    rows: list[list[object]] = []
    for item in _list(agent_outcomes.get("recent_events"))[:limit]:
        if not isinstance(item, dict):
            continue
        event = _mapping(item)
        rows.append(
            [
                event.get("created_at", ""),
                _chip(event.get("event_type", "")),
                _event_subject_cell(event, repo_fallback),
                _event_attributes_summary(_mapping(event.get("attributes"))),
            ]
        )
    return rows


def _watermark_rows(dashboard: JsonObject) -> list[list[object]]:
    state_health = _mapping(dashboard.get("state_health"))
    rows: list[list[object]] = []
    for item in _list(state_health.get("recent_watermarks")):
        if not isinstance(item, dict):
            continue
        watermark = _mapping(item)
        repo = _str(watermark.get("target_repo"))
        run_id = watermark.get("last_seen_run_id", "")
        rows.append(
            [
                watermark.get("key", ""),
                _link_external(run_id, _run_url(repo, run_id)),
                repo,
                watermark.get("workflow_file", ""),
                watermark.get("updated_at", ""),
            ]
        )
    return rows


def _ai_guardrail_rows(dashboard: JsonObject) -> str:
    ai = _mapping(dashboard.get("ai_reliability"))
    rows: list[tuple[str, object]] = [
        ("Token usage", _format_number(ai.get("token_usage", 0))),
        ("Schema calls", _format_number(ai.get("schema_calls", 0))),
        ("Schema successes", _format_number(ai.get("schema_successes", 0))),
        ("Tool-loop calls", _format_number(ai.get("tool_loop_calls", 0))),
        ("Terminal rejections", _format_number(ai.get("terminal_validation_rejections", 0))),
        ("Prompt safety", _format_percent(ai.get("prompt_safety_coverage", 0.0))),
        ("Retries", _format_number(ai.get("bedrock_retries", 0))),
        ("Retry exhausted", _format_number(ai.get("retry_exhaustions", 0))),
    ]
    gaps = [_str(item) for item in _list(ai.get("instrumentation_gaps")) if _str(item)]
    gap_html = (
        '<ul class="bullet-list">' + "".join(f"<li>{_html(gap)}</li>" for gap in gaps) + "</ul>"
        if gaps
        else '<p class="empty-inline">No instrumentation gaps recorded.</p>'
    )
    return _stat_grid(rows) + gap_html


def _warning_block(dashboard: JsonObject) -> str:
    warnings = _input_warnings(dashboard)
    if not warnings:
        return '<p class="empty-inline">No input warnings recorded.</p>'
    return (
        '<ul class="bullet-list">'
        + "".join(f"<li>{_html(item)}</li>" for item in warnings)
        + "</ul>"
    )


def _render_ops(dashboard: JsonObject) -> str:
    body = (
        '<section class="page-grid page-grid-wide">'
        + _panel(
            "Data coverage",
            _coverage_table(dashboard),
            subtitle="Missing and partial sources are called out here first so the rest of the dashboard can be read in context.",
            wide=True,
        )
        + _panel(
            "Incident queue",
            _table(
                ["Failure", "Status", "Path", "PR", "Updated"],
                _incident_rows(dashboard),
                empty="No recent incidents were present.",
            ),
            wide=True,
        )
        + _panel(
            "Event stream",
            _table(
                ["Time", "Event", "Subject", "Detail"],
                _event_rows(dashboard),
                empty="No recent event-ledger rows were available.",
            ),
            wide=True,
            anchor="event-stream",
        )
        + _panel(
            "Monitor watermarks",
            _table(
                ["Key", "Last run", "Repo", "Workflow", "Updated"],
                _watermark_rows(dashboard),
                empty="No monitor watermarks were supplied.",
            ),
            wide=True,
            anchor="watermarks",
        )
        + _panel(
            "AI reliability",
            _ai_guardrail_rows(dashboard),
            subtitle="AI counters remain available, but now live under Ops instead of taking a full standalone page.",
            anchor="ai-reliability",
        )
        + _panel(
            "Input warnings",
            _warning_block(dashboard),
            subtitle="Raw loader warnings from missing or unreadable input files.",
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="",
        page_title="Diagnostics",
        eyebrow="State and Coverage",
        intro="Source coverage, incident queue, event ledger, watermarks, and AI counters.",
        body=body,
        header_metrics=_ops_metrics(dashboard),
    )


def _redirect_page(title: str, target: str, reason: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0; url={_html_attr(target)}">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html(title)} · Redirect</title>
  <link rel="stylesheet" href="assets/site.css">
</head>
<body class="redirect-body">
  <main class="redirect-card">
    <img src="assets/valkey-horizontal.svg" alt="Valkey logo">
    <h1>{_html(title)} moved</h1>
    <p>{_html(reason)}</p>
    <p>{_html_cell(_link("Open the updated page", target))}</p>
  </main>
</body>
</html>"""


def _site_css() -> str:
    return """
:root {
  color-scheme: dark;
  --brand-ink: #1a2026;
  --brand-indigo: #30176e;
  --brand-cobalt: #0053b8;
  --brand-blue: #6983ff;
  --brand-amber: #cc9316;
  --bg-0: #0b1220;
  --bg-1: #0f1726;
  --bg-2: #111b2e;
  --panel: #111c2d;
  --panel-strong: #0d1727;
  --panel-soft: #152238;
  --line: rgba(164, 181, 218, 0.14);
  --line-strong: rgba(105, 131, 255, 0.42);
  --text: #e6eef9;
  --muted: #97a7c1;
  --heading: #f8fbff;
  --accent: #6983ff;
  --accent-soft: rgba(105, 131, 255, 0.14);
  --good: #20b47a;
  --good-soft: rgba(32, 180, 122, 0.14);
  --warn: #cc9316;
  --warn-soft: rgba(204, 147, 22, 0.16);
  --bad: #cf3c4f;
  --bad-soft: rgba(207, 60, 79, 0.16);
  --shadow: 0 8px 18px rgba(4, 8, 15, 0.28);
  --radius-lg: 16px;
  --radius-md: 12px;
  --radius-sm: 10px;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  min-height: 100vh;
  background: linear-gradient(180deg, var(--bg-0), var(--bg-1) 48%, var(--bg-2) 100%);
  color: var(--text);
  font: 15px/1.55 "Open Sans", "Segoe UI", sans-serif;
}
body::before {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  opacity: 0.18;
  background-image:
    linear-gradient(rgba(105, 131, 255, 0.08) 1px, transparent 1px),
    linear-gradient(90deg, rgba(105, 131, 255, 0.08) 1px, transparent 1px);
  background-size: 72px 72px;
  mask-image: linear-gradient(180deg, rgba(0, 0, 0, 0.9), transparent 88%);
}
a {
  color: #a9c5ff;
  text-decoration: none;
}
a:hover {
  color: #dce7ff;
}
.site-shell {
  width: min(1560px, calc(100% - 32px));
  margin: 0 auto;
  padding: 18px 0 36px;
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  gap: 16px;
  position: relative;
  z-index: 1;
}
.sidebar {
  position: sticky;
  top: 20px;
  height: fit-content;
  display: grid;
  gap: 12px;
}
.brand,
.hero,
.metric,
.panel,
.page-card,
.detail-card,
.sidebar-card {
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: var(--panel);
  box-shadow: var(--shadow);
}
.brand,
.hero,
.metric,
.panel,
.page-card,
.detail-card,
.sidebar-card {
  position: relative;
  overflow: hidden;
}
.brand::before,
.hero::before,
.panel::before,
.page-card::before,
.detail-card::before,
.metric::before,
.sidebar-card::before {
  content: "";
  position: absolute;
  inset: 0 0 auto 0;
  height: 2px;
  background: linear-gradient(90deg, var(--brand-blue), rgba(105, 131, 255, 0.12), transparent);
}
.brand {
  padding: 16px;
  display: grid;
  gap: 12px;
  background: linear-gradient(180deg, #101a2c, #0c1524);
}
.brand-logo {
  padding: 12px 14px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.98);
  width: fit-content;
}
.brand-logo img {
  width: 160px;
  display: block;
}
.brand-copy p,
.sidebar-card p,
.metric p,
.eyebrow,
th {
  margin: 0 0 8px;
  font: 700 11px/1.2 "Fira Mono", monospace;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.metric p,
.eyebrow,
.summary-grid span,
.panel-subtitle,
.mini-stats span,
.toolbar span,
.empty,
.empty-inline,
.trend-note,
.meta-pill strong,
.detail-stats dt {
  color: var(--muted);
}
.brand-copy h1 {
  margin: 0;
  font-size: 26px;
  line-height: 1.1;
  color: #fff;
  font-weight: 700;
}
.brand-copy span {
  display: block;
  color: rgba(255, 255, 255, 0.8);
  font-size: 13px;
}
.nav {
  display: grid;
  gap: 8px;
}
.nav-link {
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--panel);
  display: grid;
  gap: 4px;
  transition: border-color 160ms ease, background 160ms ease;
}
.nav-link strong {
  color: var(--heading);
  font-size: 14px;
}
.nav-link span {
  color: var(--muted);
  font-size: 12px;
}
.nav-link:hover {
  border-color: var(--line-strong);
  background: #132033;
}
.nav-link-current {
  border-color: rgba(105, 131, 255, 0.54);
  background: linear-gradient(180deg, rgba(105, 131, 255, 0.14), rgba(17, 28, 45, 0.98));
}
.sidebar-card {
  padding: 14px 16px;
  display: grid;
  gap: 4px;
  background: var(--panel-strong);
}
.sidebar-card strong {
  color: var(--heading);
  font-size: 18px;
}
.sidebar-card strong .chip {
  vertical-align: middle;
}
.sidebar-card span {
  color: var(--muted);
  font-size: 13px;
}
.page {
  min-width: 0;
}
.hero {
  padding: 18px 20px;
  margin-bottom: 12px;
  border-radius: var(--radius-lg);
  background: var(--panel-strong);
}
.hero-copy {
  display: grid;
  gap: 10px;
}
.eyebrow-row {
  display: flex;
  justify-content: space-between;
  align-items: start;
  gap: 12px;
  flex-wrap: wrap;
}
.hero-meta {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.meta-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #0f1a2b;
}
.meta-pill strong {
  margin: 0;
}
.meta-pill > span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #d8e4fa;
  font-size: 12px;
  font-weight: 600;
}
.hero h2 {
  margin: 0;
  color: var(--heading);
  font-size: 34px;
  line-height: 1.05;
  font-weight: 700;
}
.hero p {
  margin: 0;
  max-width: 64rem;
  color: #b4c2da;
  font-size: 14px;
}
.hero-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}
.metric {
  padding: 14px 16px;
  display: grid;
  gap: 6px;
  background: var(--panel);
}
.metric strong {
  font-size: 26px;
  line-height: 1.1;
  color: var(--heading);
}
.metric span {
  color: var(--muted);
  font-size: 13px;
}
.metric-accent { border-color: rgba(105, 131, 255, 0.2); }
.metric-good { border-color: rgba(32, 180, 122, 0.2); }
.metric-warn { border-color: rgba(204, 147, 22, 0.2); }
.metric-bad { border-color: rgba(207, 60, 79, 0.22); }
.metric-accent::before { background: var(--brand-blue); }
.metric-good::before { background: var(--good); }
.metric-warn::before { background: var(--warn); }
.metric-bad::before { background: var(--bad); }
.page-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.page-grid-wide {
  grid-template-columns: 1fr;
}
.panel {
  padding: 16px;
  min-width: 0;
  border-radius: var(--radius-md);
}
.panel-wide {
  grid-column: 1 / -1;
}
.panel-head {
  margin-bottom: 12px;
}
.panel h2,
.page-card h3,
.detail-card h3,
.trend-block h3 {
  margin: 0;
  color: var(--heading);
  font-size: 20px;
  line-height: 1.15;
}
.panel-subtitle {
  margin: 6px 0 0;
  font-size: 13px;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.summary-grid div {
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  background: var(--panel-soft);
}
.summary-grid strong {
  display: block;
  margin-top: 6px;
  color: var(--heading);
  font-size: 18px;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.page-card,
.detail-card {
  display: block;
  padding: 16px;
  color: inherit;
}
.page-card {
  transition: border-color 160ms ease, background 160ms ease;
}
.page-card:hover {
  border-color: var(--line-strong);
  background: #132033;
}
.page-card-head,
.detail-card-head {
  display: flex;
  align-items: start;
  justify-content: space-between;
  gap: 12px;
}
.page-card-head span {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 72px;
  padding: 6px 10px;
  border-radius: 999px;
  background: rgba(105, 131, 255, 0.12);
  color: #d3dfff;
  font: 700 11px/1.2 "Fira Mono", monospace;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.page-card p,
.detail-card p {
  margin: 12px 0 0;
  color: #b7c4db;
}
.mini-stats,
.detail-stats,
.trend-list {
  margin: 14px 0 0;
  padding: 0;
  list-style: none;
}
.mini-stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.mini-stats li,
.detail-stats div {
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  background: var(--panel-soft);
}
.mini-stats strong,
.detail-stats dd {
  display: block;
  margin: 6px 0 0;
  color: var(--heading);
  font-size: 16px;
}
.detail-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 12px;
}
.search {
  display: grid;
  gap: 6px;
  min-width: min(340px, 100%);
}
.search input {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #0e1727;
  color: var(--text);
  padding: 10px 14px;
  font: inherit;
}
.search input:focus {
  outline: none;
  border-color: var(--line-strong);
  box-shadow: 0 0 0 3px rgba(105, 131, 255, 0.12);
}
.trend-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
.trend-block {
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--panel-soft);
}
.trend-note {
  margin-top: 10px;
  font-size: 13px;
}
.trend-list {
  display: grid;
  gap: 8px;
}
.trend-list li {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  border-top: 1px solid var(--line);
  padding-top: 8px;
}
.trend-list li:first-child {
  border-top: 0;
  padding-top: 0;
}
.trend-list span {
  color: #b9c7dd;
}
.trend-list strong {
  color: var(--heading);
}
.sparkline {
  width: 100%;
  height: 58px;
  display: block;
  margin-top: 10px;
}
.table-wrap,
.heatmap-wrap {
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: #0f1828;
}
table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  min-width: 720px;
}
th, td {
  padding: 11px 12px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #132033;
  color: rgba(255, 255, 255, 0.84);
}
tbody tr:nth-child(even) td {
  background: rgba(105, 131, 255, 0.035);
}
tr:last-child td,
tr:last-child th {
  border-bottom: 0;
}
.row-tone-bad td {
  background: rgba(207, 60, 79, 0.12);
}
.row-tone-warn td {
  background: rgba(204, 147, 22, 0.1);
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 5px 10px;
  background: #0f1a2a;
  font-size: 12px;
  font-weight: 700;
  color: var(--heading);
}
.chip-good {
  color: #a8f0cf;
  border-color: rgba(32, 180, 122, 0.34);
  background: var(--good-soft);
}
.chip-warn {
  color: #ffe1a4;
  border-color: rgba(204, 147, 22, 0.34);
  background: var(--warn-soft);
}
.chip-bad {
  color: #ffc4cc;
  border-color: rgba(207, 60, 79, 0.34);
  background: var(--bad-soft);
}
.chip-info {
  color: #d2deff;
  border-color: rgba(105, 131, 255, 0.34);
  background: var(--accent-soft);
}
.link {
  color: #b8cdff;
}
.link-compact {
  font: 500 12px/1.2 "Fira Mono", monospace;
  text-transform: lowercase;
}
.empty,
.empty-inline {
  margin: 0;
  color: var(--muted);
}
.bullet-list {
  margin: 0;
  padding-left: 18px;
}
.bullet-list li {
  margin-bottom: 8px;
  color: #b7c4db;
}
.heatmap-table {
  min-width: 1080px;
}
.heatmap-table .sticky-col {
  position: sticky;
  left: 0;
  z-index: 2;
  min-width: 280px;
}
.heatmap-table .secondary-col {
  left: 280px;
  z-index: 3;
  min-width: 88px;
}
.heatmap-table thead .sticky-col,
.heatmap-table thead .secondary-col {
  background: #132033;
}
.heatmap-table tbody .sticky-col {
  background: #0f1828;
}
.heatmap-table tbody .secondary-col {
  background: #111c2d;
}
.heat-row-name {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.heat-row-name span:first-child {
  font-weight: 700;
}
.heat-cell {
  min-width: 46px;
  text-align: center;
  color: #91a3bf;
  background: rgba(105, 131, 255, 0.03);
  font-weight: 700;
}
.heat-cell-missing {
  color: #6f83a1;
  background: rgba(148, 163, 184, 0.08);
}
.heat-cell-hit {
  color: #fff6f7;
  background: rgba(207, 60, 79, var(--heat-alpha));
  box-shadow: inset 0 0 0 1px rgba(120, 20, 39, 0.24);
}
.split-note {
  margin: 0 0 16px;
  color: var(--muted);
}
.workflow-heatmap-stack {
  display: grid;
  gap: 18px;
}
.workflow-heatmap-block {
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 18px;
  background: rgba(15, 24, 40, 0.82);
}
.workflow-heatmap-head {
  display: flex;
  justify-content: space-between;
  align-items: start;
  gap: 12px;
  margin-bottom: 14px;
}
.workflow-heatmap-head h3 {
  margin: 0;
}
.workflow-heatmap-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.redirect-body {
  display: grid;
  place-items: center;
}
.redirect-card {
  width: min(520px, calc(100% - 32px));
  margin: 48px auto;
  padding: 28px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--panel);
  text-align: center;
  box-shadow: var(--shadow);
}
.redirect-card img {
  width: 176px;
  margin: 0 auto 18px;
  display: block;
  background: rgba(255, 255, 255, 0.98);
  border-radius: 12px;
  padding: 12px 14px;
}
.redirect-card h1 {
  margin: 0 0 10px;
}
.redirect-card p {
  color: #b6c4db;
}
[hidden] { display: none !important; }
@media (max-width: 1240px) {
  .site-shell {
    grid-template-columns: 1fr;
  }
  .sidebar {
    position: static;
  }
  .hero-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 920px) {
  .site-shell {
    width: min(1560px, calc(100% - 20px));
  }
  .hero,
  .panel,
  .brand,
  .sidebar-card,
  .page-card,
  .detail-card,
  .metric {
    border-radius: 12px;
  }
  .hero h2 {
    font-size: 28px;
  }
  .hero-meta,
  .eyebrow-row,
  .page-card-head,
  .detail-card-head,
  .workflow-heatmap-head {
    flex-direction: column;
    align-items: start;
  }
  .hero-metrics,
  .page-grid,
  .card-grid,
  .summary-grid,
  .trend-grid,
  .mini-stats,
  .detail-stats {
    grid-template-columns: 1fr;
  }
  .heatmap-table .sticky-col,
  .heatmap-table .secondary-col {
    position: static;
  }
}
"""


def _site_js() -> str:
    return """
const searchInputs = document.querySelectorAll("[data-filter-target]");

searchInputs.forEach((input) => {
  const targetId = input.getAttribute("data-filter-target");
  const target = document.getElementById(targetId);
  if (!target) {
    return;
  }

  const items = Array.from(target.querySelectorAll("[data-filter-item]"));
  input.addEventListener("input", () => {
    const query = input.value.trim().toLowerCase();
    items.forEach((item) => {
      const haystack = (item.getAttribute("data-filter-item") || "").toLowerCase();
      item.hidden = query !== "" && !haystack.includes(query);
    });
  });
});
"""


def build_site(dashboard: JsonObject, site_dir: Path) -> None:
    """Write the full multi-page observability site."""
    pages = {
        "index.html": _render_daily_home(dashboard),
        "diagnostics.html": _render_ops(dashboard),
        "review.html": _render_review(dashboard),
        "fuzzer.html": _render_fuzzer(dashboard),
    }
    for alias_name, (target, reason) in _ALIAS_PAGES.items():
        pages[alias_name] = _redirect_page(alias_name.replace(".html", ""), target, reason)

    assets_dir = site_dir / "assets"
    data_dir = site_dir / "data"
    assets_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / "site.css").write_text(_site_css(), encoding="utf-8")
    (assets_dir / "site.js").write_text(_site_js(), encoding="utf-8")
    (assets_dir / "valkey-horizontal.svg").write_text(_VALKEY_LOGO_SVG, encoding="utf-8")
    (data_dir / "dashboard.json").write_text(
        json.dumps(dashboard, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    for name, html_text in pages.items():
        (site_dir / name).write_text(html_text, encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard-json", required=True)
    parser.add_argument("--site-dir", default="dashboard-site")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dashboard = json.loads(Path(args.dashboard_json).read_text(encoding="utf-8"))
    build_site(_mapping(dashboard), Path(args.site_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

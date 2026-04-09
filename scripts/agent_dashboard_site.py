"""Static multi-page observability site for the Valkey CI agent.

The single-file capability dashboard is useful inside workflow artifacts, but
maintainers reviewing the bot need a calmer product surface: focused pages,
stable navigation, and workflow-shaped views that feel closer to a real site
than an exported report. This module turns the structured dashboard JSON into
that publishable static site.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

_NAV_PAGES: list[tuple[str, str, str]] = [
    ("index.html", "Overview", "Control room"),
    ("daily.html", "Daily", "Failure heatmap"),
    ("flaky.html", "Flaky", "Campaign lab"),
    ("review.html", "Review", "PR quality"),
    ("acceptance.html", "Acceptance", "Replay proof"),
    ("fuzzer.html", "Fuzzer", "Anomaly watch"),
    ("ai.html", "AI", "Reliability"),
    ("ops.html", "Ops", "Ledger and state"),
]


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
    if any(word in normalized for word in ("pass", "ready", "success", "merged", "normal")):
        tone = "good"
    elif any(word in normalized for word in ("fail", "dead", "abandoned", "anomalous", "missing")):
        tone = "bad"
    elif any(word in normalized for word in ("warning", "queued", "retry", "incomplete", "needs", "pending", "running")):
        tone = "warn"
    return _safe_html(f'<span class="chip chip-{tone}">{_html(label)}</span>')


def _link(label: object, url: object) -> _Html:
    url_text = _str(url)
    if not url_text:
        return _safe_html(_html(label))
    return _safe_html(
        f'<a href="{_html_attr(url_text)}">{_html(label)}</a>'
    )


def _status_counts(counts: JsonObject) -> _Html:
    if not counts:
        return _safe_html('<span class="empty-inline">none</span>')
    parts = [
        f'{_chip(name)} <span class="count">{_format_number(value)}</span>'
        for name, value in sorted(counts.items())
    ]
    return _safe_html('<span class="chip-list">' + "".join(parts) + "</span>")


def _table(
    headers: list[str],
    rows: list[list[object]],
    *,
    empty: str,
    row_attrs: list[str] | None = None,
) -> str:
    if not rows:
        return f'<p class="empty">{_html(empty)}</p>'
    head = "".join(f"<th>{_html(header)}</th>" for header in headers)
    rendered_rows: list[str] = []
    attrs = row_attrs or []
    for index, row in enumerate(rows):
        row_attr = f" {attrs[index]}" if index < len(attrs) and attrs[index] else ""
        rendered_rows.append(
            "<tr"
            + row_attr
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


def _metric_tile(label: str, value: object, *, tone: str = "blue", note: str = "") -> str:
    note_html = f'<span>{_html(note)}</span>' if note else ""
    return (
        f'<article class="metric metric-{_html_attr(tone)}">'
        f"<p>{_html(label)}</p>"
        f"<strong>{_format_number(value)}</strong>"
        f"{note_html}"
        "</article>"
    )


def _panel(title: str, body: str, *, wide: bool = False) -> str:
    classes = "panel panel-wide" if wide else "panel"
    return f'<section class="{classes}"><h2>{_html(title)}</h2>{body}</section>'


def _summary_rows(rows: list[tuple[str, object]]) -> str:
    return (
        '<div class="summary-grid">'
        + "".join(
            f'<div><span>{_html(label)}</span><strong>{_html_cell(value)}</strong></div>'
            for label, value in rows
        )
        + "</div>"
    )


def _page_card(title: str, href: str, body: str, stats: list[tuple[str, object]]) -> str:
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


def _callout(title: str, body: str, *, tone: str = "blue") -> str:
    return (
        f'<div class="callout callout-{_html_attr(tone)}">'
        f"<strong>{_html(title)}</strong>"
        f"<p>{_html(body)}</p>"
        "</div>"
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
    min_value = min(values)
    max_value = max(values)
    spread = max(max_value - min_value, 0.0001)
    step = width / max(len(values) - 1, 1)
    points: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        x = round(index * step, 2)
        y = round(height - (((value - min_value) / spread) * (height - 12)) - 6, 2)
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
        f'<polygon points="{area}" fill="{_html_attr(color)}" opacity="0.12"></polygon>'
        f'<polyline points="{point_text}" fill="none" stroke="{_html_attr(color)}" '
        'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></polyline>'
        f"{circles}</svg>"
    )


def _top_repo_label(dashboard: JsonObject) -> str:
    daily_health = _mapping(dashboard.get("daily_health"))
    if daily_health.get("repo"):
        return _str(daily_health.get("repo"))
    recent_watermarks = _list(_mapping(dashboard.get("state_health")).get("recent_watermarks"))
    if recent_watermarks:
        return _str(_mapping(recent_watermarks[0]).get("target_repo"), "valkey-io/valkey")
    return "valkey-io/valkey"


def _site_nav(current_page: str) -> str:
    links: list[str] = []
    for href, title, description in _NAV_PAGES:
        current = ' aria-current="page"' if href == current_page else ""
        classes = "nav-link nav-link-current" if href == current_page else "nav-link"
        links.append(
            f'<a class="{classes}" href="{_html_attr(href)}"{current}>'
            f'<strong>{_html(title)}</strong><span>{_html(description)}</span></a>'
        )
    return "".join(links)


def _layout(
    dashboard: JsonObject,
    *,
    current_page: str,
    page_title: str,
    eyebrow: str,
    intro: str,
    body: str,
) -> str:
    snapshot = _mapping(dashboard.get("snapshot"))
    acceptance = _mapping(dashboard.get("acceptance"))
    repo_label = _top_repo_label(dashboard)
    generated_at = _str(dashboard.get("generated_at"), "unknown")
    hero_stats = "".join(
        [
            _metric_tile("Failure incidents", snapshot.get("failure_incidents", 0), tone="amber"),
            _metric_tile("Active flaky campaigns", snapshot.get("active_flaky_campaigns", 0), tone="blue"),
            _metric_tile("Tracked review PRs", snapshot.get("tracked_review_prs", 0), tone="green"),
            _metric_tile("Acceptance readiness", acceptance.get("readiness", "unknown"), tone="green"),
        ]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html(page_title)} · Valkey CI Agent Observatory</title>
  <link rel="stylesheet" href="assets/site.css">
</head>
<body>
  <div class="site-shell">
    <aside class="sidebar">
      <div class="brand">
        <p>Valkey CI Agent</p>
        <h1>Observatory</h1>
        <span>{_html(repo_label)}</span>
      </div>
      <nav class="nav">
        {_site_nav(current_page)}
      </nav>
      <section class="sidebar-card">
        <p>Generated</p>
        <strong>{_html(generated_at)}</strong>
      </section>
      <section class="sidebar-card">
        <p>Replay Readiness</p>
        <strong>{_html_cell(_chip(acceptance.get("readiness", "unknown")))}</strong>
      </section>
    </aside>
    <main class="page">
      <header class="hero">
        <div class="eyebrow">{_html(eyebrow)}</div>
        <h2>{_html(page_title)}</h2>
        <p>{_html(intro)}</p>
      </header>
      <section class="hero-metrics">{hero_stats}</section>
      {body}
    </main>
  </div>
  <script src="assets/site.js"></script>
</body>
</html>"""


def _render_trend_watch(dashboard: JsonObject) -> str:
    trends = _mapping(dashboard.get("trends"))
    failure_rate = _mapping(trends.get("failure_rate"))
    review_health = _mapping(trends.get("review_health"))
    flaky_subsystems = _mapping(trends.get("flaky_subsystems"))
    subsystem_series = _mapping(flaky_subsystems.get("series"))
    palette = ["#38bdf8", "#34d399", "#f59e0b", "#fb7185"]
    subsystem_rows: list[str] = []
    for index, (name, values) in enumerate(sorted(subsystem_series.items())):
        series = [_float(value) for value in _list(values)]
        subsystem_rows.append(
            '<div class="series-row">'
            f'<span class="legend-dot" style="background:{palette[index % len(palette)]}"></span>'
            f"<strong>{_html(name)}</strong>"
            f"{_sparkline_svg(series, color=palette[index % len(palette)])}"
            "</div>"
        )
    blocks = [
        (
            "Failure rate",
            _sparkline_svg(
                [_float(value) for value in _list(failure_rate.get("rates"))],
                color="#38bdf8",
            ),
            f"{_format_number(_int(failure_rate.get('window_days')))} tracked day slots",
        ),
        (
            "Review health",
            _sparkline_svg(
                [_float(value) for value in _list(review_health.get("degraded_reviews"))],
                color="#f59e0b",
            ),
            "Coverage drift and degraded review notes over time.",
        ),
    ]
    return _panel(
        "Trend Watch",
        '<div class="trend-grid">'
        + "".join(
            '<article class="trend-block"><h3>'
            + _html(title)
            + "</h3>"
            + str(svg)
            + f'<p class="trend-note">{_html(note)}</p></article>'
            for title, svg, note in blocks
        )
        + '<article class="trend-block"><h3>Flaky subsystems</h3>'
        + (
            '<div class="series-list">' + "".join(subsystem_rows) + "</div>"
            if subsystem_rows
            else '<p class="empty">No subsystem movement yet.</p>'
        )
        + "</article></div>",
        wide=True,
    )


def _render_overview(dashboard: JsonObject) -> str:
    snapshot = _mapping(dashboard.get("snapshot"))
    acceptance = _mapping(dashboard.get("acceptance"))
    ai_reliability = _mapping(dashboard.get("ai_reliability"))
    agent_outcomes = _mapping(dashboard.get("agent_outcomes"))
    page_cards = [
        _page_card(
            "Daily heatmap",
            "daily.html",
            "Track which failures keep returning, how often they hit, and whether daily stability is recovering.",
            [
                ("Runs seen", snapshot.get("daily_runs_seen", 0)),
                ("Failures", snapshot.get("failure_incidents", 0)),
            ],
        ),
        _page_card(
            "Flaky lab",
            "flaky.html",
            "See active campaigns, hypotheses that already failed, and where Valkey subsystem pain is clustering.",
            [
                ("Active", snapshot.get("active_flaky_campaigns", 0)),
                ("Queued", snapshot.get("queued_failures", 0)),
            ],
        ),
        _page_card(
            "Review quality",
            "review.html",
            "Show maintainers what the reviewer posts, how often coverage degrades, and which PRs are being tracked.",
            [
                ("Tracked PRs", snapshot.get("tracked_review_prs", 0)),
                ("Comments", snapshot.get("review_comments", 0)),
            ],
        ),
        _page_card(
            "Replay proof",
            "acceptance.html",
            "Keep the adoption story honest with a replay scorecard and workflow contract checks.",
            [
                ("Readiness", _chip(acceptance.get("readiness", "unknown"))),
                ("Review cases", acceptance.get("review_cases", 0)),
            ],
        ),
        _page_card(
            "Fuzzer watch",
            "fuzzer.html",
            "Watch anomalies, issues, seeds, and root-cause categories without hunting through raw logs.",
            [
                ("Analyzed", snapshot.get("fuzzer_runs_analyzed", 0)),
                ("Anomalous", snapshot.get("fuzzer_anomalous_runs", 0)),
            ],
        ),
        _page_card(
            "AI reliability",
            "ai.html",
            "Audit how the model is behaving: schema success rate, retries, tool-loop quality, and safety coverage.",
            [
                ("Tokens", snapshot.get("ai_token_usage", 0)),
                ("Gaps", snapshot.get("instrumentation_gaps", 0)),
            ],
        ),
    ]

    recent_events = _list(agent_outcomes.get("recent_events"))
    event_rows = [
        [
            event.get("created_at", ""),
            _chip(event.get("event_type", "")),
            event.get("subject", ""),
            json.dumps(_mapping(event.get("attributes")), sort_keys=True)[:180],
        ]
        for event in recent_events
        if isinstance(event, dict)
    ]

    body = (
        '<section class="page-grid page-grid-wide">'
        + _panel(
            "Why this site exists",
            _callout(
                "Made for maintainers, not just workflows",
                "Each page focuses on one Valkey workflow surface so you can answer a real operational question quickly, instead of decoding one giant report.",
            )
            + '<div class="card-grid">'
            + "".join(page_cards)
            + "</div>",
            wide=True,
        )
        + _render_trend_watch(dashboard)
        + _panel(
            "Executive pulse",
            _summary_rows(
                [
                    ("Replay readiness", _chip(acceptance.get("readiness", "unknown"))),
                    ("PRs created", agent_outcomes.get("prs_created", 0)),
                    ("PRs merged", agent_outcomes.get("prs_merged", 0)),
                    ("Prompt safety", _format_percent(ai_reliability.get("prompt_safety_coverage", 0.0))),
                    ("Schema calls", ai_reliability.get("schema_calls", 0)),
                    ("Terminal rejections", ai_reliability.get("terminal_validation_rejections", 0)),
                ]
            )
            + _callout(
                "Adoption story",
                "Replay proof, Daily stability, PR review quality, fuzzer anomalies, and AI reliability now live in one place with a shared visual language.",
                tone="green",
            ),
        )
        + _panel(
            "Latest agent outcomes",
            _table(
                ["Time", "Type", "Subject", "Attributes"],
                event_rows,
                empty="No recent events were recorded.",
            ),
            wide=True,
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="index.html",
        page_title="Overview",
        eyebrow="Control Room",
        intro="A polished front door for the Valkey CI agent: one shared observability site, with each workflow family getting its own page.",
        body=body,
    )


def _daily_heatmap(daily_health: JsonObject) -> str:
    heatmap_rows = [
        _mapping(row)
        for row in _list(daily_health.get("heatmap"))
        if isinstance(row, dict)
    ]
    dates = [_str(date) for date in _list(daily_health.get("dates"))]
    if not heatmap_rows or not dates:
        return '<p class="empty">No daily health heatmap is available yet.</p>'
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
    for row in heatmap_rows[:24]:
        cells = []
        for cell in _list(row.get("cells")):
            data = _mapping(cell)
            count = _int(data.get("count"))
            alpha = 0.12 + (count / max_count) * 0.88 if count else 0.0
            text = str(count) if count else ""
            style = (
                f' style="--heat-alpha:{alpha:.2f}"'
                if count
                else ""
            )
            classes = "heat-cell heat-cell-hit" if count else "heat-cell"
            cells.append(
                f'<td class="{classes}"{style} title="{_html_attr(data.get("date"))}: {count}">'
                f"{_html(text)}</td>"
            )
        body_rows.append(
            '<tr data-filter-item="'
            + _html_attr(_str(row.get("name")))
            + '"><th class="sticky-col">'
            + _html(_str(row.get("name")))
            + '</th><td class="sticky-col secondary-col">'
            + _html(f"{_int(row.get('days_failed'))}/{_int(row.get('total_days'))}d")
            + "</td>"
            + "".join(cells)
            + "</tr>"
        )
    return (
        '<div class="toolbar"><label class="search"><span>Filter failures</span>'
        '<input type="search" placeholder="replication, jemalloc, valgrind..." '
        'data-filter-target="daily-heatmap"></label></div>'
        '<div class="heatmap-wrap" id="daily-heatmap"><table class="heatmap-table"><thead><tr>'
        '<th class="sticky-col">Failure</th><th class="sticky-col secondary-col">Freq</th>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
    )


def _render_daily(dashboard: JsonObject) -> str:
    daily_health = _mapping(dashboard.get("daily_health"))
    ci_failures = _mapping(dashboard.get("ci_failures"))
    runs = [
        _mapping(run)
        for run in _list(daily_health.get("runs"))
        if isinstance(run, dict)
    ]
    body = (
        '<section class="page-grid page-grid-wide">'
        + _panel(
            "Daily stability snapshot",
            _summary_rows(
                [
                    ("Tracked days", len(_list(daily_health.get("dates")))),
                    ("Runs", daily_health.get("total_runs", 0)),
                    ("Failed runs", daily_health.get("failed_runs", 0)),
                    ("Unique failures", daily_health.get("unique_failures", 0)),
                    ("Queued failures", ci_failures.get("queued_failures", 0)),
                    ("Recent incidents", ci_failures.get("failure_incidents", 0)),
                ]
            )
            + _callout(
                "Daily-focused view",
                "This page is intentionally shaped like the maintainer question: what keeps failing in Daily, how often, and is it getting better or worse?",
            ),
            wide=True,
        )
        + _panel("Failure heatmap", _daily_heatmap(daily_health), wide=True)
        + _panel(
            "Recent Daily runs",
            _table(
                ["Date", "Status", "Commit", "Unique Failures", "Failed Jobs", "Run"],
                [
                    [
                        run.get("date", ""),
                        _chip(run.get("status", "")),
                        run.get("commit_sha", ""),
                        run.get("unique_failures", 0),
                        run.get("failed_jobs", 0),
                        _link("Open", run.get("run_url", "")),
                    ]
                    for run in runs[:14]
                ],
                empty="No Daily run data was supplied.",
            ),
            wide=True,
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="daily.html",
        page_title="Daily",
        eyebrow="Failure Heatmap",
        intro="A focused Daily view, built to feel closer to a maintainer dashboard than a workflow artifact.",
        body=body,
    )


def _campaign_cards(flaky_tests: JsonObject) -> str:
    campaigns = [
        _mapping(campaign)
        for campaign in _list(flaky_tests.get("recent_campaigns"))
        if isinstance(campaign, dict)
    ]
    if not campaigns:
        return '<p class="empty">No flaky campaigns are active right now.</p>'
    cards: list[str] = []
    for campaign in campaigns:
        hypotheses = _list(campaign.get("failed_hypotheses"))
        hypothesis_list = "".join(
            f"<li>{_html(hypothesis)}</li>"
            for hypothesis in hypotheses[:3]
        ) or "<li>none yet</li>"
        proof_runs = (
            f"{_format_number(campaign.get('proof_passed_runs', 0))}/"
            f"{_format_number(campaign.get('proof_required_runs', 0))}"
            if _int(campaign.get("proof_required_runs", 0))
            else "n/a"
        )
        cards.append(
            '<article class="detail-card" data-filter-item="'
            + _html_attr(
                " ".join(
                    [
                        _str(campaign.get("failure_identifier")),
                        _str(campaign.get("subsystem")),
                        _str(campaign.get("job_name")),
                    ]
                )
            )
            + '"><div class="detail-card-head"><h3>'
            + _html(_str(campaign.get("failure_identifier")))
            + "</h3>"
            + str(_chip(campaign.get("status", "")))
            + str(_chip(campaign.get("proof_status", "")))
            + '</div><p class="detail-meta">'
            + _html(
                f"{_str(campaign.get('subsystem'), 'unknown subsystem')} · {_str(campaign.get('job_name'))} · {_str(campaign.get('branch'))}"
            )
            + '</p><dl class="detail-stats">'
            + f"<div><dt>Attempts</dt><dd>{_format_number(campaign.get('total_attempts', 0))}</dd></div>"
            + f"<div><dt>Full passes</dt><dd>{_format_number(campaign.get('consecutive_full_passes', 0))}</dd></div>"
            + f"<div><dt>Proof runs</dt><dd>{_html(proof_runs)}</dd></div>"
            + f"<div><dt>Queued PR</dt><dd>{_html('yes' if isinstance(campaign.get('queued_pr_payload'), dict) else 'no')}</dd></div>"
            + "</dl><h4>Failed hypotheses</h4><ul>"
            + hypothesis_list
            + "</ul></article>"
        )
    return (
        '<div class="toolbar"><label class="search"><span>Filter campaigns</span>'
        '<input type="search" placeholder="memory, replication, timeout..." data-filter-target="campaign-grid"></label></div>'
        '<div class="card-grid" id="campaign-grid">' + "".join(cards) + "</div>"
    )


def _render_flaky(dashboard: JsonObject) -> str:
    flaky_tests = _mapping(dashboard.get("flaky_tests"))
    body = (
        '<section class="page-grid">'
        + _panel(
            "Campaign health",
            _summary_rows(
                [
                    ("Total campaigns", flaky_tests.get("campaigns", 0)),
                    ("Active", flaky_tests.get("active_campaigns", 0)),
                    ("Attempts", flaky_tests.get("total_attempts", 0)),
                    ("Failed hypotheses", flaky_tests.get("failed_hypotheses", 0)),
                    ("Full passes", flaky_tests.get("consecutive_full_passes", 0)),
                    ("Proof", _status_counts(_mapping(flaky_tests.get("proof_counts")))),
                    ("Subsystem mix", _status_counts(_mapping(flaky_tests.get("subsystem_counts")))),
                ]
            ),
            wide=True,
        )
        + _panel("Campaign board", _campaign_cards(flaky_tests), wide=True)
        + _panel(
            "Status mix",
            _summary_rows(
                [
                    ("Status counts", _status_counts(_mapping(flaky_tests.get("status_counts")))),
                    ("Subsystems", _status_counts(_mapping(flaky_tests.get("subsystem_counts")))),
                ]
            )
            + _callout(
                "What makes this page different",
                "It keeps the experiment history visible, so the bot looks less like it is thrashing and more like it is learning.",
                tone="green",
            ),
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="flaky.html",
        page_title="Flaky",
        eyebrow="Campaign Lab",
        intro="A dedicated surface for flaky failures: subsystem clustering, prior failed ideas, validation streaks, and queued PR pressure.",
        body=body,
    )


def _render_review(dashboard: JsonObject) -> str:
    pr_reviews = _mapping(dashboard.get("pr_reviews"))
    acceptance = _mapping(dashboard.get("acceptance"))
    reviews = [
        _mapping(review)
        for review in _list(pr_reviews.get("recent_reviews"))
        if isinstance(review, dict)
    ]
    body = (
        '<section class="page-grid">'
        + _panel(
            "Reviewer pulse",
            _summary_rows(
                [
                    ("Tracked PRs", pr_reviews.get("tracked_prs", 0)),
                    ("Summary comments", pr_reviews.get("summary_comments", 0)),
                    ("Review comments", pr_reviews.get("review_comments", 0)),
                    ("Coverage incomplete", pr_reviews.get("coverage_incomplete_cases", 0)),
                    ("Acceptance passed", pr_reviews.get("acceptance_passed", 0)),
                    ("Model followups", _status_counts(_mapping(pr_reviews.get("model_followup_counts")))),
                ]
            ),
            wide=True,
        )
        + _panel(
            "Tracked pull requests",
            _table(
                ["PR", "Head SHA", "Summary", "Review Comments", "Updated"],
                [
                    [
                        f"{review.get('repo', '')}#{review.get('pr_number', '')}",
                        review.get("last_reviewed_head_sha", ""),
                        review.get("summary_comment_id", ""),
                        len(_list(review.get("review_comment_ids"))),
                        review.get("updated_at", ""),
                    ]
                    for review in reviews
                ],
                empty="No tracked PR review state was available.",
            ),
            wide=True,
        )
        + _panel(
            "Replay signal",
            _summary_rows(
                [
                    ("Readiness", _chip(acceptance.get("readiness", "unknown"))),
                    ("Review cases", acceptance.get("review_cases", 0)),
                    ("Review passed", acceptance.get("review_passed", 0)),
                    ("Review failed", acceptance.get("review_failed", 0)),
                    ("Findings", acceptance.get("finding_count", 0)),
                    ("Replay followups", _status_counts(_mapping(acceptance.get("model_followup_counts")))),
                ]
            )
            + _callout(
                "Defect-oriented by design",
                "This page keeps the reviewer honest: high-confidence findings, replay proof, and visible followups when coverage degrades.",
            ),
            wide=True,
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="review.html",
        page_title="Review",
        eyebrow="PR Quality",
        intro="A maintainer-facing view of what the reviewer is posting, how complete the coverage is, and whether replay cases still pass.",
        body=body,
    )


def _acceptance_review_rows(acceptance: JsonObject) -> tuple[list[list[object]], list[str]]:
    rows: list[list[object]] = []
    attrs: list[str] = []
    for result in _list(acceptance.get("recent_review_results")):
        if not isinstance(result, dict):
            continue
        followups = ", ".join(_str(value) for value in _list(result.get("model_followups"))) or "none"
        expectation_checks = _list(result.get("expectation_checks"))
        passed_checks = sum(
            1 for check in expectation_checks if _mapping(check).get("passed") is True
        )
        rows.append(
            [
                result.get("name", ""),
                result.get("pr_number", ""),
                _chip("pass" if bool(result.get("passed")) else "needs follow-up"),
                f"{passed_checks}/{len(expectation_checks)}",
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
                        str(result.get("pr_number")),
                        followups,
                    ]
                )
            )
            + '"'
        )
    return rows, attrs


def _render_acceptance(dashboard: JsonObject) -> str:
    acceptance = _mapping(dashboard.get("acceptance"))
    review_rows, review_row_attrs = _acceptance_review_rows(acceptance)
    workflow_rows = [
        [
            result.get("name", ""),
            result.get("workflow_path", ""),
            _chip("pass" if bool(result.get("passed")) else "needs follow-up"),
            len(_list(result.get("checks"))),
            result.get("notes", ""),
        ]
        for result in _list(acceptance.get("recent_workflow_results"))
        if isinstance(result, dict)
    ]
    body = (
        '<section class="page-grid">'
        + _panel(
            "Replay scorecard",
            _summary_rows(
                [
                    ("Readiness", _chip(acceptance.get("readiness", "unknown"))),
                    ("Review cases", acceptance.get("review_cases", 0)),
                    ("Workflow cases", acceptance.get("workflow_cases", 0)),
                    ("CI replay cases", acceptance.get("ci_replay_cases", 0)),
                    ("Backport cases", acceptance.get("backport_replay_cases", 0)),
                    ("Payloads seen", acceptance.get("payloads_seen", 0)),
                ]
            )
            + _callout(
                "Proof over vibes",
                "This page is the adoption anchor: it makes the bot prove itself against real Valkey-shaped cases instead of relying on a nice demo alone.",
                tone="green",
            ),
            wide=True,
        )
        + _panel(
            "Review replay cases",
            '<div class="toolbar"><label class="search"><span>Filter replay cases</span>'
            '<input type="search" placeholder="docs, DCO, core-team..." data-filter-target="acceptance-reviews"></label></div>'
            + _table(
                ["Case", "PR", "Verdict", "Checks", "Findings", "Followups"],
                review_rows,
                empty="No replay review results were available.",
                row_attrs=review_row_attrs,
            ),
            wide=True,
        )
        + _panel(
            "Workflow contract cases",
            _table(
                ["Case", "Workflow", "Verdict", "Checks", "Notes"],
                workflow_rows,
                empty="No workflow contract results were available.",
            ),
            wide=True,
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="acceptance.html",
        page_title="Acceptance",
        eyebrow="Replay Proof",
        intro="The proof layer for rollout conversations: replay verdicts, workflow contracts, and the cases that still need follow-up.",
        body=body,
    )


def _render_fuzzer(dashboard: JsonObject) -> str:
    fuzzer = _mapping(dashboard.get("fuzzer"))
    anomalies = [
        _mapping(anomaly)
        for anomaly in _list(fuzzer.get("recent_anomalies"))
        if isinstance(anomaly, dict)
    ]
    body = (
        '<section class="page-grid">'
        + _panel(
            "Fuzzer pulse",
            _summary_rows(
                [
                    ("Runs seen", fuzzer.get("runs_seen", 0)),
                    ("Runs analyzed", fuzzer.get("runs_analyzed", 0)),
                    ("Raw-log fallbacks", fuzzer.get("raw_log_fallbacks", 0)),
                    ("Statuses", _status_counts(_mapping(fuzzer.get("status_counts")))),
                    ("Issue actions", _status_counts(_mapping(fuzzer.get("issue_action_counts")))),
                    ("Root causes", _status_counts(_mapping(fuzzer.get("root_cause_counts")))),
                ]
            ),
            wide=True,
        )
        + _panel(
            "Recent anomalies",
            _table(
                ["Run", "Status", "Triage", "Scenario", "Seed", "Root Cause", "Issue", "Summary"],
                [
                    [
                        _link(anomaly.get("run_id", ""), anomaly.get("run_url", "")),
                        _chip(anomaly.get("status", "")),
                        _chip(anomaly.get("triage_verdict", "")),
                        anomaly.get("scenario_id", ""),
                        anomaly.get("seed", ""),
                        anomaly.get("root_cause_category", ""),
                        _link(anomaly.get("issue_action", ""), anomaly.get("issue_url", "")),
                        anomaly.get("summary", ""),
                    ]
                    for anomaly in anomalies
                ],
                empty="No warning or anomalous fuzzer runs were available in the supplied data.",
            ),
            wide=True,
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="fuzzer.html",
        page_title="Fuzzer",
        eyebrow="Anomaly Watch",
        intro="A focused view for fuzzer maintainers: scenario IDs, seeds, issue actions, and the root-cause categories that keep recurring.",
        body=body,
    )


def _render_ai(dashboard: JsonObject) -> str:
    ai = _mapping(dashboard.get("ai_reliability"))
    gaps = [_str(gap) for gap in _list(ai.get("instrumentation_gaps"))]
    body = (
        '<section class="page-grid">'
        + _panel(
            "Reliability pulse",
            _summary_rows(
                [
                    ("Token usage", ai.get("token_usage", 0)),
                    ("Schema calls", ai.get("schema_calls", 0)),
                    ("Schema successes", ai.get("schema_successes", 0)),
                    ("Tool loop calls", ai.get("tool_loop_calls", 0)),
                    ("Tool loop successes", ai.get("tool_loop_successes", 0)),
                    ("Prompt safety", _format_percent(ai.get("prompt_safety_coverage", 0.0))),
                ]
            ),
            wide=True,
        )
        + _panel(
            "Measured AI events",
            _table(
                ["Event", "Count"],
                [[name, count] for name, count in sorted(_mapping(ai.get("ai_metrics")).items())],
                empty="No persisted AI event counters were present.",
            ),
        )
        + _panel(
            "Guardrails",
            _summary_rows(
                [
                    ("ToolChoice rejections", ai.get("schema_tool_choice_rejections", 0)),
                    ("Fallback successes", ai.get("schema_tool_choice_fallback_successes", 0)),
                    ("Terminal rejections", ai.get("terminal_validation_rejections", 0)),
                    ("Bedrock retries", ai.get("bedrock_retries", 0)),
                    ("Retry exhaustions", ai.get("retry_exhaustions", 0)),
                    ("Non-retryable", ai.get("non_retryable_errors", 0)),
                ]
            )
            + (
                '<ul class="bullet-list">' + "".join(f"<li>{_html(gap)}</li>" for gap in gaps) + "</ul>"
                if gaps
                else '<p class="empty">No instrumentation gaps recorded.</p>'
            ),
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="ai.html",
        page_title="AI",
        eyebrow="Reliability",
        intro="The AI usage page: schema discipline, safety coverage, tool-loop quality, retries, and the gaps we still need to instrument.",
        body=body,
    )


def _render_ops(dashboard: JsonObject) -> str:
    ci_failures = _mapping(dashboard.get("ci_failures"))
    agent_outcomes = _mapping(dashboard.get("agent_outcomes"))
    state_health = _mapping(dashboard.get("state_health"))
    incidents = [
        _mapping(incident)
        for incident in _list(ci_failures.get("recent_incidents"))
        if isinstance(incident, dict)
    ]
    watermarks = [
        _mapping(item)
        for item in _list(state_health.get("recent_watermarks"))
        if isinstance(item, dict)
    ]
    body = (
        '<section class="page-grid">'
        + _panel(
            "Operations pulse",
            _summary_rows(
                [
                    ("Failure incidents", ci_failures.get("failure_incidents", 0)),
                    ("Queued failures", ci_failures.get("queued_failures", 0)),
                    ("History observations", ci_failures.get("history_observations", 0)),
                    ("PRs created", agent_outcomes.get("prs_created", 0)),
                    ("PRs merged", agent_outcomes.get("prs_merged", 0)),
                    ("Dead-lettered", agent_outcomes.get("dead_lettered", 0)),
                ]
            ),
            wide=True,
        )
        + _panel(
            "Recent incidents",
            _table(
                ["Failure", "Status", "Path", "Updated"],
                [
                    [
                        incident.get("failure_identifier", ""),
                        _chip(incident.get("status", "")),
                        incident.get("file_path", ""),
                        incident.get("updated_at", ""),
                    ]
                    for incident in incidents
                ],
                empty="No recent incidents were present.",
            ),
            wide=True,
        )
        + _panel(
            "Outcome ledger",
            _table(
                ["Time", "Type", "Subject", "Attributes"],
                [
                    [
                        event.get("created_at", ""),
                        _chip(event.get("event_type", "")),
                        event.get("subject", ""),
                        json.dumps(_mapping(event.get("attributes")), sort_keys=True)[:180],
                    ]
                    for event in _list(agent_outcomes.get("recent_events"))
                    if isinstance(event, dict)
                ],
                empty="No recent agent events were available.",
            ),
            wide=True,
        )
        + _panel(
            "State watermarks",
            _table(
                ["Key", "Last Run", "Target Repo", "Workflow", "Updated"],
                [
                    [
                        watermark.get("key", ""),
                        watermark.get("last_seen_run_id", ""),
                        watermark.get("target_repo", ""),
                        watermark.get("workflow_file", ""),
                        watermark.get("updated_at", ""),
                    ]
                    for watermark in watermarks
                ],
                empty="No monitor watermarks were present.",
            ),
        )
        + _panel(
            "Input warnings",
            _table(
                ["Warning"],
                [[warning] for warning in _list(state_health.get("input_warnings"))],
                empty="No input warnings.",
            ),
        )
        + "</section>"
    )
    return _layout(
        dashboard,
        current_page="ops.html",
        page_title="Ops",
        eyebrow="Ledger and State",
        intro="The operational backbone: incident queue, outcome ledger, and the monitor state that keeps the bot from losing context.",
        body=body,
    )


def _site_css() -> str:
    return """
:root {
  color-scheme: dark;
  --bg: #07111f;
  --panel: #0f1b2d;
  --panel-strong: #13233a;
  --line: #26364f;
  --line-strong: #355274;
  --text: #e7edf7;
  --muted: #95a5bb;
  --blue: #38bdf8;
  --green: #34d399;
  --amber: #f59e0b;
  --red: #f87171;
  --shadow: 0 20px 45px rgba(2, 8, 18, 0.28);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: linear-gradient(180deg, #07111f 0%, #091728 100%);
  color: var(--text);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }
.site-shell {
  width: min(1480px, calc(100% - 28px));
  margin: 0 auto;
  padding: 18px 0 40px;
  display: grid;
  grid-template-columns: 272px minmax(0, 1fr);
  gap: 18px;
}
.sidebar {
  position: sticky;
  top: 18px;
  height: fit-content;
  display: grid;
  gap: 14px;
}
.brand,
.sidebar-card,
.hero,
.metric,
.panel,
.page-card,
.detail-card,
.callout {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: linear-gradient(180deg, rgba(15, 27, 45, 0.98), rgba(11, 20, 35, 0.98));
  box-shadow: var(--shadow);
}
.brand {
  padding: 18px;
}
.brand p,
.sidebar-card p,
.metric p,
.eyebrow,
.summary-grid span,
.detail-meta,
.trend-note,
.mini-stats span,
.toolbar span,
.empty,
.empty-inline {
  color: var(--muted);
}
.brand p,
.sidebar-card p,
.metric p {
  margin: 0 0 8px;
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}
.brand h1 {
  margin: 0;
  font-size: 28px;
  line-height: 1;
}
.brand span {
  display: block;
  margin-top: 10px;
  color: #c8d4e4;
}
.nav {
  display: grid;
  gap: 8px;
}
.nav-link {
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(15, 27, 45, 0.92);
  display: grid;
  gap: 4px;
}
.nav-link strong {
  color: var(--text);
  font-size: 14px;
}
.nav-link span {
  color: var(--muted);
  font-size: 12px;
}
.nav-link-current {
  border-color: var(--line-strong);
  background: rgba(17, 33, 56, 0.98);
}
.sidebar-card {
  padding: 14px 16px;
}
.sidebar-card strong {
  display: block;
  font-size: 15px;
}
.page {
  min-width: 0;
}
.hero {
  padding: 28px;
  margin-bottom: 14px;
}
.eyebrow {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}
.hero h2 {
  margin: 10px 0 8px;
  font-size: 40px;
  line-height: 1.05;
}
.hero p {
  margin: 0;
  max-width: 780px;
  font-size: 16px;
}
.hero-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.metric {
  padding: 16px;
  border-top: 3px solid var(--blue);
}
.metric-green { border-top-color: var(--green); }
.metric-amber { border-top-color: var(--amber); }
.metric-red { border-top-color: var(--red); }
.metric strong {
  display: block;
  font-size: 28px;
  line-height: 1.1;
  overflow-wrap: anywhere;
}
.metric span {
  display: block;
  margin-top: 8px;
  font-size: 12px;
}
.page-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.page-grid-wide {
  grid-template-columns: 1fr;
}
.panel {
  padding: 18px;
  min-width: 0;
}
.panel-wide {
  grid-column: 1 / -1;
}
.panel h2,
.page-card h3,
.detail-card h3,
.trend-block h3 {
  margin: 0 0 14px;
  font-size: 20px;
  line-height: 1.15;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}
.summary-grid div {
  border-left: 2px solid var(--line);
  padding-left: 10px;
}
.summary-grid strong {
  display: block;
  margin-top: 5px;
  font-size: 18px;
  overflow-wrap: anywhere;
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
.page-card:hover {
  text-decoration: none;
  border-color: var(--line-strong);
}
.page-card-head,
.detail-card-head {
  display: flex;
  align-items: start;
  justify-content: space-between;
  gap: 10px;
}
.page-card-head span {
  color: var(--blue);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}
.mini-stats,
.detail-stats {
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
  border-top: 1px solid var(--line);
  padding-top: 10px;
}
.mini-stats strong,
.detail-stats dd {
  display: block;
  margin: 4px 0 0;
  font-size: 16px;
}
.detail-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.detail-stats dt,
.detail-card h4 {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}
.detail-card h4 {
  margin: 14px 0 8px;
}
.detail-card ul,
.bullet-list {
  margin: 0;
  padding-left: 18px;
}
.toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 12px;
}
.search {
  display: grid;
  gap: 6px;
  min-width: min(320px, 100%);
}
.search input {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #0b1728;
  color: var(--text);
  padding: 10px 12px;
  font: inherit;
}
.callout {
  padding: 14px 16px;
  margin-bottom: 16px;
  border-left: 3px solid var(--blue);
}
.callout-green { border-left-color: var(--green); }
.callout-amber { border-left-color: var(--amber); }
.callout strong {
  display: block;
  margin-bottom: 6px;
  font-size: 15px;
}
.callout p {
  margin: 0;
}
.trend-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}
.trend-block {
  min-width: 0;
}
.series-list {
  display: grid;
  gap: 10px;
}
.series-row {
  border-top: 1px solid var(--line);
  padding-top: 10px;
}
.series-row strong {
  display: inline-block;
  margin-left: 8px;
}
.trend-note {
  margin-top: 10px;
  font-size: 12px;
}
.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  display: inline-block;
}
.sparkline {
  width: 100%;
  height: 60px;
  display: block;
  margin-top: 10px;
}
.table-wrap,
.heatmap-wrap {
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 8px;
}
table {
  width: 100%;
  border-collapse: collapse;
  min-width: 720px;
}
th, td {
  padding: 11px 12px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
th {
  background: #101f33;
  color: #c6d2e2;
  font-size: 12px;
  text-transform: uppercase;
}
tr:last-child td,
tr:last-child th {
  border-bottom: 0;
}
.chip-list {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.chip {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 2px 8px;
  background: #15243a;
  font-size: 12px;
}
.chip-good { color: #bbf7d0; border-color: #166534; background: #052e1a; }
.chip-warn { color: #fde68a; border-color: #92400e; background: #422006; }
.chip-bad { color: #fecaca; border-color: #991b1b; background: #450a0a; }
.count { color: var(--muted); margin-right: 8px; }
.heatmap-table {
  min-width: 980px;
}
.heatmap-table .sticky-col {
  position: sticky;
  left: 0;
  z-index: 2;
  background: #0d1b2d;
  min-width: 260px;
}
.heatmap-table .secondary-col {
  left: 260px;
  min-width: 74px;
}
.heat-cell {
  min-width: 42px;
  text-align: center;
  color: #9fb0c4;
}
.heat-cell-hit {
  background: rgba(56, 189, 248, var(--heat-alpha));
  color: #f7fbff;
}
.empty {
  margin: 0;
  padding: 14px 0 0;
}
.empty-inline {
  font-size: 13px;
}
[hidden] { display: none !important; }
@media (max-width: 1180px) {
  .site-shell {
    grid-template-columns: 1fr;
  }
  .sidebar {
    position: static;
  }
}
@media (max-width: 900px) {
  .hero h2 { font-size: 32px; }
  .hero-metrics,
  .page-grid,
  .card-grid,
  .summary-grid,
  .trend-grid,
  .mini-stats,
  .detail-stats {
    grid-template-columns: 1fr;
  }
  .panel-wide {
    grid-column: auto;
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
        "index.html": _render_overview(dashboard),
        "daily.html": _render_daily(dashboard),
        "flaky.html": _render_flaky(dashboard),
        "review.html": _render_review(dashboard),
        "acceptance.html": _render_acceptance(dashboard),
        "fuzzer.html": _render_fuzzer(dashboard),
        "ai.html": _render_ai(dashboard),
        "ops.html": _render_ops(dashboard),
    }

    assets_dir = site_dir / "assets"
    data_dir = site_dir / "data"
    assets_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / "site.css").write_text(_site_css(), encoding="utf-8")
    (assets_dir / "site.js").write_text(_site_js(), encoding="utf-8")
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

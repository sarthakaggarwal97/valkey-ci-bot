"""One-click demo bundle generator for the Valkey CI agent.

This module exists for the very practical "show, don't tell" problem:
maintainers should be able to run one workflow in this repo and get a polished
packet that points at the most convincing proof surfaces:

- the public observability site
- a fresh replay lab run
- a fresh dashboard refresh
- optional Daily and Fuzzer monitor probes
- an optional live fork PR review run
- the latest recorded Daily proof example from bot state

The result is intentionally static and shareable. It turns live workflow runs
into a demo narrative instead of making humans collect links by hand.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _site_css() -> str:
    """Return the concatenated dashboard CSS for self-contained demo pages.

    The demo bundle is a single standalone HTML file, so we inline the
    checked-in stylesheets from dashboard-app/assets/css/ at render time.
    This keeps the demo visually consistent with the live dashboard.
    """
    css_dir = Path(__file__).resolve().parent.parent / "dashboard-app" / "assets" / "css"
    files = ["tokens.css", "base.css", "components.css"]
    chunks = []
    for name in files:
        path = css_dir / name
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


logger = logging.getLogger(__name__)


@dataclass
class DemoWorkflowSpec:
    """One child workflow that participates in the demo bundle."""

    key: str
    name: str
    workflow_file: str
    purpose: str
    inputs: dict[str, str]


@dataclass
class DemoWorkflowRun:
    """Tracked result for one dispatched demo workflow."""

    key: str
    name: str
    workflow_file: str
    purpose: str
    inputs: dict[str, str]
    run_id: int | None = None
    html_url: str = ""
    status: str = ""
    conclusion: str = ""
    created_at: str = ""
    error: str = ""


@dataclass
class FeaturedProof:
    """Best recent proof example to feature in the demo packet."""

    fingerprint: str
    failure_identifier: str
    job_name: str
    branch: str
    proof_status: str
    proof_summary: str
    proof_url: str
    pr_url: str
    updated_at: str


def _str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _bool_text(value: bool) -> str:
    return "Yes" if value else "No"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    text = value.strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _pages_url(repo_full_name: str) -> str:
    owner, repo = repo_full_name.split("/", 1)
    return f"https://{owner}.github.io/{repo}/"


def _github_request_json(
    url: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib_request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "valkey-ci-agent-demo",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
            if not body.strip():
                return {}
            return json.loads(body)
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"github-api-error {method} {url}: {exc.code} {detail}") from exc
    except OSError as exc:
        raise RuntimeError(f"github-api-transport-error {method} {url}: {exc}") from exc


def _dispatch_workflow(
    *,
    repo_full_name: str,
    workflow_file: str,
    ref: str,
    token: str,
    inputs: dict[str, str],
) -> None:
    url = (
        f"https://api.github.com/repos/{repo_full_name}/actions/workflows/"
        f"{workflow_file}/dispatches"
    )
    _github_request_json(
        url,
        token,
        method="POST",
        payload={"ref": ref, "inputs": inputs},
    )


def _list_workflow_runs(
    repo_full_name: str,
    workflow_file: str,
    token: str,
    *,
    branch: str,
) -> list[dict[str, Any]]:
    query = urllib_parse.urlencode(
        {
            "event": "workflow_dispatch",
            "branch": branch,
            "per_page": "20",
        }
    )
    url = (
        f"https://api.github.com/repos/{repo_full_name}/actions/workflows/"
        f"{workflow_file}/runs?{query}"
    )
    payload = _github_request_json(url, token)
    workflow_runs = payload.get("workflow_runs", [])
    return workflow_runs if isinstance(workflow_runs, list) else []


def _get_workflow_run(
    repo_full_name: str,
    run_id: int,
    token: str,
) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{repo_full_name}/actions/runs/{run_id}"
    return _github_request_json(url, token)


def _find_dispatched_run(
    *,
    repo_full_name: str,
    workflow_file: str,
    token: str,
    branch: str,
    started_after: datetime,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(5, timeout_seconds)
    grace = timedelta(seconds=15)
    while time.monotonic() < deadline:
        for run in _list_workflow_runs(repo_full_name, workflow_file, token, branch=branch):
            created_at = _parse_timestamp(_str(run.get("created_at")))
            if created_at + grace < started_after:
                continue
            if _str(run.get("event")) != "workflow_dispatch":
                continue
            if _str(run.get("head_branch")) != branch:
                continue
            return run
        time.sleep(5)
    raise RuntimeError(
        f"Timed out waiting for workflow run for {workflow_file} on {repo_full_name}@{branch}."
    )


def _dispatch_and_track(
    *,
    spec: DemoWorkflowSpec,
    repo_full_name: str,
    token: str,
    branch: str,
) -> DemoWorkflowRun:
    logger.info("Dispatching %s (%s).", spec.name, spec.workflow_file)
    started = _utc_now()
    run = DemoWorkflowRun(
        key=spec.key,
        name=spec.name,
        workflow_file=spec.workflow_file,
        purpose=spec.purpose,
        inputs=spec.inputs,
    )
    try:
        _dispatch_workflow(
            repo_full_name=repo_full_name,
            workflow_file=spec.workflow_file,
            ref=branch,
            token=token,
            inputs=spec.inputs,
        )
        payload = _find_dispatched_run(
            repo_full_name=repo_full_name,
            workflow_file=spec.workflow_file,
            token=token,
            branch=branch,
            started_after=started,
        )
        run.run_id = int(payload.get("id") or 0) or None
        run.html_url = _str(payload.get("html_url"))
        run.status = _str(payload.get("status"), "queued")
        run.conclusion = _str(payload.get("conclusion"))
        run.created_at = _str(payload.get("created_at"))
    except Exception as exc:  # noqa: BLE001
        run.status = "dispatch-failed"
        run.conclusion = "failure"
        run.error = str(exc)
        logger.warning("Failed to dispatch %s: %s", spec.workflow_file, exc)
    return run


def _wait_for_runs(
    *,
    repo_full_name: str,
    token: str,
    runs: list[DemoWorkflowRun],
    timeout_seconds: int,
) -> None:
    deadline = time.monotonic() + max(30, timeout_seconds)
    pending = [run for run in runs if run.run_id is not None]
    while pending and time.monotonic() < deadline:
        still_pending: list[DemoWorkflowRun] = []
        for run in pending:
            payload = _get_workflow_run(repo_full_name, run.run_id or 0, token)
            run.status = _str(payload.get("status"), run.status)
            run.conclusion = _str(payload.get("conclusion"), run.conclusion)
            run.html_url = _str(payload.get("html_url"), run.html_url)
            if run.status != "completed":
                still_pending.append(run)
        pending = still_pending
        if pending:
            time.sleep(15)
    for run in pending:
        run.status = "timed-out"
        if not run.conclusion:
            run.conclusion = "timed_out"
        if not run.error:
            run.error = "Timed out waiting for child workflow to finish."


def _load_featured_proof(path: Path) -> FeaturedProof | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    campaigns = payload.get("campaigns", {})
    entries = payload.get("entries", {})
    if not isinstance(campaigns, dict) or not isinstance(entries, dict):
        return None

    best: FeaturedProof | None = None
    best_score = (-1, datetime.min.replace(tzinfo=timezone.utc))
    for fingerprint, raw_campaign in campaigns.items():
        if not isinstance(raw_campaign, dict):
            continue
        proof_status = _str(raw_campaign.get("proof_status")).strip()
        proof_url = _str(raw_campaign.get("proof_url")).strip()
        if not proof_status or not proof_url:
            continue
        raw_entry = entries.get(fingerprint, {})
        pr_url = _str(raw_entry.get("pr_url")).strip() if isinstance(raw_entry, dict) else ""
        if not pr_url:
            continue
        updated_at = _str(
            raw_campaign.get("proof_updated_at") or raw_campaign.get("updated_at")
        )
        updated_ts = _parse_timestamp(updated_at)
        score = 1 if proof_status == "passed" else 0
        if (score, updated_ts) <= best_score:
            continue
        best = FeaturedProof(
            fingerprint=_str(fingerprint),
            failure_identifier=_str(raw_campaign.get("failure_identifier")),
            job_name=_str(raw_campaign.get("job_name")),
            branch=_str(raw_campaign.get("branch"), "unstable"),
            proof_status=proof_status,
            proof_summary=_str(raw_campaign.get("proof_summary")),
            proof_url=proof_url,
            pr_url=pr_url,
            updated_at=updated_at,
        )
        best_score = (score, updated_ts)
    return best


def _section_link(label: str, href: str) -> str:
    return f"[{label}]({href})"


def _chip_class(value: str) -> str:
    normalized = value.lower()
    if any(word in normalized for word in ("success", "pass", "ready")):
        return "chip-good"
    if any(word in normalized for word in ("fail", "error", "timed")):
        return "chip-bad"
    return "chip-warn"


def _html_link(label: str, href: str) -> str:
    if not href:
        return html.escape(label)
    return f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'


def _render_markdown(
    *,
    repo_full_name: str,
    pages_url: str,
    runs: list[DemoWorkflowRun],
    featured_proof: FeaturedProof | None,
    review_pr_url: str,
) -> str:
    statuses = [run for run in runs if run.status]
    successes = sum(1 for run in statuses if run.conclusion == "success")
    failures = sum(1 for run in statuses if run.conclusion not in {"", "success", "skipped"})
    lines = [
        "# Valkey CI Agent Demo Bundle",
        "",
        "This packet turns the repo into a ready-to-present demo: open the site, "
        "show the proof workflows, and use the live links below instead of hunting around GitHub.",
        "",
        "## Start Here",
        "",
        f"- Control room: {_section_link('GitHub Pages site', pages_url)}",
        f"- Daily view: {_section_link('Daily', pages_url + 'daily.html')}",
        f"- PR view: {_section_link('PRs', pages_url + 'review.html')}",
        f"- Fuzzer watch: {_section_link('Fuzzer', pages_url + 'fuzzer.html')}",
        f"- Operations: {_section_link('Ops', pages_url + 'ops.html')}",
        "",
        "## Demo Health",
        "",
        f"- Successful child runs: **{successes}/{len(statuses)}**",
        f"- Child runs needing follow-up: **{failures}**",
        "",
        "## Workflow Runs",
        "",
        "| Workflow | Purpose | Status | Run |",
        "| --- | --- | --- | --- |",
    ]
    for run in runs:
        status = run.conclusion or run.status or "unknown"
        link = _section_link("Open", run.html_url) if run.html_url else "N/A"
        if run.error:
            status = f"{status} ({run.error})"
        lines.append(f"| {run.name} | {run.purpose} | `{status}` | {link} |")

    if featured_proof:
        lines.extend(
            [
                "",
                "## Featured Daily Proof Example",
                "",
                f"- Failure: `{featured_proof.failure_identifier}`",
                f"- Job: `{featured_proof.job_name}` on `{featured_proof.branch}`",
                f"- Proof: `{featured_proof.proof_status}`",
                f"- Draft/PR: {_section_link('PR', featured_proof.pr_url)}",
                f"- Proof run: {_section_link('Workflow run', featured_proof.proof_url)}",
            ]
        )
        if featured_proof.proof_summary:
            lines.append(f"- Summary: {featured_proof.proof_summary}")

    if review_pr_url:
        lines.extend(
            [
                "",
                "## Live Review Example",
                "",
                f"- Target PR: {_section_link('Open reviewed PR', review_pr_url)}",
                "- Use this alongside the PRs page on GitHub Pages to show how the reviewer "
                "posts findings and policy notes without checking out untrusted PR head code.",
            ]
        )

    lines.extend(
        [
            "",
            "## Suggested Walkthrough",
            "",
            "1. Open the Control room page and orient everyone to the system-level view.",
            "2. Jump to PRs to show replay proof and tracked review state together.",
            "3. Open the Daily proof example so people see a real bot-created fix loop.",
            "4. Show the live review run or reviewed fork PR if one was included.",
            "5. Finish on Fuzzer or Ops depending on whether the audience cares more about anomalies or agent state.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_html(
    *,
    repo_full_name: str,
    pages_url: str,
    runs: list[DemoWorkflowRun],
    featured_proof: FeaturedProof | None,
    review_pr_url: str,
) -> str:
    statuses = [run for run in runs if run.status]
    successes = sum(1 for run in statuses if run.conclusion == "success")
    failures = sum(1 for run in statuses if run.conclusion not in {"", "success", "skipped"})

    run_cards = []
    for run in runs:
        status = run.conclusion or run.status or "unknown"
        note = html.escape(run.error) if run.error else html.escape(run.purpose)
        run_cards.append(
            '<article class="detail-card"><div class="detail-card-head">'
            f"<h3>{html.escape(run.name)}</h3>"
            f'<span class="chip {_chip_class(status)}">{html.escape(status)}</span>'
            "</div>"
            f"<p>{html.escape(run.purpose)}</p>"
            '<div class="detail-stats">'
            f"<div><dt>Workflow</dt><dd>{html.escape(run.workflow_file)}</dd></div>"
            f"<div><dt>Run</dt><dd>{_html_link('Open', run.html_url)}</dd></div>"
            f"<div><dt>Detail</dt><dd>{note}</dd></div>"
            "</div></article>"
        )

    proof_html = '<p class="empty">No proof campaign has been recorded yet on the checked-out bot-data snapshot.</p>'
    if featured_proof:
        proof_html = (
            '<article class="detail-card"><div class="detail-card-head">'
            "<h3>Latest proofed Daily fix</h3>"
            f'<span class="chip {_chip_class(featured_proof.proof_status)}">{html.escape(featured_proof.proof_status)}</span>'
            "</div>"
            f"<p>{html.escape(featured_proof.failure_identifier)}</p>"
            '<div class="detail-stats">'
            f"<div><dt>Job</dt><dd>{html.escape(featured_proof.job_name)}</dd></div>"
            f"<div><dt>Branch</dt><dd>{html.escape(featured_proof.branch)}</dd></div>"
            f"<div><dt>Updated</dt><dd>{html.escape(featured_proof.updated_at or 'recently')}</dd></div>"
            "</div>"
            f"<h4>Links</h4><ul class=\"bullet-list\"><li>{_html_link('Draft or proofed PR', featured_proof.pr_url)}</li>"
            f"<li>{_html_link('Proof workflow run', featured_proof.proof_url)}</li></ul>"
            f"<h4>Summary</h4><p>{html.escape(featured_proof.proof_summary or 'Proof summary not captured.')}</p>"
            "</article>"
        )

    review_html = '<p class="empty">No live review target was included in this bundle.</p>'
    if review_pr_url:
        review_html = (
            '<article class="detail-card"><div class="detail-card-head">'
            "<h3>Live review target</h3>"
            '<span class="chip chip-good">ready</span>'
            "</div>"
            "<p>This is the PR to open when you want to show the reviewer in a real fork-safe setting.</p>"
            f'<ul class="bullet-list"><li>{_html_link("Reviewed PR", review_pr_url)}</li>'
            f'<li>{_html_link("PRs page on the site", pages_url + "review.html")}</li></ul>'
            "</article>"
        )

    page_cards = "".join(
        [
            '<a class="page-card" href="'
            + html.escape(pages_url + suffix, quote=True)
            + '"><div class="page-card-head"><h3>'
            + html.escape(title)
            + '</h3><span>Open</span></div><p>'
            + html.escape(body)
            + "</p></a>"
            for suffix, title, body in [
                ("", "Control room", "The cleanest first screen for maintainers."),
                ("daily.html", "Daily", "Failure health and proof-ready campaign context."),
                ("review.html", "PRs", "Tracked review state plus replay proof on one page."),
                ("fuzzer.html", "Fuzzer", "Anomalies, possible core bugs, and issue routing."),
                ("ops.html", "Ops", "State coverage, event stream, and AI reliability."),
            ]
        ]
    )

    walkthrough = (
        "<ol class=\"bullet-list\">"
        "<li>Open the Control room page and show the multi-workflow view.</li>"
        "<li>Jump to PRs to prove the bot earns trust against replayed cases.</li>"
        "<li>Use the featured proof example to show a Daily failure closing into a PR and proof run.</li>"
        "<li>Open the live review target if you included one.</li>"
        "<li>Finish on Fuzzer or Ops depending on whether the audience cares more about triage or operational state.</li>"
        "</ol>"
    )

    extra_css = """
.chip { display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 8px; padding: 2px 8px; background: #15243a; font-size: 12px; }
.chip-good { color: #bbf7d0; border-color: #166534; background: #052e1a; }
.chip-warn { color: #fde68a; border-color: #92400e; background: #422006; }
.chip-bad { color: #fecaca; border-color: #991b1b; background: #450a0a; }
.stack { display: grid; gap: 14px; }
ol.bullet-list { padding-left: 20px; }
"""
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Valkey CI Agent Demo</title>"
        f"<style>{_site_css()}{extra_css}</style></head><body>"
        '<div class="site-shell"><aside class="sidebar">'
        '<section class="brand"><p>Demo bundle</p><h1>Valkey CI Agent</h1>'
        "<span>One run, one packet, no tab spelunking.</span></section>"
        '<section class="sidebar-card"><p>Start here</p><strong>'
        f'{_html_link("Open GitHub Pages site", pages_url)}</strong></section>'
        '<section class="sidebar-card"><p>Repo</p><strong>'
        f"{html.escape(repo_full_name)}</strong></section></aside>"
        '<main class="page"><section class="hero"><div class="eyebrow">Demo Day</div>'
        "<h2>Everything worth showing in one place.</h2>"
        "<p>The packet below stitches together fresh workflow runs, the public observability site, "
        "and the latest proofed Daily example so the demo feels like a product, not a scavenger hunt.</p>"
        '<div class="hero-metrics">'
        f'<article class="metric metric-green"><p>Successful runs</p><strong>{successes}</strong><span>Fresh child workflows completed cleanly</span></article>'
        f'<article class="metric metric-amber"><p>Needs follow-up</p><strong>{failures}</strong><span>Child workflows that need explanation or a rerun</span></article>'
        f'<article class="metric"><p>Pages</p><strong>{_html_link("Open site", pages_url)}</strong><span>Live public surface</span></article>'
        f'<article class="metric"><p>Live review</p><strong>{html.escape(_bool_text(bool(review_pr_url)))}</strong><span>Fork PR included in this bundle</span></article>'
        "</div></section>"
        '<section class="page-grid">'
        + _panel("Start here", '<div class="card-grid">' + page_cards + "</div>")
        + _panel("Workflow runs", '<div class="card-grid">' + "".join(run_cards) + "</div>", wide=True)
        + _panel("Featured Daily proof example", proof_html)
        + _panel("Live review example", review_html)
        + _panel("Walkthrough", walkthrough, wide=True)
        + "</section></main></div></body></html>"
    )


def _panel(title: str, body: str, *, wide: bool = False) -> str:
    classes = "panel panel-wide" if wide else "panel"
    return f'<section class="{classes}"><h2>{html.escape(title)}</h2>{body}</section>'


def _build_specs(args: argparse.Namespace) -> list[DemoWorkflowSpec]:
    specs: list[DemoWorkflowSpec] = []
    if args.run_dashboard:
        specs.append(
            DemoWorkflowSpec(
                key="dashboard",
                name="Capability Dashboard",
                workflow_file="agent-dashboard.yml",
                purpose="Refresh the public control room and static observability site.",
                inputs={},
            )
        )
    if args.run_replay:
        specs.append(
            DemoWorkflowSpec(
                key="replay",
                name="Replay Lab",
                workflow_file="agent-replay-lab.yml",
                purpose="Replay real Valkey-shaped cases before asking anyone to trust the bot.",
                inputs={
                    "manifest": args.replay_manifest,
                    "run_models": "true" if args.replay_run_models else "false",
                },
            )
        )
    if args.run_daily:
        specs.append(
            DemoWorkflowSpec(
                key="daily",
                name="Daily Monitor",
                workflow_file="monitor-valkey-daily.yml",
                purpose="Show the Daily loop scanning Valkey failures in a safe demo mode.",
                inputs={
                    "dry_run": "true" if args.daily_dry_run else "false",
                    "max_runs": str(args.daily_max_runs),
                    "verbose": "false",
                },
            )
        )
    if args.run_fuzzer:
        specs.append(
            DemoWorkflowSpec(
                key="fuzzer",
                name="Fuzzer Monitor",
                workflow_file="monitor-valkey-fuzzer.yml",
                purpose="Show anomaly triage and bug-routing signals from the fuzzer loop.",
                inputs={
                    "dry_run": "true" if args.fuzzer_dry_run else "false",
                    "max_runs": str(args.fuzzer_max_runs),
                    "verbose": "false",
                },
            )
        )
    if args.review_target_repo and args.review_pr_number:
        specs.append(
            DemoWorkflowSpec(
                key="review",
                name="External PR Review",
                workflow_file="review-external-pr.yml",
                purpose="Run a live fork-safe review against a chosen PR without touching upstream.",
                inputs={
                    "target_repo": args.review_target_repo,
                    "pr_number": str(args.review_pr_number),
                    "config_path": args.review_config_path,
                    "aws_region": args.aws_region,
                },
            )
        )
    return specs


def run_demo(args: argparse.Namespace) -> dict[str, Any]:
    pages_url = args.pages_url or _pages_url(args.repo)
    specs = _build_specs(args)
    runs = [
        _dispatch_and_track(
            spec=spec,
            repo_full_name=args.repo,
            token=args.token,
            branch=args.ref,
        )
        for spec in specs
    ]
    _wait_for_runs(
        repo_full_name=args.repo,
        token=args.token,
        runs=runs,
        timeout_seconds=args.wait_timeout_seconds,
    )

    publish_run: DemoWorkflowRun | None = None
    if args.publish_site:
        publish_run = _dispatch_and_track(
            spec=DemoWorkflowSpec(
                key="publish",
                name="Publish Dashboard Site",
                workflow_file="publish-dashboard-site.yml",
                purpose="Force a fresh GitHub Pages publish so the demo and public site stay in sync.",
                inputs={},
            ),
            repo_full_name=args.repo,
            token=args.token,
            branch=args.ref,
        )
        _wait_for_runs(
            repo_full_name=args.repo,
            token=args.token,
            runs=[publish_run],
            timeout_seconds=min(args.wait_timeout_seconds, 1800),
        )
        runs.append(publish_run)

    failure_store_path = Path(args.failure_store) if args.failure_store else None
    featured_proof = _load_featured_proof(failure_store_path) if failure_store_path else None
    review_pr_url = ""
    if args.review_target_repo and args.review_pr_number:
        review_pr_url = (
            f"https://github.com/{args.review_target_repo}/pull/{args.review_pr_number}"
        )

    report = {
        "generated_at": _utc_now().isoformat(),
        "repo": args.repo,
        "pages_url": pages_url,
        "review_pr_url": review_pr_url,
        "featured_proof": asdict(featured_proof) if featured_proof else None,
        "workflows": [asdict(run) for run in runs],
    }
    Path(args.output_json).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    Path(args.output_markdown).write_text(
        _render_markdown(
            repo_full_name=args.repo,
            pages_url=pages_url,
            runs=runs,
            featured_proof=featured_proof,
            review_pr_url=review_pr_url,
        ),
        encoding="utf-8",
    )
    html_output = _render_html(
        repo_full_name=args.repo,
        pages_url=pages_url,
        runs=runs,
        featured_proof=featured_proof,
        review_pr_url=review_pr_url,
    )
    Path(args.output_html).write_text(html_output, encoding="utf-8")

    site_dir = Path(args.site_dir)
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "index.html").write_text(html_output, encoding="utf-8")
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Agent repository in owner/repo form.")
    parser.add_argument("--token", required=True, help="GitHub token for dispatching workflows.")
    parser.add_argument("--ref", default="main", help="Branch or ref to dispatch against.")
    parser.add_argument("--pages-url", default="", help="Public GitHub Pages site URL.")
    parser.add_argument("--failure-store", default="", help="Optional local failure-store.json path.")
    parser.add_argument("--aws-region", default="us-east-1", help="AWS region for review workflow inputs.")
    parser.add_argument("--review-target-repo", default="", help="Optional owner/repo for live review.")
    parser.add_argument("--review-pr-number", type=int, default=0, help="Optional PR number for live review.")
    parser.add_argument("--review-config-path", default=".github/pr-review-bot.yml", help="Config path for external reviews.")
    parser.add_argument("--replay-manifest", default="examples/valkey-acceptance.yml", help="Replay lab manifest path.")
    parser.add_argument("--replay-run-models", action="store_true", help="Run Bedrock model passes in the replay lab.")
    parser.add_argument("--run-dashboard", action="store_true", help="Dispatch the dashboard refresh workflow.")
    parser.add_argument("--run-replay", action="store_true", help="Dispatch the replay lab workflow.")
    parser.add_argument("--run-daily", action="store_true", help="Dispatch the Daily monitor workflow.")
    parser.add_argument("--daily-dry-run", action="store_true", help="Run the Daily monitor in dry-run mode.")
    parser.add_argument("--daily-max-runs", type=int, default=3, help="Maximum Daily runs to inspect in the demo.")
    parser.add_argument("--run-fuzzer", action="store_true", help="Dispatch the Fuzzer monitor workflow.")
    parser.add_argument("--fuzzer-dry-run", action="store_true", help="Run the Fuzzer monitor in dry-run mode.")
    parser.add_argument("--fuzzer-max-runs", type=int, default=1, help="Maximum fuzzer runs to inspect in the demo.")
    parser.add_argument("--publish-site", action="store_true", help="Force a fresh GitHub Pages publish after the demo runs.")
    parser.add_argument("--wait-timeout-seconds", type=int, default=7200, help="Maximum time to wait for child workflow completion.")
    parser.add_argument("--output-markdown", default="demo-report.md", help="Markdown report output path.")
    parser.add_argument("--output-json", default="demo-report.json", help="JSON report output path.")
    parser.add_argument("--output-html", default="demo-report.html", help="HTML report output path.")
    parser.add_argument("--site-dir", default="demo-site", help="Output directory for the shareable demo site.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_demo(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

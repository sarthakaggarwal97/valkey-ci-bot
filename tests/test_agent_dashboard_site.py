"""Tests for the multi-page observability site generator."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.agent_dashboard import build_dashboard
from scripts.agent_dashboard_site import build_site, main
from tests.test_agent_dashboard import (
    _acceptance_payload,
    _failure_store,
    _fuzzer_result,
    _trend_events,
)


def _dashboard_payload() -> dict:
    return build_dashboard(
        failure_store=_failure_store(),
        rate_state={
            "queued_failures": ["fp-queued"],
            "token_usage": 18_500,
            "token_window_start": "2026-04-08T00:00:00+00:00",
            "ai_metrics": {"bedrock.invoke_schema.calls": 4},
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
        acceptance_payloads=[_acceptance_payload()],
        fuzzer_results=[_fuzzer_result()],
        events=_trend_events(),
        daily_health_data={
            "repo": "valkey-io/valkey",
            "workflow": "daily.yml",
            "branch": "unstable",
            "dates": ["2026-04-06", "2026-04-07", "2026-04-08"],
            "total_runs": 3,
            "failed_runs": 2,
            "unique_failures": 2,
            "heatmap": [
                {
                    "name": "jemalloc / sanitize",
                    "days_failed": 2,
                    "total_days": 3,
                    "cells": [
                        {"date": "2026-04-06", "count": 1},
                        {"date": "2026-04-07", "count": 0},
                        {"date": "2026-04-08", "count": 1},
                    ],
                }
            ],
            "runs": [
                {
                    "date": "2026-04-08",
                    "status": "failure",
                    "commit_sha": "abcd123",
                    "full_sha": "abcd1234ef567890",
                    "unique_failures": 1,
                    "failed_jobs": 2,
                    "run_url": "https://github.com/valkey-io/valkey/actions/runs/1",
                }
            ],
        },
        generated_at="2026-04-08T03:00:00+00:00",
    )


def test_build_site_writes_multi_page_observability_site(tmp_path: Path) -> None:
    site_dir = tmp_path / "dashboard-site"

    build_site(_dashboard_payload(), site_dir)

    assert (site_dir / "index.html").exists()
    assert (site_dir / "daily.html").exists()
    assert (site_dir / "flaky.html").exists()
    assert (site_dir / "review.html").exists()
    assert (site_dir / "acceptance.html").exists()
    assert (site_dir / "assets" / "site.css").exists()
    assert (site_dir / "assets" / "site.js").exists()
    assert (site_dir / "assets" / "valkey-horizontal.svg").exists()
    assert (site_dir / "data" / "dashboard.json").exists()

    index_html = (site_dir / "index.html").read_text(encoding="utf-8")
    daily_html = (site_dir / "daily.html").read_text(encoding="utf-8")
    review_html = (site_dir / "review.html").read_text(encoding="utf-8")
    acceptance_html = (site_dir / "acceptance.html").read_text(encoding="utf-8")
    site_css = (site_dir / "assets" / "site.css").read_text(encoding="utf-8")

    assert "Operator Console" in index_html
    assert 'alt="Valkey logo"' in index_html
    assert "Open+Sans" in index_html
    assert "Data coverage" in index_html
    assert "Trend watch" in index_html
    assert "Failure heatmap" in daily_html
    assert "jemalloc / sanitize" in daily_html
    assert "--heat-alpha:1.00" in daily_html
    assert "https://github.com/valkey-io/valkey/commit/abcd1234ef567890" in daily_html
    assert "Replay review cases" in review_html
    assert "https://github.com/valkey-io/valkey/pull/1" in review_html
    assert "Replay proof moved into the PRs page." in acceptance_html
    assert "color-scheme: dark;" in site_css
    assert "--panel: #111c2d;" in site_css
    assert "#30176e" in site_css
    assert '"Open Sans"' in site_css
    assert "rgba(207, 60, 79, var(--heat-alpha))" in site_css


def test_cli_reads_dashboard_json_and_writes_site(tmp_path: Path) -> None:
    dashboard_json = tmp_path / "agent-dashboard.json"
    dashboard_json.write_text(
        json.dumps(_dashboard_payload(), indent=2),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--dashboard-json",
            str(dashboard_json),
            "--site-dir",
            str(tmp_path / "site"),
        ]
    )

    assert exit_code == 0
    assert (tmp_path / "site" / "ops.html").exists()

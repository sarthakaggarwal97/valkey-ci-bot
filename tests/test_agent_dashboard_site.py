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
    assert (site_dir / "diagnostics.html").exists()
    assert (site_dir / "flaky.html").exists()
    assert (site_dir / "review.html").exists()
    assert (site_dir / "acceptance.html").exists()
    assert (site_dir / "ops.html").exists()
    assert (site_dir / "assets" / "site.css").exists()
    assert (site_dir / "assets" / "site.js").exists()
    assert (site_dir / "assets" / "valkey-horizontal.svg").exists()
    assert (site_dir / "data" / "dashboard.json").exists()

    index_html = (site_dir / "index.html").read_text(encoding="utf-8")
    daily_html = (site_dir / "daily.html").read_text(encoding="utf-8")
    review_html = (site_dir / "review.html").read_text(encoding="utf-8")
    acceptance_html = (site_dir / "acceptance.html").read_text(encoding="utf-8")
    diagnostics_html = (site_dir / "diagnostics.html").read_text(encoding="utf-8")
    ops_html = (site_dir / "ops.html").read_text(encoding="utf-8")
    site_css = (site_dir / "assets" / "site.css").read_text(encoding="utf-8")

    assert "Operator Console" in index_html
    assert 'alt="Valkey logo"' in index_html
    assert "Open+Sans" in index_html
    assert 'href="index.html"' in index_html
    assert "Failure heatmap" in index_html
    assert "jemalloc / sanitize" in index_html
    assert "--heat-alpha:1.00" in index_html
    assert "https://github.com/valkey-io/valkey/commit/abcd1234ef567890" in index_html
    assert "Daily CI is now the homepage." in daily_html
    assert "Replay review cases" in review_html
    assert "https://github.com/valkey-io/valkey/pull/1" in review_html
    assert "Replay proof moved into the PRs page." in acceptance_html
    assert "Diagnostics" in diagnostics_html
    assert "Data coverage" in diagnostics_html
    assert "Diagnostics moved out of the main navigation." in ops_html
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
    assert (tmp_path / "site" / "diagnostics.html").exists()


def _multi_workflow_dashboard() -> dict:
    """Dashboard payload with both daily.yml and weekly.yml workflow_reports."""
    return build_dashboard(
        failure_store=_failure_store(),
        events=_trend_events(),
        daily_health_data={
            "repo": "valkey-io/valkey",
            "workflow": "daily.yml, weekly.yml",
            "branch": "unstable",
            "workflows": ["daily.yml", "weekly.yml"],
            "dates": ["2026-04-06", "2026-04-07", "2026-04-08"],
            "days_with_runs": 3,
            "total_runs": 5,
            "failed_runs": 3,
            "unique_failures": 3,
            "heatmap": [
                {
                    "name": "cluster slot coverage",
                    "days_failed": 2,
                    "total_days": 3,
                    "cells": [
                        {"date": "2026-04-06", "count": 1},
                        {"date": "2026-04-07", "count": 0},
                        {"date": "2026-04-08", "count": 1},
                    ],
                }
            ],
            "workflow_reports": [
                {
                    "workflow": "daily.yml",
                    "total_runs": 3,
                    "failed_runs": 2,
                    "unique_failures": 2,
                    "dates": ["2026-04-06", "2026-04-07", "2026-04-08"],
                    "heatmap": [
                        {
                            "name": "cluster slot coverage",
                            "days_failed": 2,
                            "total_days": 3,
                            "cells": [
                                {"date": "2026-04-06", "count": 1},
                                {"date": "2026-04-07", "count": 0},
                                {"date": "2026-04-08", "count": 1},
                            ],
                        }
                    ],
                },
                {
                    "workflow": "weekly.yml",
                    "total_runs": 2,
                    "failed_runs": 1,
                    "unique_failures": 1,
                    "dates": ["2026-04-06", "2026-04-07", "2026-04-08"],
                    "heatmap": [
                        {
                            "name": "memory defrag",
                            "days_failed": 1,
                            "total_days": 2,
                            "cells": [
                                {"date": "2026-04-06", "count": 0},
                                {"date": "2026-04-07", "count": 1},
                                {"date": "2026-04-08", "count": 0},
                            ],
                        }
                    ],
                },
            ],
            "runs": [
                {
                    "date": "2026-04-08",
                    "workflow": "daily.yml",
                    "status": "failure",
                    "commit_sha": "abcd123",
                    "full_sha": "abcd1234ef567890",
                    "unique_failures": 1,
                    "failed_jobs": 2,
                    "run_url": "https://github.com/valkey-io/valkey/actions/runs/1",
                },
                {
                    "date": "2026-04-07",
                    "workflow": "weekly.yml",
                    "status": "failure",
                    "commit_sha": "bcde234",
                    "full_sha": "bcde2345fa678901",
                    "unique_failures": 1,
                    "failed_jobs": 1,
                    "run_url": "https://github.com/valkey-io/valkey/actions/runs/2",
                },
            ],
        },
        generated_at="2026-04-08T03:00:00+00:00",
    )


def test_multi_workflow_metrics_show_per_workflow_failures(tmp_path: Path) -> None:
    site_dir = tmp_path / "dashboard-site"
    build_site(_multi_workflow_dashboard(), site_dir)

    index_html = (site_dir / "index.html").read_text(encoding="utf-8")

    # Per-workflow failure metrics should appear instead of generic "Run types"
    assert "Daily failures" in index_html
    assert "Weekly failures" in index_html
    # Should NOT show the old generic "Run types" metric
    assert "Run types" not in index_html


def test_multi_workflow_heatmap_shows_separate_blocks(tmp_path: Path) -> None:
    site_dir = tmp_path / "dashboard-site"
    build_site(_multi_workflow_dashboard(), site_dir)

    index_html = (site_dir / "index.html").read_text(encoding="utf-8")

    # Separate heatmap blocks per workflow
    assert "cluster slot coverage" in index_html
    assert "memory defrag" in index_html
    # Workflow labels in heatmap headers
    assert "Daily" in index_html
    assert "Weekly" in index_html


def test_heatmap_shows_missing_days_for_workflows_with_gaps(tmp_path: Path) -> None:
    """When a workflow has dates with no run data, the heatmap shows a Missing days pill."""
    site_dir = tmp_path / "dashboard-site"
    dashboard = build_dashboard(
        daily_health_data={
            "repo": "valkey-io/valkey",
            "workflow": "daily.yml, weekly.yml",
            "branch": "unstable",
            "workflows": ["daily.yml", "weekly.yml"],
            "dates": ["2026-04-06", "2026-04-07", "2026-04-08"],
            "days_with_runs": 3,
            "total_runs": 4,
            "failed_runs": 2,
            "unique_failures": 2,
            "heatmap": [
                {
                    "name": "timeout",
                    "days_failed": 1,
                    "total_days": 3,
                    "cells": [
                        {"date": "2026-04-06", "count": 1, "has_run": True},
                        {"date": "2026-04-07", "count": 0, "has_run": True},
                        {"date": "2026-04-08", "count": 0, "has_run": True},
                    ],
                }
            ],
            "workflow_reports": [
                {
                    "workflow": "daily.yml",
                    "total_runs": 3,
                    "failed_runs": 1,
                    "unique_failures": 1,
                    "dates": ["2026-04-06", "2026-04-07", "2026-04-08"],
                    "days_with_runs": 3,
                    "heatmap": [
                        {
                            "name": "timeout",
                            "days_failed": 1,
                            "total_days": 3,
                            "cells": [
                                {"date": "2026-04-06", "count": 1, "has_run": True},
                                {"date": "2026-04-07", "count": 0, "has_run": True},
                                {"date": "2026-04-08", "count": 0, "has_run": True},
                            ],
                        }
                    ],
                },
                {
                    "workflow": "weekly.yml",
                    "total_runs": 1,
                    "failed_runs": 1,
                    "unique_failures": 1,
                    "dates": ["2026-04-06", "2026-04-07", "2026-04-08"],
                    "days_with_runs": 1,
                    "missing_dates": ["2026-04-07", "2026-04-08"],
                    "heatmap": [
                        {
                            "name": "memory defrag",
                            "days_failed": 1,
                            "total_days": 1,
                            "cells": [
                                {"date": "2026-04-06", "count": 1, "has_run": True},
                                {"date": "2026-04-07", "count": 0, "has_run": False},
                                {"date": "2026-04-08", "count": 0, "has_run": False},
                            ],
                        }
                    ],
                },
            ],
            "runs": [],
        },
        generated_at="2026-04-08T03:00:00+00:00",
    )
    build_site(dashboard, site_dir)

    index_html = (site_dir / "index.html").read_text(encoding="utf-8")

    # Weekly has 2 missing dates — should show a "Missing days" pill
    assert "Missing days" in index_html
    # Daily has no missing dates — should NOT have a missing pill for it
    # (the pill only appears when missing > 0)
    # The heatmap cells for weekly missing dates should show dashes
    assert "heat-cell-missing" in index_html
    assert "no run data" in index_html


def test_multi_workflow_missing_dates_show_expected_workflows(tmp_path: Path) -> None:
    site_dir = tmp_path / "dashboard-site"
    dashboard = _multi_workflow_dashboard()
    build_site(dashboard, site_dir)

    index_html = (site_dir / "index.html").read_text(encoding="utf-8")

    # 2026-04-06 has no runs — should show "Daily" and "Weekly" labels
    # for the missing-date rows instead of a generic dash
    assert "no data" in index_html
    # The run table should contain workflow labels for dates with runs
    assert "Daily" in index_html
    assert "Weekly" in index_html


def test_single_workflow_shows_unique_failures_metric(tmp_path: Path) -> None:
    """When only one workflow exists, show 'Unique failures' instead of per-workflow breakdown."""
    site_dir = tmp_path / "dashboard-site"
    build_site(_dashboard_payload(), site_dir)

    index_html = (site_dir / "index.html").read_text(encoding="utf-8")

    assert "Unique failures" in index_html
    # Should NOT show per-workflow breakdown for single workflow
    assert "Daily failures" not in index_html

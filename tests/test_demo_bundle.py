"""Tests for the one-click demo bundle generator."""

from __future__ import annotations

import json
from argparse import Namespace

from scripts.demo_bundle import (
    DemoWorkflowRun,
    FeaturedProof,
    _load_featured_proof,
    run_demo,
)


def test_load_featured_proof_prefers_latest_passed_example(tmp_path) -> None:
    path = tmp_path / "failure-store.json"
    path.write_text(
        json.dumps(
            {
                "entries": {
                    "fp-old": {"pr_url": "https://github.com/o/r/pull/1"},
                    "fp-new": {"pr_url": "https://github.com/o/r/pull/2"},
                    "fp-failed": {"pr_url": "https://github.com/o/r/pull/3"},
                },
                "campaigns": {
                    "fp-old": {
                        "failure_identifier": "old-case",
                        "job_name": "daily / linux",
                        "branch": "unstable",
                        "proof_status": "passed",
                        "proof_summary": "Old proof.",
                        "proof_url": "https://github.com/o/r/actions/runs/11",
                        "proof_updated_at": "2026-04-07T00:00:00+00:00",
                    },
                    "fp-new": {
                        "failure_identifier": "new-case",
                        "job_name": "daily / macos",
                        "branch": "unstable",
                        "proof_status": "passed",
                        "proof_summary": "Fresh proof.",
                        "proof_url": "https://github.com/o/r/actions/runs/22",
                        "proof_updated_at": "2026-04-08T00:00:00+00:00",
                    },
                    "fp-failed": {
                        "failure_identifier": "failed-case",
                        "job_name": "daily / windows",
                        "branch": "unstable",
                        "proof_status": "failed",
                        "proof_summary": "Recent, but not ideal.",
                        "proof_url": "https://github.com/o/r/actions/runs/33",
                        "proof_updated_at": "2026-04-09T00:00:00+00:00",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    featured = _load_featured_proof(path)

    assert featured == FeaturedProof(
        fingerprint="fp-new",
        failure_identifier="new-case",
        job_name="daily / macos",
        branch="unstable",
        proof_status="passed",
        proof_summary="Fresh proof.",
        proof_url="https://github.com/o/r/actions/runs/22",
        pr_url="https://github.com/o/r/pull/2",
        updated_at="2026-04-08T00:00:00+00:00",
    )


def test_run_demo_writes_report_and_site(monkeypatch, tmp_path) -> None:
    dispatched_runs = [
        DemoWorkflowRun(
            key="dashboard",
            name="Capability Dashboard",
            workflow_file="agent-dashboard.yml",
            purpose="Refresh the site.",
            inputs={},
            run_id=101,
            html_url="https://github.com/o/r/actions/runs/101",
            status="completed",
            conclusion="success",
            created_at="2026-04-09T00:00:00+00:00",
        ),
        DemoWorkflowRun(
            key="replay",
            name="Replay Lab",
            workflow_file="agent-replay-lab.yml",
            purpose="Replay proof.",
            inputs={},
            run_id=102,
            html_url="https://github.com/o/r/actions/runs/102",
            status="completed",
            conclusion="success",
            created_at="2026-04-09T00:01:00+00:00",
        ),
    ]

    def fake_dispatch_and_track(*, spec, repo_full_name, token, branch):
        del repo_full_name, token, branch
        return next(run for run in dispatched_runs if run.key == spec.key)

    monkeypatch.setattr("scripts.demo_bundle._dispatch_and_track", fake_dispatch_and_track)
    monkeypatch.setattr("scripts.demo_bundle._wait_for_runs", lambda **_: None)
    monkeypatch.setattr("scripts.demo_bundle._load_featured_proof", lambda path: None)

    args = Namespace(
        repo="sarthakaggarwal97/valkey-ci-agent",
        token="token",
        ref="main",
        pages_url="https://sarthakaggarwal97.github.io/valkey-ci-agent/",
        failure_store="",
        aws_region="us-east-1",
        review_target_repo="",
        review_pr_number=0,
        review_config_path=".github/pr-review-bot.yml",
        replay_manifest="examples/valkey-acceptance.yml",
        replay_run_models=False,
        run_dashboard=True,
        run_replay=True,
        run_daily=False,
        daily_dry_run=True,
        daily_max_runs=3,
        run_fuzzer=False,
        fuzzer_dry_run=True,
        fuzzer_max_runs=1,
        publish_site=False,
        wait_timeout_seconds=10,
        output_markdown=str(tmp_path / "demo-report.md"),
        output_json=str(tmp_path / "demo-report.json"),
        output_html=str(tmp_path / "demo-report.html"),
        site_dir=str(tmp_path / "demo-site"),
    )

    report = run_demo(args)

    report_path = tmp_path / "demo-report.json"
    html_path = tmp_path / "demo-report.html"
    site_index = tmp_path / "demo-site" / "index.html"
    markdown = (tmp_path / "demo-report.md").read_text(encoding="utf-8")
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["pages_url"] == "https://sarthakaggarwal97.github.io/valkey-ci-agent/"
    assert payload["repo"] == "sarthakaggarwal97/valkey-ci-agent"
    assert len(payload["workflows"]) == 2
    assert "Replay Lab" in markdown
    assert "Suggested Walkthrough" in markdown
    assert html_path.exists()
    assert site_index.exists()
    assert "Everything worth showing in one place." in html_path.read_text(encoding="utf-8")

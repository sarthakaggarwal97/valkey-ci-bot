"""Tests for anomalous fuzzer issue creation/upsert."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts.fuzzer_issue_publisher import (
    FuzzerIssuePublisher,
    _fingerprint_for_analysis,
    _issue_marker,
)
from scripts.models import FuzzerRunAnalysis, FuzzerSignal


def _analysis() -> FuzzerRunAnalysis:
    return FuzzerRunAnalysis(
        repo="valkey-io/valkey-fuzzer",
        workflow_file="fuzzer-run.yml",
        run_id=123,
        run_url="https://github.com/valkey-io/valkey-fuzzer/actions/runs/123",
        conclusion="failure",
        head_sha="abc123",
        scenario_id="839534793",
        seed="839534793",
        overall_status="anomalous",
        summary="Slots remained assigned to killed nodes after chaos.",
        anomalies=[
            FuzzerSignal(
                title="Split-brain or slot loss",
                severity="critical",
                evidence="CRITICAL: 1024 slots still assigned to killed nodes.",
            ),
            FuzzerSignal(
                title="Topology validation failed",
                severity="critical",
                evidence="Topology validation failed in strict mode.",
            ),
        ],
        normal_signals=["Failover election won."],
        reproduction_hint="valkey-fuzzer cluster --seed 839534793",
        raw_log_fallback_used=True,
    )


def test_upsert_issue_creates_new_issue_when_no_match_exists() -> None:
    github_client = MagicMock()
    repo = github_client.get_repo.return_value
    repo.get_issues.return_value = []
    issue = MagicMock()
    issue.number = 7
    issue.html_url = "https://github.com/valkey-io/valkey-fuzzer/issues/7"
    repo.create_issue.return_value = issue

    publisher = FuzzerIssuePublisher(github_client)
    action, url = publisher.upsert_issue("valkey-io/valkey-fuzzer", _analysis())

    assert action == "created"
    assert url.endswith("/7")
    repo.create_issue.assert_called_once()
    body = repo.create_issue.call_args.kwargs["body"]
    assert "839534793" in body
    assert "Split-brain or slot loss" in body
    assert "valkey-ci-bot:fuzzer-issue:" in body


def test_upsert_issue_updates_existing_open_issue() -> None:
    github_client = MagicMock()
    repo = github_client.get_repo.return_value
    existing = MagicMock()
    existing.pull_request = None
    existing.number = 8
    existing.html_url = "https://github.com/valkey-io/valkey-fuzzer/issues/8"
    analysis = _analysis()
    existing.body = (
        f"{_issue_marker(_fingerprint_for_analysis(analysis))}\n"
        "<!-- valkey-ci-bot:occurrences:2 -->\n"
    )
    repo.get_issues.return_value = [existing]

    publisher = FuzzerIssuePublisher(github_client)
    action, url = publisher.upsert_issue("valkey-io/valkey-fuzzer", analysis)

    assert action == "updated"
    assert url.endswith("/8")
    existing.edit.assert_called_once()
    updated_body = existing.edit.call_args.kwargs["body"]
    assert "<!-- valkey-ci-bot:occurrences:3 -->" in updated_body

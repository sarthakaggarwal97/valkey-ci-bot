"""Tests for anomalous fuzzer issue creation/upsert."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts.fuzzer_issue_publisher import (
    FuzzerIssuePublisher,
    _bump_occurrence_count,
    _fingerprint_for_analysis,
    _issue_marker,
    _render_issue_body,
    _render_occurrence_comment,
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
    assert "valkey-ci-agent:fuzzer-issue:" in body


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
        "<!-- valkey-ci-agent:occurrences:2 -->\n"
        "## Fuzzer Run Summary\n\nOriginal content here.\n"
    )
    repo.get_issues.return_value = [existing]

    publisher = FuzzerIssuePublisher(github_client)
    action, url = publisher.upsert_issue("valkey-io/valkey-fuzzer", analysis)

    assert action == "updated"
    assert url.endswith("/8")

    # Issue body should only bump the counter, not replace content.
    existing.edit.assert_called_once()
    updated_body = existing.edit.call_args.kwargs["body"]
    assert "<!-- valkey-ci-agent:occurrences:3 -->" in updated_body
    assert "Original content here." in updated_body

    # A comment should be posted with the new run's details.
    existing.create_comment.assert_called_once()
    comment_body = existing.create_comment.call_args.kwargs["body"]
    assert "## Occurrence #3" in comment_body
    assert "839534793" in comment_body
    assert "Split-brain or slot loss" in comment_body


def test_render_issue_body_uses_verdict_and_plain_severity_labels() -> None:
    body = _render_issue_body(_analysis(), fingerprint="fp123", occurrences=2)

    assert "## Fuzzer Run Summary" in body
    assert "Action Needed" in body
    assert "raw job log fallback" in body
    assert "Critical:" in body
    assert "Warning:" not in body
    assert "🔴" not in body
    assert "🟡" not in body


def test_render_occurrence_comment_contains_run_details() -> None:
    comment = _render_occurrence_comment(_analysis(), occurrences=3)

    assert "## Occurrence #3" in comment
    assert "839534793" in comment
    assert "Split-brain or slot loss" in comment
    assert "Topology validation failed" in comment
    assert "raw job log fallback" in comment
    assert "valkey-fuzzer cluster --seed 839534793" in comment
    assert "Automated by `valkey-ci-agent`" in comment


def test_bump_occurrence_count_replaces_marker() -> None:
    body = (
        "<!-- valkey-ci-agent:fuzzer-issue:abc123 -->\n"
        "<!-- valkey-ci-agent:occurrences:5 -->\n"
        "## Fuzzer Run Summary\n"
    )
    updated = _bump_occurrence_count(body, 6)
    assert "<!-- valkey-ci-agent:occurrences:6 -->" in updated
    assert "<!-- valkey-ci-agent:occurrences:5 -->" not in updated
    assert "## Fuzzer Run Summary" in updated

"""Tests for scripts/ai_eval/report.py."""

from __future__ import annotations

import json

from scripts.ai_eval.report import (
    report_needs_human,
    report_stage_latencies,
    report_token_cost,
)


def _store_with_entries():
    return {
        "entries": {
            "fp-1": {
                "fingerprint": "fp-1", "status": "needs-human",
                "rejection_reason": "thin_evidence",
                "failure_identifier": "test_hash",
                "updated_at": "2025-01-01T00:00:00Z",
            },
            "fp-2": {
                "fingerprint": "fp-2", "status": "needs-human",
                "rejection_reason": "rubric_failed",
                "failure_identifier": "test_set",
                "updated_at": "2025-01-02T00:00:00Z",
            },
            "fp-3": {
                "fingerprint": "fp-3", "status": "open",
                "failure_identifier": "test_list",
                "updated_at": "2025-01-03T00:00:00Z",
            },
            "fp-4": {
                "fingerprint": "fp-4", "status": "needs-human",
                "rejection_reason": "thin_evidence",
                "failure_identifier": "test_zset",
                "updated_at": "2025-01-04T00:00:00Z",
            },
        }
    }


# --- report_needs_human ---

def test_report_needs_human_groups_by_reason():
    output = report_needs_human(_store_with_entries())
    # 3 needs-human entries total, grouped into 2 reasons
    assert "3 entries" in output
    assert "thin_evidence (2)" in output
    assert "rubric_failed (1)" in output
    # open entry (fp-3) should not appear
    assert "test_list" not in output


def test_report_needs_human_json_output():
    output = report_needs_human(_store_with_entries(), as_json=True)
    data = json.loads(output)
    assert "thin_evidence" in data
    assert "rubric_failed" in data
    assert len(data["thin_evidence"]) == 2


def test_report_needs_human_empty_store():
    output = report_needs_human({"entries": {}})
    assert "0 entries" in output


# --- report_stage_latencies ---

def test_report_stage_latencies_computes_percentiles():
    log_lines = [
        json.dumps({"stage": "evidence", "duration_ms": 100}),
        json.dumps({"stage": "evidence", "duration_ms": 200}),
        json.dumps({"stage": "evidence", "duration_ms": 300}),
        json.dumps({"stage": "root_cause", "duration_ms": 1000}),
    ]
    output = report_stage_latencies(log_lines)
    assert "evidence" in output
    assert "root_cause" in output
    assert "n=3" in output
    assert "n=1" in output


def test_report_stage_latencies_skips_malformed():
    log_lines = [
        json.dumps({"stage": "evidence", "duration_ms": 100}),
        "not json",
        json.dumps({"no_stage_field": 1}),
    ]
    # Should not crash
    output = report_stage_latencies(log_lines)
    assert "evidence" in output


def test_report_stage_latencies_empty():
    output = report_stage_latencies([])
    assert "Stage latencies" in output


# --- report_token_cost ---

def test_report_token_cost_sums_per_stage():
    log_lines = [
        json.dumps({"stage": "evidence", "tokens_in": 1000, "tokens_out": 200}),
        json.dumps({"stage": "evidence", "tokens_in": 500, "tokens_out": 100}),
        json.dumps({"stage": "root_cause", "tokens_in": 3000, "tokens_out": 500}),
    ]
    output = report_token_cost(log_lines)
    assert "evidence" in output
    assert "in=1,500" in output  # 1000 + 500
    assert "out=300" in output  # 200 + 100
    assert "root_cause" in output
    assert "Total" in output


def test_report_token_cost_skips_malformed():
    log_lines = [
        json.dumps({"stage": "evidence", "tokens_in": 100, "tokens_out": 10}),
        "garbage",
    ]
    output = report_token_cost(log_lines)
    assert "evidence" in output


def test_report_token_cost_empty():
    output = report_token_cost([])
    assert "Total" in output

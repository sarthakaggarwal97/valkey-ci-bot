"""Tests for scripts/pipeline.py — CI failure orchestrator end-to-end."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from scripts.models import RejectionReason, ValidationResult
from scripts.pipeline import process_failure


def _failure_report() -> dict:
    return {
        "parsed_failures": [
            {
                "failure_identifier": "test_hash_race",
                "test_name": "test_hash_race",
                "file_path": "src/t_hash.c",
                "error_message": "Expected 1 got 0",
                "assertion_details": None,
                "line_number": 42,
                "stack_trace": None,
                "parser_type": "tcl",
            }
        ]
    }


def _good_bedrock_and_runner():
    """Create mocks for a happy-path pipeline run."""
    bedrock = MagicMock()
    # analyst returns 1 hypothesis, no critic call since only 1 passes,
    # fix generator returns a patch, mask check passes
    analyst_response = json.dumps({
        "hypotheses": [{
            "summary": "race in hash",
            "causal_chain": ["dictResize concurrent"],
            "evidence_refs": ["log:0", "file:src/t_hash.c"],
            "confidence": "high",
            "disconfirmed_alternatives": [],
        }]
    })
    # Patch touches src/t_hash.c near the failure line AND adds a test
    patch = """--- a/src/t_hash.c
+++ b/src/t_hash.c
@@ -40,3 +40,3 @@
 line1
-old
+new
 line3
--- a/tests/unit/type/hash.tcl
+++ b/tests/unit/type/hash.tcl
@@ -0,0 +1,2 @@
+test {regression for race} {
+}
"""
    mask_response = json.dumps({"masks": False, "rationale": "patch fixes race"})
    # Calls in order: analyst, fix generator, mask check
    bedrock.invoke.side_effect = [analyst_response, patch, mask_response]

    runner = MagicMock()
    runner.run.return_value = ValidationResult(passed=True, output="OK")
    return bedrock, runner


# --- Happy path ---

def test_pipeline_happy_path_dry_run():
    bedrock, runner = _good_bedrock_and_runner()
    outcome = process_failure(
        failure_id="fp-1", run_id=1, job_ids=["j1"],
        workflow="ci.yml", failure_reports=[_failure_report()],
        log_text="error in test_hash_race\nline\nmore",
        bedrock_client=bedrock, validation_runner=runner,
        dry_run=True,
    )
    assert outcome.final_status == "dry-run"
    assert outcome.evidence is not None
    assert outcome.root_cause is not None
    assert outcome.root_cause.accepted is not None
    assert outcome.tournament is not None
    assert outcome.tournament.winning is not None
    assert outcome.rubric is not None
    assert outcome.rubric.overall_passed is True
    # Should have logs for evidence, root_cause, tournament, rubric
    assert outcome.stage_logs is not None
    stages = {log.stage for log in outcome.stage_logs}
    assert {"evidence", "root_cause", "tournament", "rubric"}.issubset(stages)


# --- Needs-human paths ---

def test_pipeline_root_cause_thin_evidence_routes_to_needs_human():
    bedrock = MagicMock()
    # Analyst returns hypothesis with bad evidence refs
    bedrock.invoke.return_value = json.dumps({
        "hypotheses": [{
            "summary": "x", "causal_chain": ["a"],
            "evidence_refs": [],  # no refs → deterministic fail
            "confidence": "high",
            "disconfirmed_alternatives": [],
        }]
    })
    runner = MagicMock()
    outcome = process_failure(
        failure_id="fp-1", run_id=1, job_ids=["j1"],
        workflow="ci.yml", failure_reports=[_failure_report()],
        log_text="logline",
        bedrock_client=bedrock, validation_runner=runner,
        dry_run=True,
    )
    assert outcome.final_status == "needs-human"
    assert outcome.rejection_reason == RejectionReason.THIN_EVIDENCE


def test_pipeline_tournament_empty_routes_to_needs_human():
    bedrock = MagicMock()
    analyst_response = json.dumps({
        "hypotheses": [{
            "summary": "race", "causal_chain": ["a"],
            "evidence_refs": ["log:0"], "confidence": "high",
            "disconfirmed_alternatives": [],
        }]
    })
    # Fix generator returns empty → no candidates
    bedrock.invoke.side_effect = [analyst_response, ""]
    runner = MagicMock()
    outcome = process_failure(
        failure_id="fp-1", run_id=1, job_ids=["j1"],
        workflow="ci.yml", failure_reports=[_failure_report()],
        log_text="logline",
        bedrock_client=bedrock, validation_runner=runner,
        dry_run=True,
    )
    assert outcome.final_status == "needs-human"
    assert outcome.rejection_reason == RejectionReason.TOURNAMENT_EMPTY


def test_pipeline_rubric_failed_routes_to_needs_human():
    bedrock = MagicMock()
    analyst_response = json.dumps({
        "hypotheses": [{
            "summary": "race", "causal_chain": ["a"],
            "evidence_refs": ["log:0"], "confidence": "high",
            "disconfirmed_alternatives": [],
        }]
    })
    # Big patch that fails patch_size
    big_patch = "--- a/x.c\n+++ b/x.c\n" + "\n".join([f"+line{i}" for i in range(600)])
    bedrock.invoke.side_effect = [analyst_response, big_patch]
    runner = MagicMock()
    runner.run.return_value = ValidationResult(passed=True, output="OK")
    outcome = process_failure(
        failure_id="fp-1", run_id=1, job_ids=["j1"],
        workflow="ci.yml", failure_reports=[_failure_report()],
        log_text="error in test_hash_race\nline",
        bedrock_client=bedrock, validation_runner=runner,
        dry_run=True,
    )
    assert outcome.final_status == "needs-human"
    assert outcome.rejection_reason == RejectionReason.RUBRIC_FAILED
    assert outcome.rubric is not None
    assert "patch_size" in outcome.rubric.blocking_checks


# --- Evidence errors ---

def test_pipeline_evidence_error_returns_error_status():
    bedrock = MagicMock()
    runner = MagicMock()
    # Empty workflow name causes EvidencePack.validate() to fail
    outcome = process_failure(
        failure_id="", run_id=None, job_ids=[],
        workflow="", failure_reports=[],
        bedrock_client=bedrock, validation_runner=runner,
        dry_run=True,
    )
    assert outcome.final_status == "error"


# --- Stage logs structure ---

def test_pipeline_stage_logs_have_durations():
    bedrock, runner = _good_bedrock_and_runner()
    outcome = process_failure(
        failure_id="fp-1", run_id=1, job_ids=["j1"],
        workflow="ci.yml", failure_reports=[_failure_report()],
        log_text="line\nerror in test_hash_race\nline",
        bedrock_client=bedrock, validation_runner=runner,
        dry_run=True,
    )
    assert outcome.stage_logs is not None
    for log in outcome.stage_logs:
        assert log.duration_ms >= 0
        assert log.stage
        assert log.failure_id == "fp-1"


def test_pipeline_stage_log_to_json():
    bedrock, runner = _good_bedrock_and_runner()
    outcome = process_failure(
        failure_id="fp-1", run_id=1, job_ids=["j1"],
        workflow="ci.yml", failure_reports=[_failure_report()],
        log_text="line\nerror in test_hash_race\nline",
        bedrock_client=bedrock, validation_runner=runner,
        dry_run=True,
    )
    # Each log record should be JSON-serializable
    for log in outcome.stage_logs or []:
        d = json.loads(log.to_json())
        assert d["stage"]
        assert "duration_ms" in d
        assert "outcome" in d


# --- State persistence via failure_store ---

def test_pipeline_persists_needs_human_to_failure_store():
    """When the pipeline routes a failure to needs-human, the store entry
    should be updated with the rejection reason and status."""
    from scripts.models import FailureStoreEntry

    bedrock = MagicMock()
    # Analyst returns hypothesis with no evidence refs → THIN_EVIDENCE
    bedrock.invoke.return_value = json.dumps({
        "hypotheses": [{
            "summary": "x", "causal_chain": ["a"],
            "evidence_refs": [],
            "confidence": "high",
            "disconfirmed_alternatives": [],
        }]
    })
    runner = MagicMock()

    # Fake failure store with an existing entry
    class FakeStore:
        def __init__(self):
            self.entries = {
                "fp-1": FailureStoreEntry(
                    fingerprint="fp-1",
                    failure_identifier="test_hash_race",
                    test_name="test_hash_race",
                    incident_key="ik-1",
                    error_signature="sig",
                    file_path="tests/unit/type/hash.tcl",
                    pr_url=None,
                    status="open",
                    created_at="2025-01-01T00:00:00Z",
                    updated_at="2025-01-01T00:00:00Z",
                ),
            }

    store = FakeStore()
    process_failure(
        failure_id="fp-1", run_id=1, job_ids=["j1"],
        workflow="ci.yml", failure_reports=[_failure_report()],
        log_text="logline",
        bedrock_client=bedrock, validation_runner=runner,
        failure_store=store, dry_run=True,
    )
    entry = store.entries["fp-1"]
    assert entry.status == "needs-human"
    assert entry.rejection_reason == "thin_evidence"
    assert entry.evidence_pack is not None
    assert entry.evidence_pack["failure_id"] == "fp-1"


def test_pipeline_persists_pr_url_on_success():
    from scripts.models import FailureStoreEntry

    bedrock, runner = _good_bedrock_and_runner()

    class FakePRManager:
        def create_pr(self, *args, **kwargs):
            return "https://github.com/org/repo/pull/42"

    class FakeStore:
        def __init__(self):
            self.entries = {
                "fp-1": FailureStoreEntry(
                    fingerprint="fp-1",
                    failure_identifier="test_hash_race",
                    test_name="test_hash_race",
                    incident_key="ik-1",
                    error_signature="sig",
                    file_path="src/t_hash.c",
                    pr_url=None,
                    status="open",
                    created_at="2025-01-01T00:00:00Z",
                    updated_at="2025-01-01T00:00:00Z",
                ),
            }

    store = FakeStore()
    outcome = process_failure(
        failure_id="fp-1", run_id=1, job_ids=["j1"],
        workflow="ci.yml", failure_reports=[_failure_report()],
        log_text="line\nerror in test_hash_race\nline",
        bedrock_client=bedrock, validation_runner=runner,
        pr_manager=FakePRManager(),
        failure_store=store, dry_run=False,
    )
    assert outcome.final_status == "pr-created"
    entry = store.entries["fp-1"]
    assert entry.pr_url == "https://github.com/org/repo/pull/42"
    assert entry.status == "processing"
    assert entry.evidence_pack is not None


def test_pipeline_handles_missing_store_entry_gracefully():
    """If failure_store has no entry for this failure_id, persist is a no-op."""
    bedrock, runner = _good_bedrock_and_runner()

    class FakeStore:
        def __init__(self):
            self.entries = {}

    store = FakeStore()
    # Should not raise
    outcome = process_failure(
        failure_id="fp-not-in-store", run_id=1, job_ids=["j1"],
        workflow="ci.yml", failure_reports=[_failure_report()],
        log_text="line\nerror in test_hash_race\nline",
        bedrock_client=bedrock, validation_runner=runner,
        failure_store=store, dry_run=True,
    )
    assert outcome.final_status == "dry-run"
    assert store.entries == {}  # no entry was fabricated

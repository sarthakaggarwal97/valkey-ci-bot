"""Tests for the needs-human additions to FailureStoreEntry.

The broader stage-contract tests were reverted along with the speculative
AI pipeline; this file covers only what remains: the two optional fields
(evidence_pack, rejection_reason) that let a maintainer see why a failure
was routed to human triage.
"""

from __future__ import annotations

from scripts.models import FailureStoreEntry


def test_failure_store_entry_accepts_new_fields_with_defaults():
    entry = FailureStoreEntry(
        fingerprint="fp-1",
        failure_identifier="test_x",
        test_name="test_x",
        incident_key="ik-1",
        error_signature="sig",
        file_path="tests/unit/x.tcl",
        pr_url=None,
        status="open",
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
    )
    assert entry.evidence_pack is None
    assert entry.rejection_reason is None


def test_failure_store_entry_with_needs_human_state():
    entry = FailureStoreEntry(
        fingerprint="fp-1",
        failure_identifier="test_x",
        test_name="test_x",
        incident_key="ik-1",
        error_signature="sig",
        file_path="tests/unit/x.tcl",
        pr_url=None,
        status="needs-human",
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        rejection_reason="thin_evidence",
        evidence_pack={"failure_id": "fp-1"},
    )
    assert entry.status == "needs-human"
    assert entry.rejection_reason == "thin_evidence"
    assert entry.evidence_pack == {"failure_id": "fp-1"}

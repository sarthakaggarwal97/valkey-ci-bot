"""Tests for scripts/pipeline_adapter.py — bridges new pipeline types to
the legacy pr_manager / code_reviewer / failure_store APIs."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from scripts.models import (
    CommitInfo,
    CriticVerdict,
    EvidencePack,
    FailureStoreEntry,
    FixCandidate,
    InspectedFile,
    LogExcerpt,
    ParsedFailure,
    RejectionReason,
    RootCauseHypothesis,
    RootCauseResult,
    TournamentResult,
    ValidatedCandidate,
    ValidationResult,
)
from scripts.pipeline_adapter import (
    create_pr_via_legacy_manager,
    evidence_to_failure_report,
    evidence_to_pr_review_context,
    hypothesis_to_root_cause_report,
    update_failure_store_entry,
)


def _evidence() -> EvidencePack:
    return EvidencePack(
        failure_id="fp-1", run_id=42, job_ids=["job-1"],
        workflow="daily.yml",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="test_hash",
                test_name="test_hash",
                file_path="src/t_hash.c",
                error_message="assertion failed",
                assertion_details=None,
                line_number=100,
                stack_trace=None,
                parser_type="tcl",
            )
        ],
        log_excerpts=[LogExcerpt(source="job-log", content="FAIL output")],
        source_files_inspected=[
            InspectedFile(path="src/t_hash.c", reason="stack", excerpt="int foo;"),
        ],
        test_files_inspected=[
            InspectedFile(path="tests/unit/hash.tcl", reason="test"),
        ],
        valkey_guidance_used=[],
        recent_commits=[CommitInfo(sha="abc", message="m", author="a")],
        linked_urls=[],
        unknowns=[],
        built_at="2025-01-01T00:00:00Z",
    )


def _hypothesis(files=None, confidence="high") -> RootCauseHypothesis:
    return RootCauseHypothesis(
        summary="race condition",
        causal_chain=["step1", "step2"],
        evidence_refs=files or ["file:src/t_hash.c", "log:0"],
        confidence=confidence,
        disconfirmed_alternatives=["not a timeout"],
    )


# --- evidence_to_failure_report ---

def test_evidence_to_failure_report_basic():
    ev = _evidence()
    report = evidence_to_failure_report(
        ev, job_name="test-sanitizer", commit_sha="abc123",
        workflow_file="daily.yml", repo_full_name="valkey-io/valkey",
    )
    assert report.workflow_name == "daily.yml"
    assert report.job_name == "test-sanitizer"
    assert report.commit_sha == "abc123"
    assert len(report.parsed_failures) == 1
    assert report.parsed_failures[0].test_name == "test_hash"
    assert report.raw_log_excerpt == "FAIL output"
    assert report.failure_source == "trusted"
    assert report.is_unparseable is False


def test_evidence_to_failure_report_falls_back_to_first_job_id():
    ev = _evidence()
    report = evidence_to_failure_report(ev)
    assert report.job_name == "job-1"


def test_evidence_to_failure_report_marks_unparseable_when_no_failures():
    ev = _evidence()
    ev.parsed_failures = []
    report = evidence_to_failure_report(ev)
    assert report.is_unparseable is True


def test_evidence_to_failure_report_default_target_branch():
    report = evidence_to_failure_report(_evidence())
    assert report.target_branch == "unstable"


# --- hypothesis_to_root_cause_report ---

def test_hypothesis_to_root_cause_report_extracts_files_from_refs():
    hyp = _hypothesis(files=["file:src/a.c", "file:src/b.c", "log:0"])
    report = hypothesis_to_root_cause_report(hyp, _evidence())
    assert report.description == "race condition"
    assert "src/a.c" in report.files_to_change
    assert "src/b.c" in report.files_to_change
    assert report.confidence == "high"


def test_hypothesis_to_root_cause_report_falls_back_to_inspected_files():
    hyp = _hypothesis(files=["log:0"])  # No file: refs
    report = hypothesis_to_root_cause_report(hyp, _evidence())
    # Falls back to inspected files
    assert "src/t_hash.c" in report.files_to_change
    assert "tests/unit/hash.tcl" in report.files_to_change


def test_hypothesis_to_root_cause_report_includes_disconfirmed_in_rationale():
    hyp = _hypothesis()
    report = hypothesis_to_root_cause_report(hyp, _evidence())
    assert "not a timeout" in report.rationale
    assert "step1" in report.rationale


# --- update_failure_store_entry ---

def test_update_failure_store_entry_applies_all_updates():
    entry = FailureStoreEntry(
        fingerprint="fp", failure_identifier="f", test_name="t",
        incident_key="i", error_signature="e", file_path="p",
        pr_url=None, status="open",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    ev = _evidence()
    updated = update_failure_store_entry(
        entry,
        evidence=ev,
        rejection=RejectionReason.THIN_EVIDENCE,
        pr_url="https://github.com/org/r/pull/1",
        status="needs-human",
    )
    assert updated is entry  # Same reference
    assert entry.evidence_pack is not None
    assert entry.rejection_reason == "thin_evidence"
    assert entry.pr_url == "https://github.com/org/r/pull/1"
    assert entry.status == "needs-human"
    assert entry.updated_at != "2024-01-01T00:00:00Z"  # Was bumped


def test_update_failure_store_entry_partial_update():
    entry = FailureStoreEntry(
        fingerprint="fp", failure_identifier="f", test_name="t",
        incident_key="i", error_signature="e", file_path="p",
        pr_url="existing-url", status="open",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    # Only update status — pr_url should be preserved
    update_failure_store_entry(entry, status="merged")
    assert entry.status == "merged"
    assert entry.pr_url == "existing-url"
    assert entry.rejection_reason is None


# --- evidence_to_pr_review_context ---

def test_evidence_to_pr_review_context_builds_both_types():
    ev = _evidence()
    pr, diff_scope = evidence_to_pr_review_context(
        ev, pr_number=123, pr_title="Fix hash", diff_text="diff text",
        base_sha="b" * 40, head_sha="h" * 40,
    )
    assert pr.number == 123
    assert pr.title == "Fix hash"
    assert pr.base_sha == "b" * 40
    # Inspected files become ChangedFile entries
    paths = [f.path for f in pr.files]
    assert "src/t_hash.c" in paths
    assert "tests/unit/hash.tcl" in paths
    # Commits come from recent_commits
    assert len(pr.commits) == 1
    assert pr.commits[0].sha == "abc"
    # DiffScope mirrors PR files
    assert diff_scope.base_sha == "b" * 40
    assert len(diff_scope.files) == 2


def test_evidence_to_pr_review_context_falls_back_to_log_excerpt_diff():
    ev = _evidence()
    # Empty diff_text — adapter should use the first log excerpt
    pr, _ = evidence_to_pr_review_context(ev, pr_number=1, diff_text="")
    # No ChangedFile.patch is set, but the function doesn't crash
    assert pr.number == 1


def test_evidence_to_pr_review_context_empty_evidence_files():
    ev = _evidence()
    ev.source_files_inspected = []
    ev.test_files_inspected = []
    pr, diff_scope = evidence_to_pr_review_context(ev, pr_number=1)
    assert pr.files == []
    assert diff_scope.files == []


# --- create_pr_via_legacy_manager ---

def test_create_pr_via_legacy_manager_happy_path():
    ev = _evidence()
    hyp = _hypothesis()
    rc = RootCauseResult(
        accepted=hyp, rejected=[],
        critic_verdict=CriticVerdict(1, 0, False),
    )
    winner = ValidatedCandidate(
        candidate=FixCandidate(
            candidate_id="c1", prompt_variant="minimal",
            patch="diff content", rationale="fix",
        ),
        validation_result=ValidationResult(passed=True, output="OK"),
    )
    tournament = TournamentResult(winning=winner, rejected=[])

    pr_manager = MagicMock()
    pr_manager.create_pr.return_value = "https://github.com/x/y/pull/1"

    url = create_pr_via_legacy_manager(
        pr_manager, tournament, rc, ev,
        job_name="test-job", commit_sha="abc",
        workflow_file="ci.yml", repo_full_name="x/y",
        target_branch="unstable",
    )
    assert url == "https://github.com/x/y/pull/1"
    # Verify the legacy PRManager got the correct types
    call_args = pr_manager.create_pr.call_args
    # create_pr(patch, failure_report, root_cause, target_branch, *, draft=False)
    args = call_args[0]
    assert args[0] == "diff content"  # patch
    assert args[1].job_name == "test-job"  # FailureReport
    assert args[2].description == "race condition"  # RootCauseReport
    assert args[3] == "unstable"  # target_branch


def test_create_pr_via_legacy_manager_refuses_without_winner():
    ev = _evidence()
    rc = RootCauseResult(
        accepted=_hypothesis(), rejected=[],
        critic_verdict=CriticVerdict(1, 0, False),
    )
    tournament = TournamentResult(winning=None, rejected=[], reason_if_empty="x")
    with pytest.raises(ValueError, match="no winning candidate"):
        create_pr_via_legacy_manager(
            MagicMock(), tournament, rc, ev,
        )


def test_create_pr_via_legacy_manager_refuses_without_accepted_hypothesis():
    ev = _evidence()
    rc = RootCauseResult(
        accepted=None, rejected=[],
        critic_verdict=CriticVerdict(0, 0, False),
        rejection_reason=RejectionReason.THIN_EVIDENCE,
    )
    winner = ValidatedCandidate(
        candidate=FixCandidate(
            candidate_id="c1", prompt_variant="minimal",
            patch="diff", rationale="fix",
        ),
        validation_result=ValidationResult(passed=True, output="OK"),
    )
    tournament = TournamentResult(winning=winner, rejected=[])
    with pytest.raises(ValueError, match="no accepted root cause"):
        create_pr_via_legacy_manager(
            MagicMock(), tournament, rc, ev,
        )

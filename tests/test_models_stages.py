"""Tests for evidence-first AI pipeline stage contracts."""

from __future__ import annotations

import pytest

from scripts.models import (
    CommitInfo,
    CriticVerdict,
    EvidencePack,
    FixCandidate,
    InspectedFile,
    LogExcerpt,
    ParsedFailure,
    RejectedCandidate,
    RejectedHypothesis,
    RejectionReason,
    RootCauseHypothesis,
    RootCauseResult,
    RubricCheck,
    RubricVerdict,
    TournamentResult,
    ValidatedCandidate,
    ValidationResult,
)


def _make_evidence_pack(**overrides) -> EvidencePack:
    defaults = dict(
        failure_id="fp-abc123",
        run_id=12345,
        job_ids=["job-1"],
        workflow="daily.yml",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="test_hash",
                test_name="test_hash",
                file_path="tests/unit/type/hash.tcl",
                error_message="Expected 1 got 0",
                assertion_details=None,
                line_number=42,
                stack_trace=None,
                parser_type="tcl",
            )
        ],
        log_excerpts=[LogExcerpt(source="job-log", content="FAILED test_hash")],
        source_files_inspected=[InspectedFile(path="src/t_hash.c", reason="stack trace")],
        test_files_inspected=[InspectedFile(path="tests/unit/type/hash.tcl", reason="failing test")],
        valkey_guidance_used=["coding-style"],
        recent_commits=[CommitInfo(sha="abc123", message="fix hash", author="dev")],
        linked_urls=["https://github.com/valkey-io/valkey/actions/runs/12345"],
        unknowns=[],
        built_at="2025-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    return EvidencePack(**defaults)


# --- RejectionReason ---

def test_rejection_reason_values():
    assert RejectionReason.THIN_EVIDENCE.value == "thin_evidence"
    assert RejectionReason.RUBRIC_FAILED.value == "rubric_failed"
    assert len(RejectionReason) == 6


# --- LogExcerpt ---

def test_log_excerpt_validate_ok():
    LogExcerpt(source="job-log", content="some log").validate()


def test_log_excerpt_validate_empty():
    with pytest.raises(ValueError, match="content must not be empty"):
        LogExcerpt(source="job-log", content="").validate()


# --- InspectedFile ---

def test_inspected_file_validate_ok():
    InspectedFile(path="src/server.c", reason="stack trace").validate()


def test_inspected_file_validate_empty_path():
    with pytest.raises(ValueError, match="path must not be empty"):
        InspectedFile(path="", reason="test").validate()


# --- CommitInfo ---

def test_commit_info_validate_ok():
    CommitInfo(sha="abc", message="fix", author="dev").validate()


def test_commit_info_validate_empty_sha():
    with pytest.raises(ValueError, match="sha must not be empty"):
        CommitInfo(sha="", message="fix", author="dev").validate()


# --- EvidencePack ---

def test_evidence_pack_validate_ok():
    _make_evidence_pack().validate()


def test_evidence_pack_validate_empty_failure_id():
    with pytest.raises(ValueError, match="failure_id"):
        _make_evidence_pack(failure_id="").validate()


def test_evidence_pack_validate_empty_workflow():
    with pytest.raises(ValueError, match="workflow"):
        _make_evidence_pack(workflow="").validate()


def test_evidence_pack_round_trip():
    ep = _make_evidence_pack()
    d = ep.to_dict()
    ep2 = EvidencePack.from_dict(d)
    assert ep2.failure_id == ep.failure_id
    assert ep2.run_id == ep.run_id
    assert len(ep2.parsed_failures) == 1
    assert ep2.parsed_failures[0].test_name == "test_hash"
    assert len(ep2.log_excerpts) == 1
    assert ep2.log_excerpts[0].content == "FAILED test_hash"
    assert len(ep2.recent_commits) == 1
    ep2.validate()


# --- RootCauseHypothesis ---

def test_hypothesis_validate_ok():
    RootCauseHypothesis(
        summary="race in hash resize",
        causal_chain=["dictResize called without lock"],
        evidence_refs=["log:42"],
        confidence="high",
    ).validate()


def test_hypothesis_validate_bad_confidence():
    with pytest.raises(ValueError, match="Invalid confidence"):
        RootCauseHypothesis(
            summary="x", causal_chain=["y"], evidence_refs=[], confidence="maybe"
        ).validate()


def test_hypothesis_validate_empty_chain():
    with pytest.raises(ValueError, match="causal_chain"):
        RootCauseHypothesis(
            summary="x", causal_chain=[], evidence_refs=[], confidence="low"
        ).validate()


def test_hypothesis_round_trip():
    h = RootCauseHypothesis(
        summary="race", causal_chain=["a", "b"], evidence_refs=["log:1"],
        confidence="medium", disconfirmed_alternatives=["not a timeout"],
    )
    h2 = RootCauseHypothesis.from_dict(h.to_dict())
    assert h2.summary == h.summary
    assert h2.disconfirmed_alternatives == ["not a timeout"]


# --- RootCauseResult ---

def test_root_cause_result_round_trip():
    result = RootCauseResult(
        accepted=RootCauseHypothesis(
            summary="race", causal_chain=["a"], evidence_refs=["log:1"], confidence="high"
        ),
        rejected=[
            RejectedHypothesis(
                hypothesis=RootCauseHypothesis(
                    summary="timeout", causal_chain=["b"], evidence_refs=[], confidence="low"
                ),
                reason="speculative",
            )
        ],
        critic_verdict=CriticVerdict(
            deterministic_checks_passed=1, deterministic_checks_failed=1,
            model_critic_called=True, model_critic_rationale="accepted race hypothesis",
        ),
        rejection_reason=None,
    )
    d = result.to_dict()
    r2 = RootCauseResult.from_dict(d)
    assert r2.accepted is not None
    assert r2.accepted.summary == "race"
    assert len(r2.rejected) == 1
    assert r2.critic_verdict.model_critic_called is True
    assert r2.rejection_reason is None


def test_root_cause_result_with_rejection():
    result = RootCauseResult(
        accepted=None,
        rejected=[],
        critic_verdict=CriticVerdict(0, 3, False),
        rejection_reason=RejectionReason.THIN_EVIDENCE,
    )
    d = result.to_dict()
    r2 = RootCauseResult.from_dict(d)
    assert r2.accepted is None
    assert r2.rejection_reason == RejectionReason.THIN_EVIDENCE


# --- FixCandidate ---

def test_fix_candidate_validate_ok():
    FixCandidate(
        candidate_id="c1", prompt_variant="minimal",
        patch="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new",
        rationale="fix the bug",
    ).validate()


def test_fix_candidate_validate_bad_variant():
    with pytest.raises(ValueError, match="prompt_variant"):
        FixCandidate(
            candidate_id="c1", prompt_variant="aggressive",
            patch="diff", rationale="x",
        ).validate()


def test_fix_candidate_round_trip():
    fc = FixCandidate(
        candidate_id="c1", prompt_variant="root_cause_deep",
        patch="diff content", rationale="deep fix",
        evidence_refs=["log:1", "file:src/server.c"],
    )
    fc2 = FixCandidate.from_dict(fc.to_dict())
    assert fc2.prompt_variant == "root_cause_deep"
    assert fc2.evidence_refs == ["log:1", "file:src/server.c"]


# --- TournamentResult ---

def test_tournament_result_with_winner():
    winner = ValidatedCandidate(
        candidate=FixCandidate(
            candidate_id="c1", prompt_variant="minimal",
            patch="diff", rationale="fix",
        ),
        validation_result=ValidationResult(passed=True, output="OK"),
    )
    result = TournamentResult(winning=winner, rejected=[])
    d = result.to_dict()
    r2 = TournamentResult.from_dict(d)
    assert r2.winning is not None
    assert r2.winning.candidate.candidate_id == "c1"
    assert r2.winning.validation_result.passed is True


def test_tournament_result_empty():
    result = TournamentResult(
        winning=None, rejected=[],
        reason_if_empty="all_candidates_failed_validation",
    )
    d = result.to_dict()
    r2 = TournamentResult.from_dict(d)
    assert r2.winning is None
    assert r2.reason_if_empty == "all_candidates_failed_validation"


# --- RubricCheck / RubricVerdict ---

def test_rubric_check_round_trip():
    rc = RubricCheck(name="patch_size", kind="deterministic", passed=True, detail="42 lines")
    rc2 = RubricCheck.from_dict(rc.to_dict())
    assert rc2.name == "patch_size"
    assert rc2.passed is True


def test_rubric_verdict_validate_empty():
    with pytest.raises(ValueError, match="checks must not be empty"):
        RubricVerdict(checks=[], overall_passed=True, blocking_checks=[]).validate()


def test_rubric_verdict_round_trip():
    verdict = RubricVerdict(
        checks=[
            RubricCheck(name="patch_size", kind="deterministic", passed=True, detail="ok"),
            RubricCheck(name="mask_check", kind="model", passed=False, detail="masks failure"),
        ],
        overall_passed=False,
        blocking_checks=["mask_check"],
    )
    d = verdict.to_dict()
    v2 = RubricVerdict.from_dict(d)
    assert len(v2.checks) == 2
    assert v2.overall_passed is False
    assert v2.blocking_checks == ["mask_check"]


# --- FailureStoreEntry needs-human ---

def test_failure_store_entry_needs_human():
    from scripts.models import FailureStoreEntry

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
        rejection_reason=RejectionReason.THIN_EVIDENCE.value,
        evidence_pack={"failure_id": "fp-1"},
    )
    assert entry.status == "needs-human"
    assert entry.rejection_reason == "thin_evidence"
    assert entry.evidence_pack is not None


# --- Config AI stages ---

def test_config_ai_stages_defaults():
    from scripts.config import AIStagesConfig, BotConfig

    cfg = BotConfig()
    assert cfg.ai_stages.fixes.candidate_count == 1
    assert cfg.ai_stages.fixes.global_validation_concurrency == 3
    assert cfg.ai_stages.min_confidence_for_fix == "medium"
    assert cfg.ai_stages.root_cause_analyst.model == ""


def test_config_ai_stages_from_yaml():
    from scripts.config import load_config_data

    raw = {
        "ai": {
            "stages": {
                "root_cause_analyst": {"model": "us.anthropic.claude-haiku-4-v1"},
            },
            "fixes": {
                "candidate_count": 3,
                "global_validation_concurrency": 2,
            },
            "min_confidence_for_fix": "high",
        }
    }
    cfg = load_config_data(raw)
    assert cfg.ai_stages.root_cause_analyst.model == "us.anthropic.claude-haiku-4-v1"
    assert cfg.ai_stages.root_cause_critic.model == ""
    assert cfg.ai_stages.fixes.candidate_count == 3
    assert cfg.ai_stages.fixes.global_validation_concurrency == 2
    assert cfg.ai_stages.min_confidence_for_fix == "high"

"""Tests for scripts/stages/fix_tournament.py."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from scripts.models import (
    EvidencePack,
    FixCandidate,
    InspectedFile,
    LogExcerpt,
    ParsedFailure,
    RootCauseHypothesis,
    ValidationResult,
)
from scripts.stages.fix_tournament import (
    _count_patch_lines,
    generate_candidates,
    rank_and_pick,
    run_tournament,
    validate_candidates,
)


def _make_evidence() -> EvidencePack:
    return EvidencePack(
        failure_id="fp-1", run_id=1, job_ids=["job-1"], workflow="ci.yml",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="test_x", test_name="test_x",
                file_path="tests/x.tcl", error_message="fail",
                assertion_details=None, line_number=1,
                stack_trace=None, parser_type="tcl",
            )
        ],
        log_excerpts=[LogExcerpt(source="log", content="FAIL")],
        source_files_inspected=[InspectedFile(path="src/x.c", reason="x")],
        test_files_inspected=[],
        valkey_guidance_used=[], recent_commits=[],
        linked_urls=[], unknowns=[],
        built_at="2025-01-01T00:00:00Z",
    )


def _make_root_cause() -> RootCauseHypothesis:
    return RootCauseHypothesis(
        summary="bug in x", causal_chain=["step1"],
        evidence_refs=["log:0"], confidence="high",
    )


def _patch(lines: int = 2) -> str:
    body = "\n".join([f"+line{i}" for i in range(lines)])
    return f"--- a/src/x.c\n+++ b/src/x.c\n@@ -1,{lines} +1,{lines} @@\n{body}\n"


# --- _count_patch_lines ---

def test_count_patch_lines_simple():
    assert _count_patch_lines(_patch(5)) == 5


# --- generate_candidates ---

def test_generate_candidates_count_1_produces_minimal():
    bedrock = MagicMock()
    bedrock.invoke.return_value = _patch()
    candidates = generate_candidates(
        _make_evidence(), _make_root_cause(), bedrock, candidate_count=1,
    )
    assert len(candidates) == 1
    assert candidates[0].prompt_variant == "minimal"


def test_generate_candidates_count_3_produces_diverse_variants():
    bedrock = MagicMock()
    # Return a unique patch per call so candidates differ
    bedrock.invoke.side_effect = [_patch(2), _patch(5), _patch(10)]
    candidates = generate_candidates(
        _make_evidence(), _make_root_cause(), bedrock, candidate_count=3,
    )
    assert len(candidates) == 3
    variants = [c.prompt_variant for c in candidates]
    assert set(variants) == {"minimal", "root_cause_deep", "defensive_guard"}


def test_generate_candidates_distinct_prompts_sent_to_model():
    """Verify the 3 prompts sent to the mock differ materially."""
    bedrock = MagicMock()
    bedrock.invoke.return_value = _patch()
    generate_candidates(
        _make_evidence(), _make_root_cause(), bedrock, candidate_count=3,
    )
    # bedrock.invoke is now (system_prompt, user_prompt, *, model_id=...)
    # In Python 3.7 mock, call is (args_tuple, kwargs_dict) — use index access
    system_prompts = []
    for call in bedrock.invoke.call_args_list:
        args = call[0]  # args tuple
        # First positional arg is the system prompt
        system_prompts.append(args[0] if args else "")
    # All 3 system prompts should be different
    assert len(set(system_prompts)) == 3, f"Prompts not distinct: {[p[:80] for p in system_prompts]}"
    # Verify each contains its variant's hint
    joined = " ".join(system_prompts)
    assert "SMALLEST" in joined
    assert "underlying" in joined.lower() or "root cause" in joined.lower()
    assert "defensive guard" in joined.lower()


def test_generate_candidates_skips_empty_responses():
    bedrock = MagicMock()
    bedrock.invoke.side_effect = [_patch(), "", _patch()]
    candidates = generate_candidates(
        _make_evidence(), _make_root_cause(), bedrock, candidate_count=3,
    )
    # Empty response is skipped
    assert len(candidates) == 2


# --- validate_candidates ---

def test_validate_candidates_all_pass():
    candidates = [
        FixCandidate(
            candidate_id=f"c{i}", prompt_variant="minimal",
            patch=_patch(), rationale="fix",
        )
        for i in range(3)
    ]
    runner = MagicMock()
    runner.run.return_value = ValidationResult(passed=True, output="OK")
    results = validate_candidates(candidates, runner)
    assert len(results) == 3
    for r in results:
        assert r.validation_result.passed is True


def test_validate_candidates_mixed():
    candidates = [
        FixCandidate(candidate_id="c1", prompt_variant="minimal", patch=_patch(), rationale="x"),
        FixCandidate(candidate_id="c2", prompt_variant="minimal", patch=_patch(), rationale="y"),
    ]
    runner = MagicMock()
    runner.run.side_effect = [
        ValidationResult(passed=True, output="OK"),
        ValidationResult(passed=False, output="fail"),
    ]
    results = validate_candidates(candidates, runner)
    passed = [r for r in results if r.validation_result.passed]
    failed = [r for r in results if not r.validation_result.passed]
    assert len(passed) == 1
    assert len(failed) == 1


def test_validate_candidates_catches_runner_exception():
    from scripts.models import RejectedCandidate
    candidates = [FixCandidate(candidate_id="c1", prompt_variant="minimal", patch=_patch(), rationale="x")]
    runner = MagicMock()
    runner.run.side_effect = RuntimeError("validation crashed")
    results = validate_candidates(candidates, runner)
    assert len(results) == 1
    assert isinstance(results[0], RejectedCandidate)
    assert "validation crashed" in results[0].reason.lower()


def test_validate_candidates_respects_semaphore():
    """Verify the global semaphore limits concurrent validation count."""
    # Create 6 candidates; semaphore allows 2 concurrent
    candidates = [
        FixCandidate(candidate_id=f"c{i}", prompt_variant="minimal", patch=_patch(), rationale="x")
        for i in range(6)
    ]
    semaphore = threading.BoundedSemaphore(2)
    concurrent_count = 0
    peak_concurrent = 0
    lock = threading.Lock()

    def slow_run(patch):
        nonlocal concurrent_count, peak_concurrent
        with lock:
            concurrent_count += 1
            peak_concurrent = max(peak_concurrent, concurrent_count)
        time.sleep(0.05)
        with lock:
            concurrent_count -= 1
        return ValidationResult(passed=True, output="OK")

    runner = MagicMock()
    runner.run = slow_run
    results = validate_candidates(candidates, runner, semaphore=semaphore)
    assert len(results) == 6
    assert peak_concurrent <= 2, f"Semaphore breached: peak was {peak_concurrent}"


# --- rank_and_pick ---

def test_rank_and_pick_winner_is_smallest_passing():
    from scripts.models import ValidatedCandidate
    c1 = ValidatedCandidate(
        candidate=FixCandidate(candidate_id="big", prompt_variant="root_cause_deep", patch=_patch(10), rationale="x"),
        validation_result=ValidationResult(passed=True, output="OK"),
    )
    c2 = ValidatedCandidate(
        candidate=FixCandidate(candidate_id="small", prompt_variant="minimal", patch=_patch(2), rationale="y"),
        validation_result=ValidationResult(passed=True, output="OK"),
    )
    result = rank_and_pick([c1, c2])
    assert result.winning is not None
    assert result.winning.candidate.candidate_id == "small"
    assert len(result.rejected) == 1
    assert result.rejected[0].candidate.candidate_id == "big"


def test_rank_and_pick_prefers_minimal_variant_when_tied():
    from scripts.models import ValidatedCandidate
    # Same patch size, different variants
    c_deep = ValidatedCandidate(
        candidate=FixCandidate(candidate_id="deep", prompt_variant="root_cause_deep", patch=_patch(2), rationale="x"),
        validation_result=ValidationResult(passed=True, output="OK"),
    )
    c_min = ValidatedCandidate(
        candidate=FixCandidate(candidate_id="min", prompt_variant="minimal", patch=_patch(2), rationale="y"),
        validation_result=ValidationResult(passed=True, output="OK"),
    )
    result = rank_and_pick([c_deep, c_min])
    assert result.winning.candidate.candidate_id == "min"


def test_rank_and_pick_empty_when_all_fail():
    from scripts.models import ValidatedCandidate
    c1 = ValidatedCandidate(
        candidate=FixCandidate(candidate_id="a", prompt_variant="minimal", patch=_patch(), rationale="x"),
        validation_result=ValidationResult(passed=False, output="fail"),
    )
    result = rank_and_pick([c1])
    assert result.winning is None
    assert result.reason_if_empty == "all_candidates_failed_validation"


def test_rank_and_pick_empty_when_no_candidates():
    result = rank_and_pick([])
    assert result.winning is None
    assert result.reason_if_empty == "no_candidates_generated"


# --- run_tournament end-to-end ---

def test_run_tournament_end_to_end():
    bedrock = MagicMock()
    bedrock.invoke.return_value = _patch()
    runner = MagicMock()
    runner.run.return_value = ValidationResult(passed=True, output="OK")
    result = run_tournament(
        _make_evidence(), _make_root_cause(), bedrock, runner, candidate_count=1,
    )
    assert result.winning is not None
    assert result.winning.validation_result.passed is True


def test_run_tournament_empty_when_no_candidates_generated():
    bedrock = MagicMock()
    bedrock.invoke.return_value = ""  # Empty response
    runner = MagicMock()
    result = run_tournament(
        _make_evidence(), _make_root_cause(), bedrock, runner, candidate_count=1,
    )
    assert result.winning is None
    assert result.reason_if_empty == "no_candidates_generated"

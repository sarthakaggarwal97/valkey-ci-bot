"""Tests for scripts/stages/root_cause.py — RootCauseAnalyst + RootCauseCritic."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from scripts.models import (
    EvidencePack,
    InspectedFile,
    LogExcerpt,
    ParsedFailure,
    RejectionReason,
    RootCauseHypothesis,
)
from scripts.stages.root_cause import RootCauseAnalyst, RootCauseCritic, analyze


def _make_evidence() -> EvidencePack:
    return EvidencePack(
        failure_id="fp-1",
        run_id=1,
        job_ids=["job-1"],
        workflow="ci.yml",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="test_hash_race",
                test_name="test_hash_race",
                file_path="tests/unit/type/hash.tcl",
                error_message="Race detected",
                assertion_details=None,
                line_number=42,
                stack_trace=None,
                parser_type="tcl",
            )
        ],
        log_excerpts=[LogExcerpt(source="job-log", content="ERROR: race detected")],
        source_files_inspected=[InspectedFile(path="src/t_hash.c", reason="stack")],
        test_files_inspected=[InspectedFile(path="tests/unit/type/hash.tcl", reason="failing")],
        valkey_guidance_used=[],
        recent_commits=[],
        linked_urls=[],
        unknowns=[],
        built_at="2025-01-01T00:00:00Z",
    )


def _mock_bedrock(response_json: str | list[str]) -> MagicMock:
    """Create a mock Bedrock client returning the given response(s)."""
    mock = MagicMock()
    if isinstance(response_json, list):
        mock.invoke.side_effect = response_json
    else:
        mock.invoke.return_value = response_json
    return mock


# --- RootCauseAnalyst ---

def test_analyst_returns_hypotheses_from_model():
    bedrock = _mock_bedrock(json.dumps({
        "hypotheses": [
            {
                "summary": "race condition in dictResize",
                "causal_chain": ["dictResize called concurrently", "no lock held"],
                "evidence_refs": ["log:0", "file:src/t_hash.c"],
                "confidence": "high",
                "disconfirmed_alternatives": ["not a timeout"],
            }
        ]
    }))
    analyst = RootCauseAnalyst()
    hyps = analyst.propose(_make_evidence(), bedrock)
    assert len(hyps) == 1
    assert hyps[0].summary == "race condition in dictResize"
    assert hyps[0].confidence == "high"
    assert "log:0" in hyps[0].evidence_refs


def test_analyst_returns_empty_on_model_error():
    bedrock = MagicMock()
    bedrock.invoke.side_effect = RuntimeError("bedrock down")
    analyst = RootCauseAnalyst()
    hyps = analyst.propose(_make_evidence(), bedrock)
    assert hyps == []


def test_analyst_returns_empty_on_bad_json():
    bedrock = _mock_bedrock("not json at all")
    analyst = RootCauseAnalyst()
    hyps = analyst.propose(_make_evidence(), bedrock)
    assert hyps == []


# --- RootCauseCritic deterministic pre-check ---

def test_critic_rejects_all_when_no_evidence_refs():
    critic = RootCauseCritic()
    hyps = [
        RootCauseHypothesis(summary="h1", causal_chain=["a"], evidence_refs=[], confidence="high"),
        RootCauseHypothesis(summary="h2", causal_chain=["b"], evidence_refs=[], confidence="high"),
    ]
    bedrock = MagicMock()
    result = critic.judge(hyps, _make_evidence(), bedrock_client=bedrock)
    assert result.accepted is None
    assert result.rejection_reason == RejectionReason.THIN_EVIDENCE
    assert len(result.rejected) == 2
    # Model critic should NOT have been called — deterministic pre-check filtered all
    bedrock.invoke.assert_not_called()
    assert result.critic_verdict.model_critic_called is False


def test_critic_filters_bad_refs_but_keeps_good():
    critic = RootCauseCritic()
    ev = _make_evidence()
    hyps = [
        RootCauseHypothesis(
            summary="good", causal_chain=["a"],
            evidence_refs=["log:0"], confidence="high",
        ),
        RootCauseHypothesis(
            summary="bad", causal_chain=["b"],
            evidence_refs=[], confidence="high",
        ),
    ]
    # With only 1 passing, no model critic needed (uses first)
    result = critic.judge(hyps, ev, bedrock_client=None)
    assert result.accepted is not None
    assert result.accepted.summary == "good"
    assert len(result.rejected) == 1
    assert result.rejected[0].hypothesis.summary == "bad"


def test_critic_calls_model_when_multiple_hypotheses_pass():
    critic = RootCauseCritic()
    ev = _make_evidence()
    hyps = [
        RootCauseHypothesis(
            summary="h1", causal_chain=["a"],
            evidence_refs=["log:0"], confidence="high",
        ),
        RootCauseHypothesis(
            summary="h2", causal_chain=["b"],
            evidence_refs=["file:src/t_hash.c"], confidence="high",
        ),
    ]
    bedrock = _mock_bedrock(json.dumps({
        "accepted_index": 1,
        "rationale": "h2 has stronger evidence",
    }))
    result = critic.judge(hyps, ev, bedrock_client=bedrock)
    assert result.accepted is not None
    assert result.accepted.summary == "h2"
    assert result.critic_verdict.model_critic_called is True
    assert "h2" in result.critic_verdict.model_critic_rationale


def test_critic_falls_back_to_highest_confidence_if_model_rejects():
    critic = RootCauseCritic()
    ev = _make_evidence()
    hyps = [
        RootCauseHypothesis(
            summary="low_conf", causal_chain=["a"],
            evidence_refs=["log:0"], confidence="low",
        ),
        RootCauseHypothesis(
            summary="high_conf", causal_chain=["b"],
            evidence_refs=["file:src/t_hash.c"], confidence="high",
        ),
    ]
    bedrock = _mock_bedrock(json.dumps({"accepted_index": None, "rationale": "none strong enough"}))
    result = critic.judge(hyps, ev, bedrock_client=bedrock)
    # With accepted_index=None, fallback picks highest confidence
    assert result.accepted is not None
    assert result.accepted.summary == "high_conf"


def test_critic_confidence_gate_rejects_low():
    critic = RootCauseCritic()
    ev = _make_evidence()
    hyps = [
        RootCauseHypothesis(
            summary="h", causal_chain=["a"],
            evidence_refs=["log:0"], confidence="low",
        ),
    ]
    result = critic.judge(hyps, ev, bedrock_client=None, min_confidence="medium")
    assert result.accepted is None
    assert result.rejection_reason == RejectionReason.LOW_CONFIDENCE_ROOT_CAUSE


def test_critic_confidence_gate_allows_medium():
    critic = RootCauseCritic()
    ev = _make_evidence()
    hyps = [
        RootCauseHypothesis(
            summary="h", causal_chain=["a"],
            evidence_refs=["log:0"], confidence="medium",
        ),
    ]
    result = critic.judge(hyps, ev, bedrock_client=None, min_confidence="medium")
    assert result.accepted is not None
    assert result.rejection_reason is None


def test_critic_empty_hypotheses_returns_thin_evidence():
    critic = RootCauseCritic()
    result = critic.judge([], _make_evidence(), bedrock_client=None)
    assert result.accepted is None
    assert result.rejection_reason == RejectionReason.THIN_EVIDENCE


# --- analyze convenience ---

def test_analyze_end_to_end_happy_path():
    bedrock = MagicMock()
    # First call: analyst returns 1 hypothesis
    # No second call because only 1 passed (no critic needed)
    bedrock.invoke.return_value = json.dumps({
        "hypotheses": [
            {
                "summary": "race in hash",
                "causal_chain": ["step1"],
                "evidence_refs": ["log:0"],
                "confidence": "high",
                "disconfirmed_alternatives": [],
            }
        ]
    })
    result = analyze(_make_evidence(), bedrock, min_confidence="medium")
    assert result.accepted is not None
    assert result.accepted.summary == "race in hash"


def test_analyze_rejects_when_analyst_returns_nothing():
    bedrock = MagicMock()
    bedrock.invoke.return_value = "not json"
    result = analyze(_make_evidence(), bedrock)
    assert result.accepted is None
    assert result.rejection_reason == RejectionReason.THIN_EVIDENCE

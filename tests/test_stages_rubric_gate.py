"""Tests for RubricGate — combines deterministic + model rubric."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from scripts.models import (
    EvidencePack,
    InspectedFile,
    LogExcerpt,
    ParsedFailure,
)
from scripts.stages.rubric import RubricGate, check_does_not_mask_failure


def _make_evidence() -> EvidencePack:
    return EvidencePack(
        failure_id="fp-1", run_id=1, job_ids=["j1"], workflow="ci.yml",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="test_x", test_name="test_x",
                file_path="tests/unit/type/hash.tcl",
                error_message="assertion failed", assertion_details=None,
                line_number=42, stack_trace=None, parser_type="tcl",
            )
        ],
        log_excerpts=[LogExcerpt(source="log", content="FAIL")],
        source_files_inspected=[InspectedFile(path="src/t_hash.c", reason="x")],
        test_files_inspected=[InspectedFile(path="tests/unit/type/hash.tcl", reason="failing")],
        valkey_guidance_used=[], recent_commits=[],
        linked_urls=[], unknowns=[],
        built_at="2025-01-01T00:00:00Z",
    )


_GOOD_PATCH = """\
--- a/tests/unit/type/hash.tcl
+++ b/tests/unit/type/hash.tcl
@@ -40,3 +40,3 @@
 line1
-old line
+new line
 line3
"""

_GOOD_MSG = "Fix hash\n\nSigned-off-by: Dev <dev@example.com>"


# --- check_does_not_mask_failure ---

def test_mask_check_passes_when_model_says_not_masked():
    bedrock = MagicMock()
    bedrock.invoke.return_value = json.dumps({
        "masks": False, "rationale": "patch fixes the underlying race",
    })
    ev = _make_evidence()
    check = check_does_not_mask_failure(
        _GOOD_PATCH, ev, "assertion failed", bedrock,
    )
    assert check.passed is True
    assert "race" in check.detail


def test_mask_check_fails_when_model_says_masked():
    bedrock = MagicMock()
    bedrock.invoke.return_value = json.dumps({
        "masks": True, "rationale": "patch widens timeout to hide the race",
    })
    check = check_does_not_mask_failure(
        _GOOD_PATCH, _make_evidence(), "assertion failed", bedrock,
    )
    assert check.passed is False
    assert "timeout" in check.detail.lower()


def test_mask_check_fail_open_on_model_error():
    """Model errors should fail open (pass) rather than block PRs on transient errors."""
    bedrock = MagicMock()
    bedrock.invoke.side_effect = RuntimeError("bedrock down")
    check = check_does_not_mask_failure(
        _GOOD_PATCH, _make_evidence(), "assertion failed", bedrock,
    )
    assert check.passed is True
    assert "fail-open" in check.detail.lower()


def test_mask_check_fail_open_on_bad_json():
    bedrock = MagicMock()
    bedrock.invoke.return_value = "not valid json"
    check = check_does_not_mask_failure(
        _GOOD_PATCH, _make_evidence(), "assertion failed", bedrock,
    )
    assert check.passed is True


# --- RubricGate ---

def test_gate_deterministic_failure_short_circuits_model():
    """If deterministic checks fail, the model critic should NOT be called."""
    gate = RubricGate()
    bedrock = MagicMock()
    bedrock.invoke.return_value = json.dumps({"masks": False, "rationale": ""})
    ev = _make_evidence()
    # No DCO signoff → deterministic fail
    bad_msg = "Fix hash with no signoff"
    verdict = gate.judge(
        patch=_GOOD_PATCH, evidence=ev, commit_message=bad_msg,
        failing_assertion="fail", bedrock_client=bedrock,
    )
    assert verdict.overall_passed is False
    assert "dco_signoff" in verdict.blocking_checks
    # Model critic should NOT have been invoked
    bedrock.invoke.assert_not_called()
    # No model check in the verdict's checks
    assert all(c.kind == "deterministic" for c in verdict.checks)


def test_gate_all_deterministic_pass_runs_model():
    """When deterministic checks pass, the model critic should run."""
    gate = RubricGate()
    bedrock = MagicMock()
    bedrock.invoke.return_value = json.dumps({
        "masks": False, "rationale": "looks good",
    })
    ev = _make_evidence()
    verdict = gate.judge(
        patch=_GOOD_PATCH, evidence=ev, commit_message=_GOOD_MSG,
        is_bug_fix=False,  # skip test-required check
        failing_assertion="some failure",
        bedrock_client=bedrock,
    )
    # Model was called
    bedrock.invoke.assert_called_once()
    # Verdict includes the model check
    kinds = {c.kind for c in verdict.checks}
    assert "model" in kinds


def test_gate_model_says_masks_blocks_overall():
    gate = RubricGate()
    bedrock = MagicMock()
    bedrock.invoke.return_value = json.dumps({
        "masks": True, "rationale": "test was disabled",
    })
    ev = _make_evidence()
    verdict = gate.judge(
        patch=_GOOD_PATCH, evidence=ev, commit_message=_GOOD_MSG,
        is_bug_fix=False,
        failing_assertion="fail",
        bedrock_client=bedrock,
    )
    assert verdict.overall_passed is False
    assert "does_not_mask_failure" in verdict.blocking_checks


def test_gate_skips_model_when_no_bedrock_client():
    """Without a bedrock client, only deterministic checks run."""
    gate = RubricGate()
    ev = _make_evidence()
    verdict = gate.judge(
        patch=_GOOD_PATCH, evidence=ev, commit_message=_GOOD_MSG,
        is_bug_fix=False,
        failing_assertion="fail",
        bedrock_client=None,
    )
    assert all(c.kind == "deterministic" for c in verdict.checks)


def test_gate_skips_model_when_no_failing_assertion():
    gate = RubricGate()
    bedrock = MagicMock()
    ev = _make_evidence()
    verdict = gate.judge(
        patch=_GOOD_PATCH, evidence=ev, commit_message=_GOOD_MSG,
        is_bug_fix=False,
        failing_assertion="",  # no assertion provided
        bedrock_client=bedrock,
    )
    bedrock.invoke.assert_not_called()
    assert all(c.kind == "deterministic" for c in verdict.checks)

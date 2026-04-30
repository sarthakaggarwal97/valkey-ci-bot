"""Tests for scripts/ai_eval/scoring.py."""

from __future__ import annotations

from scripts.ai_eval.scoring import score_fix_patch, score_rejection, score_root_cause


# --- score_root_cause ---

def test_score_root_cause_all_keywords_match():
    result = score_root_cause(
        causal_chain=["race condition in dictResize", "no lock held"],
        expected_keywords=["race", "dictResize"],
    )
    assert result.passed is True
    assert result.score == 1.0


def test_score_root_cause_partial_match():
    result = score_root_cause(
        causal_chain=["race condition observed"],
        expected_keywords=["race", "dictResize", "lock"],
    )
    assert result.score == 1/3
    assert result.passed is False  # score < 0.5


def test_score_root_cause_case_insensitive():
    result = score_root_cause(
        causal_chain=["Race Condition"],
        expected_keywords=["RACE"],
    )
    assert result.score == 1.0


def test_score_root_cause_no_expected_keywords_passes():
    result = score_root_cause(causal_chain=[], expected_keywords=[])
    assert result.passed is True
    assert result.score == 1.0


# --- score_fix_patch ---

def test_score_fix_patch_passes_all_properties():
    patch = """--- a/src/x.c
+++ b/src/x.c
@@ -1,2 +1,3 @@
 existing
+new line
 other
--- a/tests/foo.tcl
+++ b/tests/foo.tcl
@@ -1 +1,2 @@
+test line
"""
    expected = {
        "min_patch_lines": 1,
        "max_patch_lines": 10,
        "must_touch_files": ["src/x.c"],
        "must_not_touch_files": ["src/unrelated.c"],
        "must_include_test": True,
    }
    result = score_fix_patch(patch, expected)
    assert result.passed is True
    assert result.score == 1.0


def test_score_fix_patch_fails_too_big():
    patch = "\n".join([f"+line{i}" for i in range(500)])
    expected = {"max_patch_lines": 100}
    result = score_fix_patch(patch, expected)
    assert result.passed is False
    assert any("Too many lines" in d for d in result.details)


def test_score_fix_patch_fails_missing_required_file():
    patch = """--- a/src/other.c
+++ b/src/other.c
@@ -1 +1 @@
-a
+b
"""
    expected = {"must_touch_files": ["src/needed.c"]}
    result = score_fix_patch(patch, expected)
    assert result.passed is False


def test_score_fix_patch_fails_missing_test():
    patch = """--- a/src/x.c
+++ b/src/x.c
@@ -1 +1 @@
-a
+b
"""
    result = score_fix_patch(patch, {"must_include_test": True})
    assert result.passed is False


def test_score_fix_patch_no_expectations_passes():
    result = score_fix_patch("any patch", {})
    assert result.passed is True
    assert result.score == 1.0


# --- score_rejection ---

def test_score_rejection_expected_none_actual_none_passes():
    result = score_rejection(actual_reason=None, expected_rejection=None)
    assert result.passed is True
    assert result.score == 1.0


def test_score_rejection_expected_none_but_rejected_fails():
    result = score_rejection(
        actual_reason="thin_evidence", expected_rejection=None,
    )
    assert result.passed is False


def test_score_rejection_expected_matches():
    result = score_rejection(
        actual_reason="thin_evidence",
        expected_rejection={"reason": "thin_evidence"},
    )
    assert result.passed is True


def test_score_rejection_expected_mismatches():
    result = score_rejection(
        actual_reason="rubric_failed",
        expected_rejection={"reason": "thin_evidence"},
    )
    assert result.passed is False

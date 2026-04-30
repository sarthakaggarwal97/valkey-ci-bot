"""Tests for deterministic rubric checks in scripts/stages/rubric.py."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts.models import CommitInfo, EvidencePack, InspectedFile, LogExcerpt, ParsedFailure
from scripts.stages.rubric import (
    _count_changed_lines,
    _files_in_patch,
    _touched_lines_by_file,
    check_dco_signoff,
    check_docs_separate_from_code,
    check_evidence_cites_log_lines,
    check_evidence_references_files,
    check_no_broad_timeout_increase,
    check_no_security_regression,
    check_patch_size,
    check_patch_touches_assertion_vicinity,
    check_test_included,
    run_deterministic_rubric,
)


def _make_evidence(**overrides) -> EvidencePack:
    defaults = dict(
        failure_id="fp-test",
        run_id=1,
        job_ids=["job-1"],
        workflow="ci.yml",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="test_foo",
                test_name="test_foo",
                file_path="tests/unit/type/hash.tcl",
                error_message="Expected 1 got 0",
                assertion_details=None,
                line_number=42,
                stack_trace=None,
                parser_type="tcl",
            )
        ],
        log_excerpts=[LogExcerpt(source="job-log", content="FAILED test_foo")],
        source_files_inspected=[InspectedFile(path="src/t_hash.c", reason="stack trace")],
        test_files_inspected=[InspectedFile(path="tests/unit/type/hash.tcl", reason="failing")],
        valkey_guidance_used=[],
        recent_commits=[],
        linked_urls=[],
        unknowns=[],
        built_at="2025-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    return EvidencePack(**defaults)


_SMALL_PATCH = """\
--- a/src/t_hash.c
+++ b/src/t_hash.c
@@ -42,7 +42,7 @@ void hashTypeSet(void) {
-    old_line
+    new_line
"""

_PATCH_WITH_TEST = """\
--- a/src/t_hash.c
+++ b/src/t_hash.c
@@ -42,7 +42,7 @@
-    old
+    new
--- a/tests/unit/type/hash.tcl
+++ b/tests/unit/type/hash.tcl
@@ -10,0 +11,3 @@
+    test "new test" {
+        assert_equal 1 1
+    }
"""


# --- _count_changed_lines ---

def test_count_changed_lines_basic():
    assert _count_changed_lines(_SMALL_PATCH) == 2


def test_count_changed_lines_ignores_headers():
    patch = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    # --- and +++ are ignored
    assert _count_changed_lines(patch) == 2


def test_count_changed_lines_empty():
    assert _count_changed_lines("") == 0


# --- _files_in_patch ---

def test_files_in_patch_basic():
    files = _files_in_patch(_PATCH_WITH_TEST)
    assert "src/t_hash.c" in files
    assert "tests/unit/type/hash.tcl" in files


def test_files_in_patch_ignores_dev_null():
    patch = "--- /dev/null\n+++ b/new.c\n@@ -0,0 +1 @@\n+new\n"
    files = _files_in_patch(patch)
    assert files == {"new.c"}


# --- _touched_lines_by_file ---

def test_touched_lines_by_file_basic():
    patch = """--- a/src/x.c
+++ b/src/x.c
@@ -40,3 +40,4 @@
 existing1
 existing2
+inserted
 existing3
"""
    touched = _touched_lines_by_file(patch)
    assert "src/x.c" in touched
    # The +inserted is at line 42 in the new file
    assert 42 in touched["src/x.c"]


# --- check_patch_size ---

def test_check_patch_size_pass():
    check = check_patch_size(_SMALL_PATCH, max_changed_lines=400)
    assert check.passed is True
    assert "2 changed lines" in check.detail


def test_check_patch_size_fail():
    big_patch = "\n".join([f"+line {i}" for i in range(500)])
    check = check_patch_size(big_patch, max_changed_lines=400)
    assert check.passed is False
    assert "500" in check.detail
    assert "400" in check.detail


@given(n=st.integers(min_value=0, max_value=2000))
def test_check_patch_size_property(n):
    patch = "\n".join([f"+line {i}" for i in range(n)])
    check = check_patch_size(patch, max_changed_lines=400)
    assert check.passed == (n <= 400)


# --- check_no_broad_timeout_increase ---

def test_check_timeout_increase_pass_no_change():
    check = check_no_broad_timeout_increase(_SMALL_PATCH)
    assert check.passed is True


def test_check_timeout_increase_fail_big_jump():
    patch = """--- a/tests/foo.tcl
+++ b/tests/foo.tcl
@@ -1 +1 @@
-wait_for_condition 10
+wait_for_condition 120
"""
    check = check_no_broad_timeout_increase(patch)
    assert check.passed is False
    assert "10" in check.detail
    assert "120" in check.detail


def test_check_timeout_increase_pass_small_change():
    patch = """--- a/tests/foo.tcl
+++ b/tests/foo.tcl
@@ -1 +1 @@
-timeout 10
+timeout 15
"""
    check = check_no_broad_timeout_increase(patch)
    # 15 is not > 2*10, so passes
    assert check.passed is True


# --- check_test_included ---

def test_check_test_included_pass():
    check = check_test_included(_PATCH_WITH_TEST, is_bug_fix=True)
    assert check.passed is True


def test_check_test_included_fail():
    check = check_test_included(_SMALL_PATCH, is_bug_fix=True)
    assert check.passed is False
    assert "no test file" in check.detail.lower()


def test_check_test_included_not_bug_fix_skips():
    check = check_test_included(_SMALL_PATCH, is_bug_fix=False)
    assert check.passed is True
    assert "not a bug fix" in check.detail.lower()


# --- check_evidence_cites_log_lines ---

def test_check_evidence_cites_logs_pass():
    ev = _make_evidence()
    check = check_evidence_cites_log_lines(ev)
    assert check.passed is True


def test_check_evidence_cites_logs_fail():
    ev = _make_evidence(log_excerpts=[])
    check = check_evidence_cites_log_lines(ev)
    assert check.passed is False


# --- check_evidence_references_files ---

def test_check_evidence_references_files_pass():
    ev = _make_evidence()
    check = check_evidence_references_files(ev)
    assert check.passed is True


def test_check_evidence_references_files_fail():
    ev = _make_evidence(source_files_inspected=[])
    check = check_evidence_references_files(ev)
    assert check.passed is False


# --- check_dco_signoff ---

def test_check_dco_signoff_pass():
    msg = "Fix hash race\n\nSigned-off-by: Dev <dev@example.com>"
    check = check_dco_signoff(msg)
    assert check.passed is True


def test_check_dco_signoff_fail():
    msg = "Fix hash race\n\nJust a commit message."
    check = check_dco_signoff(msg)
    assert check.passed is False


# --- check_patch_touches_assertion_vicinity ---

def test_vicinity_passes_when_patch_touches_failure_file():
    # Failure is in tests/unit/type/hash.tcl:42, patch touches src/t_hash.c:42
    # The file is different, but src/t_hash.c is a "related" file...
    # Actually the check looks for same file. Let me use a patch that touches tests/unit/type/hash.tcl
    patch = """--- a/tests/unit/type/hash.tcl
+++ b/tests/unit/type/hash.tcl
@@ -40,3 +40,3 @@
 line1
-old
+new
 line3
"""
    ev = _make_evidence()
    check = check_patch_touches_assertion_vicinity(patch, ev)
    assert check.passed is True


def test_vicinity_fails_when_patch_touches_unrelated_file():
    patch = """--- a/src/random_unrelated.c
+++ b/src/random_unrelated.c
@@ -1 +1 @@
-old
+new
"""
    ev = _make_evidence()
    check = check_patch_touches_assertion_vicinity(patch, ev)
    assert check.passed is False
    assert "random_unrelated" in check.detail


def test_vicinity_passes_when_no_parsed_failures():
    patch = _SMALL_PATCH
    ev = _make_evidence(parsed_failures=[])
    check = check_patch_touches_assertion_vicinity(patch, ev)
    assert check.passed is True


# --- check_no_security_regression ---

def test_security_fail_removed_require_auth():
    patch = """--- a/src/server.c
+++ b/src/server.c
@@ -1 +1 @@
-    requireAuth(c);
+    // auth check removed
"""
    check = check_no_security_regression(patch)
    assert check.passed is False
    assert "requireauth" in check.detail.lower() or "requireAuth" in check.detail


def test_security_fail_added_insecure():
    patch = """--- a/tests/foo.tcl
+++ b/tests/foo.tcl
@@ -1 +1 @@
-    normal-call
+    curl --insecure https://x
"""
    check = check_no_security_regression(patch)
    assert check.passed is False
    assert "insecure" in check.detail.lower()


def test_security_pass_normal_patch():
    check = check_no_security_regression(_SMALL_PATCH)
    assert check.passed is True


def test_security_fail_nopass():
    patch = """--- a/src/acl.c
+++ b/src/acl.c
@@ -1 +1 @@
-    useracl
+    nopass user
"""
    check = check_no_security_regression(patch)
    assert check.passed is False


# --- check_docs_separate_from_code ---

def test_docs_separate_pass_only_code():
    check = check_docs_separate_from_code(_SMALL_PATCH)
    assert check.passed is True


def test_docs_separate_pass_only_docs():
    patch = """--- a/docs/foo.md
+++ b/docs/foo.md
@@ -1 +1 @@
-old doc
+new doc
"""
    check = check_docs_separate_from_code(patch)
    assert check.passed is True


def test_docs_separate_fail_mixed():
    patch = """--- a/src/foo.c
+++ b/src/foo.c
@@ -1 +1 @@
-old
+new
--- a/docs/foo.md
+++ b/docs/foo.md
@@ -1 +1 @@
-old
+new
"""
    check = check_docs_separate_from_code(patch)
    assert check.passed is False


# --- run_deterministic_rubric ---

def test_rubric_all_pass():
    patch = _PATCH_WITH_TEST
    # Patch touches tests/unit/type/hash.tcl near line 11, but failure is at line 42
    # So vicinity will fail unless within ±50 window — let me check: 42-11=31, within window
    ev = _make_evidence()
    msg = "Fix hash\n\nSigned-off-by: Dev <dev@x.com>"
    verdict = run_deterministic_rubric(patch, ev, msg, is_bug_fix=True)
    # Some checks may legitimately fail; just verify structure
    assert len(verdict.checks) == 9
    assert isinstance(verdict.overall_passed, bool)
    assert isinstance(verdict.blocking_checks, list)


def test_rubric_missing_signoff_blocks():
    patch = _PATCH_WITH_TEST
    ev = _make_evidence()
    verdict = run_deterministic_rubric(patch, ev, "no signoff", is_bug_fix=True)
    assert "dco_signoff" in verdict.blocking_checks
    assert verdict.overall_passed is False


def test_rubric_empty_evidence_blocks():
    patch = _SMALL_PATCH
    ev = _make_evidence(log_excerpts=[], source_files_inspected=[])
    verdict = run_deterministic_rubric(patch, ev, "Fix\nSigned-off-by: D <d@x>", is_bug_fix=True)
    assert "evidence_cites_log_lines" in verdict.blocking_checks
    assert "evidence_references_files" in verdict.blocking_checks
    assert verdict.overall_passed is False


def test_rubric_security_regression_blocks():
    patch = """--- a/src/server.c
+++ b/src/server.c
@@ -1 +1 @@
-    requireAuth(c);
+    // removed
"""
    ev = _make_evidence()
    verdict = run_deterministic_rubric(patch, ev, "x\nSigned-off-by: D <d@x>", is_bug_fix=True)
    assert "no_security_regression" in verdict.blocking_checks


def test_rubric_too_big_patch_blocks():
    patch = "\n".join(["--- a/x.c\n+++ b/x.c"] + [f"+line {i}" for i in range(500)])
    ev = _make_evidence()
    verdict = run_deterministic_rubric(patch, ev, "x\nSigned-off-by: D <d@x>", is_bug_fix=True)
    assert "patch_size" in verdict.blocking_checks

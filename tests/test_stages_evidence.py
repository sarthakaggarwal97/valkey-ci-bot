"""Tests for scripts/stages/evidence.py."""

from __future__ import annotations

from scripts.models import ParsedFailure
from scripts.stages.evidence import (
    _excerpt_around,
    _extract_file_refs,
    build_for_ci_failure,
    build_for_pr_review,
)


# --- helpers ---

def test_extract_file_refs_finds_source_paths():
    text = "Error at src/t_hash.c:42 while processing tests/unit/type/hash.tcl"
    paths = _extract_file_refs(text)
    assert "src/t_hash.c" in paths
    assert "tests/unit/type/hash.tcl" in paths


def test_extract_file_refs_deduplicates():
    text = "src/server.c and src/server.c again"
    paths = _extract_file_refs(text)
    assert paths.count("src/server.c") == 1


def test_excerpt_around_basic():
    lines = ["l0", "l1", "l2", "l3", "l4", "l5", "l6"]
    content, start, end = _excerpt_around(lines, 3, before=1, after=2)
    assert content == "l2\nl3\nl4\nl5"
    assert start == 2
    assert end == 6


def test_excerpt_around_clamps_at_edges():
    lines = ["l0", "l1", "l2"]
    content, start, end = _excerpt_around(lines, 0, before=10, after=10)
    assert start == 0
    assert end == 3


# --- build_for_ci_failure ---

def test_build_ci_failure_basic():
    report = {
        "parsed_failures": [
            {
                "failure_identifier": "test_foo",
                "test_name": "test_foo",
                "file_path": "tests/unit/x.tcl",
                "error_message": "assertion failed",
                "assertion_details": None,
                "line_number": 10,
                "stack_trace": None,
                "parser_type": "tcl",
            }
        ]
    }
    ep = build_for_ci_failure(
        failure_id="fp-1", run_id=42, job_ids=["job-1"],
        workflow="ci.yml", failure_reports=[report],
        log_text="some log content\nFAILED test_foo here\nmore text",
    )
    assert ep.failure_id == "fp-1"
    assert ep.run_id == 42
    assert len(ep.parsed_failures) == 1
    assert ep.parsed_failures[0].test_name == "test_foo"
    # Log excerpt should include the FAIL line
    assert any("test_foo" in le.content for le in ep.log_excerpts)
    # Inspected files include tests/unit/x.tcl
    assert any(f.path == "tests/unit/x.tcl" for f in ep.test_files_inspected)


def test_build_ci_failure_missing_log_records_unknown():
    ep = build_for_ci_failure(
        failure_id="fp-1", run_id=1, job_ids=["j"],
        workflow="ci.yml", failure_reports=[],
        log_text=None,
    )
    assert "log_text_unavailable" in ep.unknowns
    assert "no_parsed_failures" in ep.unknowns


def test_build_ci_failure_validates_cleanly():
    ep = build_for_ci_failure(
        failure_id="fp-1", run_id=1, job_ids=["j"],
        workflow="ci.yml", failure_reports=[],
        log_text="fallback content\nline2\nline3",
    )
    ep.validate()  # must not raise


def test_build_ci_failure_separates_source_and_test_files():
    report = {
        "parsed_failures": [
            {
                "failure_identifier": "t1",
                "test_name": "t1",
                "file_path": "src/t_hash.c",
                "error_message": "err",
                "assertion_details": None,
                "line_number": 1,
                "stack_trace": None,
                "parser_type": "gtest",
            }
        ]
    }
    ep = build_for_ci_failure(
        failure_id="fp-1", run_id=1, job_ids=["j"],
        workflow="ci.yml", failure_reports=[report],
        log_text="error in tests/unit/hash.tcl here",
    )
    source_paths = [f.path for f in ep.source_files_inspected]
    test_paths = [f.path for f in ep.test_files_inspected]
    assert "src/t_hash.c" in source_paths
    assert "tests/unit/hash.tcl" in test_paths


def test_build_ci_failure_accepts_ParsedFailure_objects():
    pf = ParsedFailure(
        failure_identifier="t1", test_name="t1",
        file_path="src/a.c", error_message="e",
        assertion_details=None, line_number=1,
        stack_trace=None, parser_type="gtest",
    )
    report = {"parsed_failures": [pf]}
    ep = build_for_ci_failure(
        failure_id="fp-1", run_id=1, job_ids=["j"],
        workflow="ci.yml", failure_reports=[report],
    )
    assert len(ep.parsed_failures) == 1
    assert ep.parsed_failures[0].failure_identifier == "t1"


def test_build_ci_failure_includes_recent_commits():
    ep = build_for_ci_failure(
        failure_id="fp-1", run_id=1, job_ids=["j"],
        workflow="ci.yml", failure_reports=[],
        recent_commits=[
            {"sha": "abc", "message": "fix", "author": "dev"},
            {"sha": "def", "message": "test", "author": "dev", "files_changed": ["src/x.c"]},
        ],
    )
    assert len(ep.recent_commits) == 2
    assert ep.recent_commits[0].sha == "abc"
    assert ep.recent_commits[1].files_changed == ["src/x.c"]


# --- build_for_pr_review ---

def test_build_for_pr_review_basic():
    ep = build_for_pr_review(
        pr_number=123, diff="--- a/x\n+++ b/x\n",
        files_changed=["src/server.c", "tests/foo.tcl"],
    )
    assert ep.failure_id == "pr-123"
    assert ep.workflow == "pr-review"
    assert len(ep.source_files_inspected) == 1
    assert ep.source_files_inspected[0].path == "src/server.c"
    assert len(ep.test_files_inspected) == 1
    assert ep.test_files_inspected[0].path == "tests/foo.tcl"


def test_build_for_pr_review_truncates_long_diff():
    long_diff = "x" * 100_000
    ep = build_for_pr_review(
        pr_number=1, diff=long_diff, files_changed=["src/x.c"],
    )
    # Diff is truncated to 50000 chars
    assert len(ep.log_excerpts[0].content) <= 50_000


def test_build_for_pr_review_validates_cleanly():
    ep = build_for_pr_review(
        pr_number=1, diff="diff content", files_changed=["src/x.c"],
    )
    ep.validate()


def test_build_for_pr_review_includes_linked_url():
    ep = build_for_pr_review(
        pr_number=456, diff="diff", files_changed=[],
    )
    assert any("/pull/456" in url for url in ep.linked_urls)


def test_round_trip_through_to_dict():
    ep = build_for_ci_failure(
        failure_id="fp-1", run_id=1, job_ids=["j"],
        workflow="ci.yml", failure_reports=[],
        log_text="fallback",
    )
    from scripts.models import EvidencePack
    ep2 = EvidencePack.from_dict(ep.to_dict())
    assert ep2.failure_id == ep.failure_id
    assert ep2.workflow == ep.workflow
    assert len(ep2.log_excerpts) == len(ep.log_excerpts)

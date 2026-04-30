"""Tests for the improved log parsing pipeline.

Covers:
  - Timestamped [err]: format (the GitHub Actions line prefix)
  - [exception]: markers
  - *** [TIMEOUT]: runtest summary lines
  - Expanded _ERROR_MARKERS regex catches Valkey-specific failure tokens
  - _is_workflow_condition_only filters out step-skip evaluation noise
"""

from __future__ import annotations

import sys
import typing

# Python 3.7 typing.Protocol backfill so the test can run in older local envs.
# CI uses Python 3.11 where this is a no-op.
if not hasattr(typing, "Protocol"):
    try:
        from typing_extensions import Protocol as _Protocol
        typing.Protocol = _Protocol  # type: ignore[attr-defined]
    except ImportError:
        pass

import pytest

from scripts.log_parser import _ERROR_MARKERS, _extract_marker_excerpt
from scripts.parsers.tcl_parser import TclTestParser


# --- TclTestParser: handles timestamped [err]: ---

def test_tcl_parser_handles_timestamped_err():
    log = (
        "2026-03-24T00:42:18.1989111Z [ok]: PFCOUNT returns approximated cardinality (17 ms)\n"
        "2026-03-24T00:42:33.0783830Z [err]: HyperLogLog sparse encoding stress test in tests/unit/hyperloglog.tcl\n"
        "2026-03-24T00:42:33.0783831Z Expected '127.0.0.1:21185' to equal '1.2.3.4:21185'\n"
    )
    parser = TclTestParser()
    assert parser.can_parse(log)
    results = parser.parse(log)
    assert len(results) == 1
    assert results[0].test_name == "HyperLogLog sparse encoding stress test"
    assert results[0].file_path == "tests/unit/hyperloglog.tcl"
    assert "127.0.0.1" in (results[0].assertion_details or "")


def test_tcl_parser_handles_plain_err_without_timestamp():
    """Old-format (untimestamped) logs should still work."""
    log = "[err]: Some test failure in tests/unit/server.tcl\n"
    parser = TclTestParser()
    assert parser.can_parse(log)
    results = parser.parse(log)
    assert len(results) == 1
    assert results[0].test_name == "Some test failure"
    assert results[0].file_path == "tests/unit/server.tcl"


def test_tcl_parser_handles_exception_marker():
    log = (
        "2026-03-24T00:42:18.1989111Z [ok]: baseline test (10 ms)\n"
        "2026-03-24T00:42:33.0783830Z [exception]: Cluster test failed in tests/unit/cluster/basic.tcl\n"
    )
    parser = TclTestParser()
    assert parser.can_parse(log)
    results = parser.parse(log)
    assert len(results) == 1
    assert results[0].test_name == "Cluster test failed"
    assert results[0].file_path == "tests/unit/cluster/basic.tcl"
    assert "[exception]" in results[0].error_message.lower()


def test_tcl_parser_handles_timestamped_timeout_summary():
    log = (
        "2026-03-24T05:30:40.1194677Z *** [TIMEOUT]: Fix cluster migration "
        "in tests/unit/cluster/many-slot-migration.tcl\n"
    )
    parser = TclTestParser()
    assert parser.can_parse(log)
    results = parser.parse(log)
    assert len(results) == 1
    assert results[0].test_name == "Fix cluster migration"
    assert results[0].file_path == "tests/unit/cluster/many-slot-migration.tcl"
    assert "[TIMEOUT]" in results[0].error_message


def test_tcl_parser_drops_ms_suffix_from_err_description():
    """The (N ms) suffix some lines carry should not leak into the test name."""
    log = "[err]: Simple test case in tests/unit/foo.tcl (42 ms)\n"
    parser = TclTestParser()
    results = parser.parse(log)
    assert len(results) == 1
    assert results[0].test_name == "Simple test case"
    assert "42 ms" not in results[0].test_name


def test_tcl_parser_deduplicates_same_err_twice():
    log = (
        "[err]: A flaky test in tests/unit/foo.tcl\n"
        "[err]: A flaky test in tests/unit/foo.tcl\n"
    )
    parser = TclTestParser()
    results = parser.parse(log)
    assert len(results) == 1


def test_tcl_parser_no_match_on_ok_only_log():
    """A log of pure [ok]: lines should not be flagged as parseable."""
    log = "\n".join(
        [f"2026-03-24T00:42:{i:02d}Z [ok]: test {i} (10 ms)" for i in range(30)]
    )
    parser = TclTestParser()
    assert not parser.can_parse(log)
    assert parser.parse(log) == []


# --- _ERROR_MARKERS regex ---

def test_error_markers_catches_err_marker():
    assert _ERROR_MARKERS.search("something [err]: bad")


def test_error_markers_catches_exception_marker():
    assert _ERROR_MARKERS.search("[exception]: oh no")


def test_error_markers_catches_tcl_error():
    assert _ERROR_MARKERS.search("Tcl error: bad syntax")


def test_error_markers_catches_panic():
    assert _ERROR_MARKERS.search("panic: runtime error in goroutine 5")


def test_error_markers_catches_segfault():
    assert _ERROR_MARKERS.search("Segmentation fault (core dumped)")


def test_error_markers_catches_make_failure():
    assert _ERROR_MARKERS.search("make[1]: *** [Makefile:42: target] Error 2")


def test_error_markers_catches_undefined_reference():
    assert _ERROR_MARKERS.search("undefined reference to `foo'")


def test_error_markers_ignores_pass_lines():
    # [ok]: lines should not match
    assert not _ERROR_MARKERS.search("2026-03-24T00:42:18Z [ok]: test passes")


# --- _extract_marker_excerpt with new markers ---

def test_extract_marker_excerpt_finds_timestamped_err():
    lines = [
        f"2026-03-24T00:42:{i:02d}Z [ok]: baseline {i}" for i in range(40)
    ] + [
        "2026-03-24T00:45:00Z [err]: Failing test in tests/unit/foo.tcl",
        "2026-03-24T00:45:00Z Expected 1 to equal 2",
    ] + [
        f"2026-03-24T00:46:{i:02d}Z [ok]: trailing {i}" for i in range(20)
    ]
    excerpt = _extract_marker_excerpt(lines, limit=200)
    assert excerpt is not None
    assert "[err]:" in excerpt
    assert "Failing test" in excerpt


# --- _is_workflow_condition_only ---

def test_workflow_condition_only_detects_pure_evaluation_log():
    from scripts.log_parser import is_workflow_condition_only as _is_workflow_condition_only
    log = (
        "2026-03-14T00:17:01.88Z Evaluating test-fedorarawhide-tls-module-no-tls.if\n"
        "2026-03-14T00:17:01.88Z Evaluating: (success() && ((github.event_name == 'workflow_call')))\n"
    )
    assert _is_workflow_condition_only(log) is True


def test_workflow_condition_only_allows_real_log():
    from scripts.log_parser import is_workflow_condition_only as _is_workflow_condition_only
    log = (
        "2026-03-24T00:42:18Z [ok]: baseline (10 ms)\n"
        "2026-03-24T00:42:33Z [err]: real failure in tests/unit/foo.tcl\n"
        "2026-03-24T00:42:33Z Expected 1 to equal 2\n"
    )
    assert _is_workflow_condition_only(log) is False


def test_workflow_condition_only_handles_empty_log():
    from scripts.log_parser import is_workflow_condition_only as _is_workflow_condition_only
    assert _is_workflow_condition_only("") is True
    assert _is_workflow_condition_only("   \n  \n ") is True


def test_workflow_condition_only_partial_eval_allows_through():
    """A log with just one evaluation line plus real failure content must pass."""
    from scripts.log_parser import is_workflow_condition_only as _is_workflow_condition_only
    log = (
        "2026-03-24T00:42:18Z Evaluating: (success() && (github.event_name == 'push'))\n"
        "2026-03-24T00:42:33Z [err]: real failure in tests/unit/foo.tcl\n"
        "2026-03-24T00:42:33Z Expected 1 to equal 2\n"
        "2026-03-24T00:42:33Z Process completed with exit code 2\n"
    )
    assert _is_workflow_condition_only(log) is False

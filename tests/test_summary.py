"""Tests for scripts/summary.py — WorkflowSummary."""

from __future__ import annotations

import os
import tempfile

from scripts.summary import (
    ApprovalCandidate,
    ApprovalSummary,
    FuzzerRunSummaryRow,
    FuzzerWorkflowSummary,
    WorkflowSummary,
)


class TestWorkflowSummaryRender:
    """Unit tests for rendering the markdown summary."""

    def test_empty_results(self):
        summary = WorkflowSummary(mode="analyze")
        md = summary.render()
        assert "analyze run" in md
        assert "No failures processed." in md

    def test_single_result(self):
        summary = WorkflowSummary(mode="analyze")
        summary.add_result("build-linux", "TestFoo.Bar", "pr-created")
        md = summary.render()
        assert "**1** failure(s) processed" in md
        assert "**0** error(s)" in md
        assert "build-linux" in md
        assert "TestFoo.Bar" in md
        assert "pr-created" in md

    def test_multiple_results_with_errors(self):
        summary = WorkflowSummary(mode="reconcile")
        summary.add_result("job-a", "id-a", "pr-created")
        summary.add_result("job-b", "id-b", "error", error="Bedrock timeout")
        md = summary.render()
        assert "reconcile run" in md
        assert "**2** failure(s) processed" in md
        assert "**1** error(s)" in md
        assert "Bedrock timeout" in md

    def test_table_headers_present(self):
        summary = WorkflowSummary()
        summary.add_result("j", "f", "ok")
        md = summary.render()
        assert "| Job |" in md
        assert "| Failure |" in md
        assert "| Outcome |" in md
        assert "| Error |" in md

    def test_error_cell_empty_when_no_error(self):
        summary = WorkflowSummary()
        summary.add_result("j", "f", "skipped")
        md = summary.render()
        # The error column should be empty (just "| |" at the end)
        lines = [l for l in md.splitlines() if l.startswith("| j")]
        assert len(lines) == 1
        assert lines[0].endswith("|")


class TestWorkflowSummaryWrite:
    """Tests for writing to GITHUB_STEP_SUMMARY."""

    def test_write_returns_rendered_markdown(self):
        summary = WorkflowSummary(mode="analyze")
        summary.add_result("job-x", "test-1", "pr-created")
        md = summary.write()
        assert "job-x" in md
        assert "pr-created" in md

    def test_write_to_step_summary_file(self, monkeypatch, tmp_path):
        summary_file = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

        summary = WorkflowSummary(mode="analyze")
        summary.add_result("build", "compile-error", "analysis-failed")
        md = summary.write()

        content = summary_file.read_text()
        assert content == md
        assert "build" in content

    def test_write_appends_to_existing_file(self, monkeypatch, tmp_path):
        summary_file = tmp_path / "summary.md"
        summary_file.write_text("# Existing content\n")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

        summary = WorkflowSummary()
        summary.add_result("j", "f", "ok")
        summary.write()

        content = summary_file.read_text()
        assert content.startswith("# Existing content\n")
        assert "| j |" in content

    def test_write_without_env_var_still_returns_md(self, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        summary = WorkflowSummary()
        summary.add_result("j", "f", "ok")
        md = summary.write()
        assert "| j |" in md


# ---------------------------------------------------------------------------
# PRSummaryComment tests (Requirement 11.2)
# ---------------------------------------------------------------------------

from scripts.summary import PRSummaryComment, StepTiming


class TestPRSummaryCommentRender:
    """Unit tests for PRSummaryComment.render()."""

    def test_empty_steps(self):
        comment = PRSummaryComment()
        md = comment.render()
        assert "Processing Summary" in md
        assert "Fix generation retries:** 0" in md
        assert "Validation retries:** 0" in md

    def test_single_step(self):
        comment = PRSummaryComment()
        comment.add_step("detection", duration_seconds=1.5, status="completed")
        md = comment.render()
        assert "| detection | 1.5s | completed |" in md

    def test_multiple_steps(self):
        comment = PRSummaryComment()
        comment.add_step("detection", 0.3)
        comment.add_step("parsing", 0.5)
        comment.add_step("analysis", 12.1)
        comment.add_step("generation", 8.4)
        comment.add_step("validation", 45.0)
        comment.add_step("pr_creation", 2.0)
        md = comment.render()
        assert "detection" in md
        assert "parsing" in md
        assert "analysis" in md
        assert "generation" in md
        assert "validation" in md
        assert "pr_creation" in md

    def test_retries_shown(self):
        comment = PRSummaryComment(fix_retries=2, validation_retries=1)
        comment.add_step("generation", 5.0)
        md = comment.render()
        assert "Fix generation retries:** 2" in md
        assert "Validation retries:** 1" in md

    def test_total_time_from_explicit_value(self):
        comment = PRSummaryComment(total_duration_seconds=99.9)
        comment.add_step("detection", 1.0)
        md = comment.render()
        assert "Total time:** 99.9s" in md

    def test_total_time_computed_from_steps(self):
        comment = PRSummaryComment()
        comment.add_step("detection", 1.0)
        comment.add_step("parsing", 2.0)
        md = comment.render()
        assert "Total time:** 3.0s" in md

    def test_step_with_failed_status(self):
        comment = PRSummaryComment()
        comment.add_step("validation", 10.0, status="failed")
        md = comment.render()
        assert "| validation | 10.0s | failed |" in md

    def test_table_headers_present(self):
        comment = PRSummaryComment()
        comment.add_step("detection", 1.0)
        md = comment.render()
        assert "| Step |" in md
        assert "| Duration |" in md
        assert "| Status |" in md


class TestApprovalSummary:
    def test_empty_summary_is_blank(self):
        summary = ApprovalSummary()
        assert summary.render() == ""

    def test_renders_candidate_details(self):
        summary = ApprovalSummary()
        summary.add_candidate(
            ApprovalCandidate(
                job_name="test-ubuntu-jemalloc",
                failure_identifier="TestSuite.TestCase",
                workflow_run_url="https://github.com/valkey-io/valkey/actions/runs/1",
                confidence="high",
                is_flaky=False,
                failure_streak=2,
                total_failure_observations=3,
                last_known_good_sha="abcdef1234567890",
                first_bad_sha="fedcba0987654321",
                files_to_change=["src/server.c"],
                rationale="The failure reproduces after a null check regression.",
            )
        )

        md = summary.render()

        assert "Approval Queue" in md
        assert "test-ubuntu-jemalloc" in md
        assert "TestSuite.TestCase" in md
        assert "abcdef123456" in md
        assert "fedcba098765" in md
        assert "src/server.c" in md


class TestFuzzerWorkflowSummary:
    def test_empty_summary_is_not_blank(self):
        summary = FuzzerWorkflowSummary()
        md = summary.render()
        assert "Valkey Fuzzer Analysis" in md
        assert "No fuzzer runs analyzed." in md

    def test_renders_rows(self):
        summary = FuzzerWorkflowSummary()
        summary.add_row(
            FuzzerRunSummaryRow(
                run_id=123,
                run_url="https://github.com/valkey-io/valkey-fuzzer/actions/runs/123",
                conclusion="failure",
                overall_status="anomalous",
                scenario_id="839534793",
                seed="839534793",
                anomaly_count=2,
                normal_signal_count=1,
                summary="Slot coverage failed after chaos.",
                reproduction_hint="valkey-fuzzer cluster --seed 839534793",
            )
        )

        md = summary.render()

        assert "Run 123" in md
        assert "anomalous" in md
        assert "839534793" in md
        assert "Slot coverage failed after chaos." in md

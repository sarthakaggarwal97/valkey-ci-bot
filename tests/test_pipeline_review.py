"""Tests for scripts/pipeline_review.py — PR review orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts.models import ReviewFinding
from scripts.pipeline_review import _gate_findings, process_pr_review
from scripts.stages.evidence import build_for_pr_review


def _ev():
    return build_for_pr_review(
        pr_number=1, diff="diff", files_changed=["src/x.c", "tests/x.tcl"],
    )


# --- _gate_findings ---

def test_gate_dedupes_findings():
    ev = _ev()
    findings = [
        ReviewFinding(path="src/x.c", line=10, body="a", severity="high", title="T1"),
        ReviewFinding(path="src/x.c", line=10, body="b", severity="high", title="T1"),
    ]
    gated, verdict = _gate_findings(findings, ev)
    assert len(gated) == 1


def test_gate_drops_findings_not_in_inspected_files():
    ev = _ev()
    findings = [
        ReviewFinding(path="src/x.c", line=1, body="ok", severity="low", title="A"),
        ReviewFinding(path="src/other.c", line=1, body="bad", severity="low", title="B"),
    ]
    gated, _ = _gate_findings(findings, ev)
    assert len(gated) == 1
    assert gated[0].path == "src/x.c"


def test_gate_respects_max_comments():
    ev = _ev()
    findings = [
        ReviewFinding(path="src/x.c", line=i, body=f"b{i}", severity="low", title=f"T{i}")
        for i in range(50)
    ]
    gated, verdict = _gate_findings(findings, ev, max_comments=5)
    assert len(gated) == 5


# --- process_pr_review ---

def test_process_pr_review_happy_path():
    code_reviewer = MagicMock()
    code_reviewer.review.return_value = [
        ReviewFinding(path="src/x.c", line=1, body="finding", severity="medium", title="F1"),
    ]
    publisher = MagicMock()
    outcome = process_pr_review(
        pr_number=1, diff="diff", files_changed=["src/x.c"],
        code_reviewer=code_reviewer, comment_publisher=publisher,
    )
    assert outcome.published is True
    assert outcome.gated_findings is not None
    assert len(outcome.gated_findings) == 1


def test_process_pr_review_handles_reviewer_error():
    code_reviewer = MagicMock()
    code_reviewer.review.side_effect = RuntimeError("reviewer crashed")
    outcome = process_pr_review(
        pr_number=1, diff="diff", files_changed=["src/x.c"],
        code_reviewer=code_reviewer,
    )
    assert outcome.error is not None
    assert "Review failed" in outcome.error


def test_process_pr_review_without_publisher_doesnt_publish():
    code_reviewer = MagicMock()
    code_reviewer.review.return_value = []
    outcome = process_pr_review(
        pr_number=1, diff="diff", files_changed=["src/x.c"],
        code_reviewer=code_reviewer,
    )
    assert outcome.published is False
    assert outcome.error is None


def test_process_pr_review_handles_dict_findings():
    code_reviewer = MagicMock()
    code_reviewer.review.return_value = [
        {"path": "src/x.c", "line": 1, "body": "b", "severity": "medium", "title": "T"},
    ]
    outcome = process_pr_review(
        pr_number=1, diff="diff", files_changed=["src/x.c"],
        code_reviewer=code_reviewer,
    )
    assert outcome.findings is not None
    assert len(outcome.findings) == 1
    assert isinstance(outcome.findings[0], ReviewFinding)


def test_process_pr_review_uses_adapter_when_config_supplied():
    """With a config, pipeline_review should call reviewer with the legacy
    (PullRequestContext, DiffScope, config) shape, not (EvidencePack,)."""
    from scripts.config import ReviewerConfig
    code_reviewer = MagicMock()
    code_reviewer.review.return_value = [
        ReviewFinding(
            path="src/x.c", line=1, body="b", severity="medium", title="T",
        ),
    ]
    config = ReviewerConfig()
    outcome = process_pr_review(
        pr_number=5, diff="diff", files_changed=["src/x.c"],
        code_reviewer=code_reviewer, config=config,
    )
    # reviewer.review was called with 3 positional args
    call = code_reviewer.review.call_args
    args = call[0]
    assert len(args) == 3
    from scripts.models import DiffScope, PullRequestContext
    assert isinstance(args[0], PullRequestContext)
    assert isinstance(args[1], DiffScope)
    assert args[0].number == 5

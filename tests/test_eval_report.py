from __future__ import annotations

from scripts.eval.flow_scorer import FlowScore
from scripts.eval.report import render_report


def test_render_report_produces_markdown():
    scores = [
        FlowScore(flow="review", fixture_name="pr-123", correctness=0.8, style=0.9),
        FlowScore(flow="review", fixture_name="pr-456", correctness=0.3, style=1.0),
        FlowScore(flow="daily", fixture_name="run-789", correctness=0.6, style=1.0),
    ]
    report = render_report(scores)
    assert "# CI Agent Eval Report" in report
    assert "| review |" in report
    assert "| daily |" in report
    assert "pr-456" in report


def test_render_report_empty():
    report = render_report([])
    assert "# CI Agent Eval Report" in report

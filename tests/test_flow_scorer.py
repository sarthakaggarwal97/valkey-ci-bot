from __future__ import annotations

from scripts.eval.flow_scorer import (
    score_backport_flow,
    score_daily_flow,
    score_fuzzer_flow,
    score_review_flow,
)


def test_review_perfect_match():
    agent = [{"path": "src/a.c", "line": 42, "body": "Missing NULL check."}]
    maint = [{"path": "src/a.c", "line": 44, "body": "Check for NULL."}]
    score = score_review_flow("test", agent, maint, line_tolerance=5)
    assert score.correctness == 1.0
    assert score.details["true_positives"] == 1


def test_review_no_overlap():
    agent = [{"path": "src/a.c", "line": 10, "body": "x"}]
    maint = [{"path": "src/b.c", "line": 20, "body": "y"}]
    score = score_review_flow("test", agent, maint)
    assert score.correctness == 0.0
    assert score.details["false_positives"] == 1
    assert score.details["missed"] == 1


def test_review_empty_maintainer():
    agent = [{"path": "src/a.c", "line": 10, "body": "x"}]
    score = score_review_flow("test", agent, [])
    assert score.correctness == 0.5


def test_daily_jaccard_and_flaky():
    rca = {"files_to_change": ["src/a.c", "src/b.c"], "is_flaky": True}
    truth = {"fix_files_changed": ["src/a.c", "src/c.c"], "is_flaky": True}
    score = score_daily_flow("test", rca, truth)
    assert score.details["jaccard"] > 0
    assert score.details["flaky_match"] is True


def test_daily_no_ground_truth():
    score = score_daily_flow("test", {"files_to_change": ["x"]}, {"fix_files_changed": []})
    assert score.correctness == 0.5


def test_backport_empty_file_fails():
    agent = {"files_changed": ["src/a.c"], "file_details": [{"path": "src/a.c", "size": 0}]}
    truth = {"files_changed": ["src/a.c"]}
    score = score_backport_flow("test", agent, truth)
    assert score.correctness == 0.0
    assert score.details["has_empty_files"] is True


def test_backport_perfect_match():
    agent = {"files_changed": ["src/a.c", "src/b.c"], "file_details": []}
    truth = {"files_changed": ["src/a.c", "src/b.c"]}
    score = score_backport_flow("test", agent, truth)
    assert score.correctness == 1.0


def test_fuzzer_category_match():
    agent = {"root_cause_category": "cluster-split-brain", "files_involved": ["src/cluster.c"]}
    truth = {"root_cause_category": "cluster", "fix_files_changed": ["src/cluster.c"]}
    score = score_fuzzer_flow("test", agent, truth)
    assert score.correctness > 0.5

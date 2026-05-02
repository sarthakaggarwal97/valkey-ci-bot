from __future__ import annotations

from scripts.eval.eval_scorer import score_findings, score_style


def test_score_findings_perfect_match():
    findings = [{"path": "src/a.c"}, {"path": "src/b.c"}]
    score = score_findings(findings, ["src/a.c", "src/b.c"])
    assert score.precision == 1.0
    assert score.recall == 1.0
    assert score.false_positives == 0
    assert score.missed == 0


def test_score_findings_with_false_positives():
    findings = [{"path": "src/a.c"}, {"path": "src/x.c"}]
    score = score_findings(findings, ["src/a.c", "src/b.c"])
    assert score.precision == 0.5
    assert score.recall == 0.5
    assert score.false_positives == 1
    assert score.missed == 1


def test_score_findings_empty():
    score = score_findings([], ["src/a.c"])
    assert score.precision == 0.0
    assert score.recall == 0.0
    assert score.missed == 1


def test_score_style_clean():
    findings = [{"body": "This leaks memory on the error path."}]
    assert score_style(findings) == 1.0


def test_score_style_forensic():
    findings = [
        {"body": "The diff shows +0/-5272 lines removed."},
        {"body": "I ran git cat-file and confirmed 0 bytes."},
        {"body": "Missing NULL check after zmalloc."},
    ]
    assert score_style(findings) < 0.5


def test_score_style_empty():
    assert score_style([]) == 1.0

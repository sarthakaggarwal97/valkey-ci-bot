from __future__ import annotations

import re
from dataclasses import dataclass

_FORENSIC_PATTERNS = [
    re.compile(r"\bwc -l\b", re.IGNORECASE),
    re.compile(r"\bgit cat-file\b", re.IGNORECASE),
    re.compile(r"\bthe diff shows\b", re.IGNORECASE),
    re.compile(r"\bI ran\b", re.IGNORECASE),
    re.compile(r"\b\d+ bytes on disk\b", re.IGNORECASE),
    re.compile(r"diff \+\d+/-\d+", re.IGNORECASE),
]


@dataclass
class EvalScore:
    precision: float  # fraction of agent findings that are real
    recall: float     # fraction of real issues found
    false_positives: int
    true_positives: int
    missed: int
    style_score: float  # 0-1, higher = more human-like
    total_findings: int


def score_findings(
    agent_findings: list[dict],
    expected_paths: list[str],
) -> EvalScore:
    agent_paths = {f.get("path", "") for f in agent_findings}
    expected_set = set(expected_paths)
    tp = len(agent_paths & expected_set)
    fp = len(agent_paths - expected_set)
    missed = len(expected_set - agent_paths)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + missed) if (tp + missed) > 0 else 0.0
    return EvalScore(
        precision=round(precision, 3),
        recall=round(recall, 3),
        false_positives=fp,
        true_positives=tp,
        missed=missed,
        style_score=0.0,  # filled by score_style
        total_findings=len(agent_findings),
    )


def score_style(findings: list[dict]) -> float:
    if not findings:
        return 1.0
    forensic_count = 0
    for f in findings:
        body = f.get("body", "")
        if any(p.search(body) for p in _FORENSIC_PATTERNS):
            forensic_count += 1
    return round(1.0 - (forensic_count / len(findings)), 3)

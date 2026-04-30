"""Deterministic scorers for the AI eval harness."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScoringResult:
    """Result of scoring one fixture against expectations."""

    fixture_id: str
    scorer: str
    passed: bool
    score: float  # 0.0 to 1.0
    details: list[str] = field(default_factory=list)


def score_root_cause(
    causal_chain: list[str],
    expected_keywords: list[str],
    fixture_id: str = "",
) -> ScoringResult:
    """Score root cause by keyword match in the causal chain."""
    if not expected_keywords:
        return ScoringResult(fixture_id=fixture_id, scorer="root_cause", passed=True, score=1.0)

    chain_text = " ".join(causal_chain).lower()
    matched = [kw for kw in expected_keywords if kw.lower() in chain_text]
    score = len(matched) / len(expected_keywords) if expected_keywords else 1.0
    return ScoringResult(
        fixture_id=fixture_id, scorer="root_cause",
        passed=score >= 0.5,
        score=score,
        details=[f"Matched {len(matched)}/{len(expected_keywords)}: {matched}"],
    )


def score_fix_patch(
    patch: str,
    expected: dict,
    fixture_id: str = "",
) -> ScoringResult:
    """Score a fix patch against expected properties."""
    details: list[str] = []
    checks_passed = 0
    total_checks = 0

    # Count changed lines
    changed = sum(
        1 for line in patch.splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )

    if "min_patch_lines" in expected:
        total_checks += 1
        if changed >= expected["min_patch_lines"]:
            checks_passed += 1
        else:
            details.append(f"Too few lines: {changed} < {expected['min_patch_lines']}")

    if "max_patch_lines" in expected:
        total_checks += 1
        if changed <= expected["max_patch_lines"]:
            checks_passed += 1
        else:
            details.append(f"Too many lines: {changed} > {expected['max_patch_lines']}")

    if "must_touch_files" in expected:
        import re
        files = set(re.findall(r"^(?:---|\+\+\+) [ab]/(.+)$", patch, re.MULTILINE))
        for f in expected["must_touch_files"]:
            total_checks += 1
            if f in files:
                checks_passed += 1
            else:
                details.append(f"Missing expected file: {f}")

    if "must_not_touch_files" in expected:
        import re
        files = set(re.findall(r"^(?:---|\+\+\+) [ab]/(.+)$", patch, re.MULTILINE))
        for f in expected["must_not_touch_files"]:
            total_checks += 1
            if f not in files:
                checks_passed += 1
            else:
                details.append(f"Touched forbidden file: {f}")

    if expected.get("must_include_test"):
        total_checks += 1
        import re
        files = set(re.findall(r"^(?:---|\+\+\+) [ab]/(.+)$", patch, re.MULTILINE))
        if any(f.startswith("tests/") for f in files):
            checks_passed += 1
        else:
            details.append("No test file in patch")

    score = checks_passed / total_checks if total_checks else 1.0
    return ScoringResult(
        fixture_id=fixture_id, scorer="fix_patch",
        passed=score >= 0.8, score=score, details=details,
    )


def score_rejection(
    actual_reason: str | None,
    expected_rejection: dict | None,
    fixture_id: str = "",
) -> ScoringResult:
    """Score whether the pipeline correctly rejected (or didn't reject)."""
    if expected_rejection is None:
        passed = actual_reason is None
        return ScoringResult(
            fixture_id=fixture_id, scorer="rejection",
            passed=passed, score=1.0 if passed else 0.0,
            details=["Expected no rejection" + (f" but got {actual_reason}" if actual_reason else "")],
        )

    expected_reason = expected_rejection.get("reason", "")
    passed = actual_reason == expected_reason
    return ScoringResult(
        fixture_id=fixture_id, scorer="rejection",
        passed=passed, score=1.0 if passed else 0.0,
        details=[f"Expected {expected_reason}, got {actual_reason}"],
    )

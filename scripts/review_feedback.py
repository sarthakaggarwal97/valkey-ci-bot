"""PR review feedback loop: tracks finding resolutions to calibrate confidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ReviewFinding:
    """A single review finding with resolution tracking."""

    finding_id: str
    file_path: str
    line: int
    severity: str
    confidence: str
    was_resolved: Optional[bool] = None
    resolution_type: str = "pending"


class FeedbackTracker:
    """Tracks review finding outcomes to calibrate confidence thresholds."""

    def __init__(self) -> None:
        self._findings: dict[int, list[ReviewFinding]] = {}

    def record_finding(self, pr_number: int, finding: ReviewFinding) -> None:
        """Store a finding for a PR."""
        self._findings.setdefault(pr_number, []).append(finding)

    def record_resolution(
        self, pr_number: int, finding_id: str, resolution_type: str
    ) -> None:
        """Mark how a finding was resolved."""
        for f in self._findings.get(pr_number, []):
            if f.finding_id == finding_id:
                f.resolution_type = resolution_type
                f.was_resolved = resolution_type in ("fixed", "outdated")
                break

    def get_accuracy_stats(self) -> dict:
        """Return aggregate resolution statistics."""
        all_findings = [f for flist in self._findings.values() for f in flist]
        total = len(all_findings)
        resolved = sum(1 for f in all_findings if f.resolution_type == "fixed")
        dismissed = sum(1 for f in all_findings if f.resolution_type == "dismissed")
        pending = sum(1 for f in all_findings if f.resolution_type == "pending")
        actionable = resolved + dismissed
        return {
            "total": total,
            "resolved": resolved,
            "dismissed": dismissed,
            "pending": pending,
            "precision_rate": resolved / actionable if actionable else 0.0,
        }

    def get_confidence_calibration(self) -> dict[str, dict]:
        """Return per-confidence-level resolution stats."""
        buckets: dict[str, list[ReviewFinding]] = {}
        for flist in self._findings.values():
            for f in flist:
                buckets.setdefault(f.confidence, []).append(f)

        result: dict[str, dict] = {}
        for level, findings in buckets.items():
            resolved = sum(1 for f in findings if f.resolution_type == "fixed")
            dismissed = sum(1 for f in findings if f.resolution_type == "dismissed")
            pending = sum(1 for f in findings if f.resolution_type == "pending")
            actionable = resolved + dismissed
            result[level] = {
                "total": len(findings),
                "resolved": resolved,
                "dismissed": dismissed,
                "pending": pending,
                "precision_rate": resolved / actionable if actionable else 0.0,
            }
        return result

"""PR-review pipeline orchestrator — wires evidence-first stages for reviews.

Routes PR reviews through EvidencePack + rubric gating.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from scripts.models import EvidencePack, ReviewFinding, RubricCheck, RubricVerdict
from scripts.stages.evidence import build_for_pr_review

logger = logging.getLogger(__name__)


@dataclass
class ReviewPipelineOutcome:
    """Result of processing one PR through the review pipeline."""

    pr_number: int
    evidence: EvidencePack | None = None
    findings: list[ReviewFinding] | None = None
    gated_findings: list[ReviewFinding] | None = None
    published: bool = False
    error: str | None = None


def _gate_findings(
    findings: list[ReviewFinding],
    evidence: EvidencePack,
    max_comments: int = 25,
) -> tuple[list[ReviewFinding], RubricVerdict]:
    """Apply deterministic gates to review findings."""
    checks: list[RubricCheck] = []
    gated: list[ReviewFinding] = []
    seen: set[str] = set()

    for f in findings:
        # Dedup by file+line+title
        key = f"{f.path}:{f.line}:{f.title}"
        if key in seen:
            continue
        seen.add(key)

        # Must cite inspected files
        all_paths = {sf.path for sf in evidence.source_files_inspected}
        all_paths |= {tf.path for tf in evidence.test_files_inspected}
        if f.path not in all_paths and not any(f.path.endswith(p) for p in all_paths):
            checks.append(RubricCheck(
                name="finding_cites_file", kind="deterministic", passed=False,
                detail=f"Finding on {f.path} not in inspected files",
            ))
            continue

        gated.append(f)
        if len(gated) >= max_comments:
            break

    checks.append(RubricCheck(
        name="max_comments", kind="deterministic",
        passed=len(gated) <= max_comments,
        detail=f"{len(gated)} findings (max {max_comments})",
    ))

    blocking = [c.name for c in checks if not c.passed]
    verdict = RubricVerdict(
        checks=checks, overall_passed=len(blocking) == 0,
        blocking_checks=blocking,
    )
    return gated, verdict


def process_pr_review(
    *,
    pr_number: int,
    diff: str,
    files_changed: list[str],
    pr_title: str = "",
    pr_body: str = "",
    code_reviewer: Any | None = None,
    comment_publisher: Any | None = None,
    config: Any | None = None,
    recent_commits: list[dict[str, Any]] | None = None,
) -> ReviewPipelineOutcome:
    """Process one PR through the evidence-first review pipeline."""
    # Stage 0: Evidence
    try:
        evidence = build_for_pr_review(
            pr_number=pr_number, diff=diff, files_changed=files_changed,
            pr_title=pr_title, pr_body=pr_body,
            recent_commits=recent_commits,
        )
    except Exception as exc:
        return ReviewPipelineOutcome(
            pr_number=pr_number, error=f"Evidence build failed: {exc}",
        )

    # Stage 1: Review (delegates to existing code_reviewer)
    findings: list[ReviewFinding] = []
    if code_reviewer:
        try:
            from scripts.pipeline_adapter import evidence_to_pr_review_context
            pr_ctx, diff_scope = evidence_to_pr_review_context(
                evidence,
                pr_number=pr_number,
                pr_title=pr_title,
                pr_body=pr_body,
                diff_text=diff,
            )
            # CodeReviewer.review(pr, diff_scope, config, *, short_summary="")
            if config is not None:
                raw_findings = code_reviewer.review(pr_ctx, diff_scope, config)
            else:
                # Fall back to a minimal call when no config is supplied
                # (mainly for tests that pass a MagicMock reviewer).
                raw_findings = code_reviewer.review(evidence)
            if isinstance(raw_findings, list):
                for f in raw_findings:
                    if isinstance(f, ReviewFinding):
                        findings.append(f)
                    elif isinstance(f, dict):
                        findings.append(ReviewFinding(**f))
        except Exception as exc:
            return ReviewPipelineOutcome(
                pr_number=pr_number, evidence=evidence,
                error=f"Review failed: {exc}",
            )

    # Stage 2: Gate findings
    max_comments = 25
    if config and hasattr(config, "max_review_comments"):
        max_comments = config.max_review_comments
    gated, _verdict = _gate_findings(findings, evidence, max_comments)

    # Stage 3: Publish
    published = False
    if comment_publisher and gated:
        try:
            comment_publisher.publish(pr_number, gated)
            published = True
        except Exception as exc:
            logger.error("Publish failed for PR #%d: %s", pr_number, exc)

    return ReviewPipelineOutcome(
        pr_number=pr_number, evidence=evidence,
        findings=findings, gated_findings=gated,
        published=published,
    )

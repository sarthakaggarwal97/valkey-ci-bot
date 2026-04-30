"""Adapter between the new evidence-first pipeline and the existing
pr_manager / failure_store / code_reviewer infrastructure.

The stages in ``scripts/stages/`` produce new typed outputs (EvidencePack,
RootCauseResult, TournamentResult, RubricVerdict). The existing
infrastructure expects older types (FailureReport, RootCauseReport,
PullRequestContext, DiffScope). This module converts between them so the
new pipeline can drive the existing PR creation, comment publishing,
and state persistence paths without rewriting them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from scripts.models import (
    ChangedFile,
    DiffScope,
    EvidencePack,
    FailureReport,
    FailureStoreEntry,
    PullRequestCommit,
    PullRequestContext,
    RejectionReason,
    RootCauseHypothesis,
    RootCauseReport,
    RootCauseResult,
    TournamentResult,
)

logger = logging.getLogger(__name__)


def evidence_to_failure_report(
    evidence: EvidencePack,
    *,
    job_name: str = "",
    commit_sha: str = "",
    workflow_file: str = "",
    repo_full_name: str = "",
    target_branch: str = "unstable",
) -> FailureReport:
    """Convert an EvidencePack to the legacy FailureReport format.

    This lets the new pipeline feed existing PRManager.create_pr and any
    other code that still expects FailureReport.
    """
    workflow_name = evidence.workflow or workflow_file
    return FailureReport(
        workflow_name=workflow_name,
        job_name=job_name or (evidence.job_ids[0] if evidence.job_ids else ""),
        matrix_params={},
        commit_sha=commit_sha,
        failure_source="trusted",
        parsed_failures=list(evidence.parsed_failures),
        raw_log_excerpt=(
            evidence.log_excerpts[0].content if evidence.log_excerpts else None
        ),
        is_unparseable=not evidence.parsed_failures,
        workflow_file=workflow_file or evidence.workflow,
        repo_full_name=repo_full_name,
        workflow_run_id=evidence.run_id,
        target_branch=target_branch,
    )


def hypothesis_to_root_cause_report(
    hypothesis: RootCauseHypothesis,
    evidence: EvidencePack,
) -> RootCauseReport:
    """Convert a RootCauseHypothesis to the legacy RootCauseReport format."""
    # Extract file paths referenced in evidence_refs (format: "file:path")
    files_to_change: list[str] = []
    for ref in hypothesis.evidence_refs:
        if ref.startswith("file:"):
            files_to_change.append(ref[len("file:"):])
    # Fall back to all inspected files if evidence_refs didn't cite any
    if not files_to_change:
        files_to_change = [
            f.path for f in evidence.source_files_inspected
            + evidence.test_files_inspected
        ]

    rationale_parts = list(hypothesis.causal_chain)
    if hypothesis.disconfirmed_alternatives:
        rationale_parts.append(
            "Disconfirmed alternatives: "
            + "; ".join(hypothesis.disconfirmed_alternatives)
        )
    rationale = " | ".join(rationale_parts) if rationale_parts else ""

    return RootCauseReport(
        description=hypothesis.summary,
        files_to_change=files_to_change,
        confidence=hypothesis.confidence,
        rationale=rationale,
        is_flaky=False,  # stages don't classify flakiness today
        flakiness_indicators=None,
    )


def update_failure_store_entry(
    entry: FailureStoreEntry,
    *,
    evidence: EvidencePack | None = None,
    rejection: RejectionReason | None = None,
    pr_url: str | None = None,
    status: str | None = None,
) -> FailureStoreEntry:
    """Apply pipeline-outcome updates to a FailureStoreEntry in place.

    Returns the same entry for chaining. Fields not provided keep their
    existing values.
    """
    if evidence is not None:
        entry.evidence_pack = evidence.to_dict()
    if rejection is not None:
        entry.rejection_reason = rejection.value
    if pr_url is not None:
        entry.pr_url = pr_url
    if status is not None:
        entry.status = status
    entry.updated_at = datetime.now(timezone.utc).isoformat()
    return entry


def evidence_to_pr_review_context(
    evidence: EvidencePack,
    *,
    pr_number: int,
    pr_title: str = "",
    pr_body: str = "",
    base_sha: str = "",
    head_sha: str = "",
    author: str = "",
    repo_full_name: str = "",
    diff_text: str = "",
    base_ref: str = "",
    head_ref: str = "",
) -> tuple[PullRequestContext, DiffScope]:
    """Convert PR review evidence to the PullRequestContext + DiffScope pair
    expected by the existing CodeReviewer.review() API.

    ``diff_text`` is stored per-file in ChangedFile.patch — for a reviewer
    that wants to see all changes, use the first log excerpt in evidence
    (which holds the raw diff) if diff_text is empty.
    """
    if not diff_text and evidence.log_excerpts:
        diff_text = evidence.log_excerpts[0].content

    changed_files: list[ChangedFile] = []
    for inspected in evidence.source_files_inspected + evidence.test_files_inspected:
        changed_files.append(ChangedFile(
            path=inspected.path,
            status="modified",
            additions=0,
            deletions=0,
            patch=None,
            contents=inspected.excerpt,
            is_binary=False,
        ))

    commits = [
        PullRequestCommit(sha=c.sha, message=c.message)
        for c in evidence.recent_commits
    ]

    pr = PullRequestContext(
        repo=repo_full_name,
        number=pr_number,
        title=pr_title,
        body=pr_body,
        base_sha=base_sha,
        head_sha=head_sha,
        author=author,
        files=changed_files,
        review_comments=[],
        commits=commits,
        base_ref=base_ref,
        head_ref=head_ref,
        labels=[],
    )
    diff_scope = DiffScope(
        base_sha=base_sha,
        head_sha=head_sha,
        files=changed_files,
        incremental=False,
    )
    return pr, diff_scope


def create_pr_via_legacy_manager(
    pr_manager: Any,
    tournament: TournamentResult,
    root_cause: RootCauseResult,
    evidence: EvidencePack,
    *,
    job_name: str = "",
    commit_sha: str = "",
    workflow_file: str = "",
    repo_full_name: str = "",
    target_branch: str = "unstable",
    draft: bool = False,
) -> str:
    """Create a PR using the existing PRManager.create_pr API.

    Converts pipeline outputs to legacy types and invokes the existing
    PR creation path (which itself is already gated by publish_guard).

    Returns the PR URL on success.

    Raises:
        ValueError: when there is no winning candidate or accepted hypothesis.
        RuntimeError / GithubException: from the underlying PR manager.
    """
    if tournament.winning is None:
        raise ValueError("create_pr_via_legacy_manager: no winning candidate")
    if root_cause.accepted is None:
        raise ValueError("create_pr_via_legacy_manager: no accepted root cause")

    failure_report = evidence_to_failure_report(
        evidence,
        job_name=job_name,
        commit_sha=commit_sha,
        workflow_file=workflow_file,
        repo_full_name=repo_full_name,
        target_branch=target_branch,
    )
    rc_report = hypothesis_to_root_cause_report(root_cause.accepted, evidence)

    return pr_manager.create_pr(
        tournament.winning.candidate.patch,
        failure_report,
        rc_report,
        target_branch,
        draft=draft,
    )

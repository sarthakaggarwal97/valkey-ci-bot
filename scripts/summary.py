"""GitHub Actions workflow summary and PR summary comments for CI Failure Bot.

Collects processing results during a pipeline run and emits a
markdown summary to ``$GITHUB_STEP_SUMMARY`` (or returns the
rendered string for testing).

Also provides ``PRSummaryComment`` for posting processing step
summaries on created pull requests.

Requirements: 11.2, 11.4
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ProcessingResult:
    """Outcome of processing a single failure."""

    job_name: str
    failure_identifier: str
    outcome: str  # e.g. "pr-created", "skipped-duplicate", "analysis-failed", etc.
    error: str | None = None


@dataclass
class WorkflowSummary:
    """Accumulates per-failure results and renders a markdown summary."""

    mode: str = "analyze"  # "analyze" or "reconcile"
    results: list[ProcessingResult] = field(default_factory=list)

    # ---- collection helpers ----

    def add_result(
        self,
        job_name: str,
        failure_identifier: str,
        outcome: str,
        error: str | None = None,
    ) -> None:
        """Record the outcome for one processed failure."""
        self.results.append(
            ProcessingResult(
                job_name=job_name,
                failure_identifier=failure_identifier,
                outcome=outcome,
                error=error,
            )
        )

    # ---- rendering ----

    def render(self) -> str:
        """Return the full markdown summary string."""
        lines: list[str] = []
        lines.append(f"## CI Failure Bot — {self.mode} run\n")

        if not self.results:
            lines.append("No failures processed.\n")
            return "\n".join(lines)

        # Summary counts
        total = len(self.results)
        errors = sum(1 for r in self.results if r.error)
        lines.append(f"**{total}** failure(s) processed, **{errors}** error(s).\n")

        # Markdown table
        lines.append("| Job | Failure | Outcome | Error |")
        lines.append("|-----|---------|---------|-------|")
        for r in self.results:
            error_cell = r.error or ""
            lines.append(
                f"| {r.job_name} | {r.failure_identifier} "
                f"| {r.outcome} | {error_cell} |"
            )

        lines.append("")  # trailing newline
        return "\n".join(lines)

    # ---- output ----

    def write(self) -> str:
        """Render the summary and write it to ``$GITHUB_STEP_SUMMARY``.

        Always returns the rendered markdown string (useful for testing).
        If the environment variable is not set the summary is only logged.
        """
        md = self.render()
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            try:
                with open(summary_path, "a") as fh:
                    fh.write(md)
                logger.info("Workflow summary written to %s", summary_path)
            except OSError as exc:
                logger.warning("Failed to write workflow summary: %s", exc)
        else:
            logger.debug("GITHUB_STEP_SUMMARY not set; summary not written to file.")
        return md


# ---------------------------------------------------------------------------
# PR Summary Comment (Requirement 11.2)
# ---------------------------------------------------------------------------

PROCESSING_STEPS = [
    "detection",
    "parsing",
    "analysis",
    "generation",
    "validation",
    "pr_creation",
]


@dataclass
class StepTiming:
    """Timing and status for a single processing step."""

    name: str
    duration_seconds: float = 0.0
    status: str = "completed"  # "completed", "skipped", "failed"


@dataclass
class PRSummaryComment:
    """Collects processing metadata and renders a PR summary comment.

    After a PR is created, this comment is posted on the PR listing
    the processing steps completed, time taken, and retries.

    Requirement 11.2
    """

    steps: list[StepTiming] = field(default_factory=list)
    fix_retries: int = 0
    validation_retries: int = 0
    total_duration_seconds: float = 0.0

    def add_step(
        self,
        name: str,
        duration_seconds: float = 0.0,
        status: str = "completed",
    ) -> None:
        """Record a processing step."""
        self.steps.append(
            StepTiming(name=name, duration_seconds=duration_seconds, status=status)
        )

    def render(self) -> str:
        """Return the markdown comment body."""
        lines: list[str] = []
        lines.append("## Processing Summary\n")

        # Steps table
        lines.append("| Step | Duration | Status |")
        lines.append("|------|----------|--------|")
        for step in self.steps:
            duration_str = f"{step.duration_seconds:.1f}s"
            lines.append(f"| {step.name} | {duration_str} | {step.status} |")

        lines.append("")

        # Retries
        lines.append(f"**Fix generation retries:** {self.fix_retries}")
        lines.append(f"**Validation retries:** {self.validation_retries}")

        # Total time
        total = self.total_duration_seconds
        if total <= 0.0 and self.steps:
            total = sum(s.duration_seconds for s in self.steps)
        lines.append(f"**Total time:** {total:.1f}s")

        lines.append("")
        return "\n".join(lines)


@dataclass
class ApprovalCandidate:
    """Queued fix awaiting manual approval before PR creation."""

    job_name: str
    failure_identifier: str
    workflow_run_url: str
    confidence: str
    is_flaky: bool
    failure_streak: int
    total_failure_observations: int
    last_known_good_sha: str | None
    first_bad_sha: str | None
    files_to_change: list[str]
    rationale: str


@dataclass
class ApprovalSummary:
    """Human-facing summary of queued fixes waiting for approval."""

    candidates: list[ApprovalCandidate] = field(default_factory=list)

    def add_candidate(self, candidate: ApprovalCandidate) -> None:
        """Record one queued candidate."""
        self.candidates.append(candidate)

    def render(self) -> str:
        """Render approval-ready candidates as markdown."""
        if not self.candidates:
            return ""

        lines = ["## Approval Queue\n"]
        lines.append("| Job | Failure | Confidence | Flaky | Streak | Suspect Range | Run |")
        lines.append("|-----|---------|------------|-------|--------|---------------|-----|")
        for candidate in self.candidates:
            suspect_range = "unknown"
            if candidate.last_known_good_sha or candidate.first_bad_sha:
                suspect_range = (
                    f"{(candidate.last_known_good_sha or 'unknown')[:12]} -> "
                    f"{(candidate.first_bad_sha or 'unknown')[:12]}"
                )
            lines.append(
                "| "
                f"{candidate.job_name} | "
                f"{candidate.failure_identifier} | "
                f"{candidate.confidence} | "
                f"{'yes' if candidate.is_flaky else 'no'} | "
                f"{candidate.failure_streak} | "
                f"{suspect_range} | "
                f"[run]({candidate.workflow_run_url}) |"
            )

        for candidate in self.candidates:
            lines.append("")
            lines.append(
                f"### {candidate.job_name} — {candidate.failure_identifier}"
            )
            lines.append(
                f"- Failure observations: {candidate.total_failure_observations}"
            )
            lines.append(f"- Consecutive failures: {candidate.failure_streak}")
            if candidate.last_known_good_sha:
                lines.append(
                    f"- Last known good commit: `{candidate.last_known_good_sha}`"
                )
            if candidate.first_bad_sha:
                lines.append(f"- First bad commit: `{candidate.first_bad_sha}`")
            if candidate.files_to_change:
                lines.append(
                    f"- Files to change: {', '.join(candidate.files_to_change)}"
                )
            lines.append(f"- Rationale: {candidate.rationale}")

        lines.append("")
        return "\n".join(lines)

    def write(self) -> str:
        """Append the approval summary to ``$GITHUB_STEP_SUMMARY``."""
        md = self.render()
        if not md:
            return md

        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            try:
                with open(summary_path, "a") as fh:
                    fh.write(md)
                logger.info("Approval summary written to %s", summary_path)
            except OSError as exc:
                logger.warning("Failed to write approval summary: %s", exc)
        return md


@dataclass
class ReviewStageResult:
    """Outcome of a PR reviewer stage."""

    stage: str
    outcome: str
    detail: str | None = None


@dataclass
class ReviewWorkflowSummary:
    """Workflow summary tailored for PR reviewer runs."""

    mode: str
    results: list[ReviewStageResult] = field(default_factory=list)

    def add_result(self, stage: str, outcome: str, detail: str | None = None) -> None:
        """Record one reviewer stage result."""
        self.results.append(
            ReviewStageResult(stage=stage, outcome=outcome, detail=detail)
        )

    def render(self) -> str:
        """Render the reviewer workflow summary."""
        lines = [f"## PR Review Bot - {self.mode} run\n"]
        if not self.results:
            lines.append("No review stages executed.\n")
            return "\n".join(lines)

        lines.append("| Stage | Outcome | Detail |")
        lines.append("|-------|---------|--------|")
        for result in self.results:
            lines.append(
                f"| {result.stage} | {result.outcome} | {result.detail or ''} |"
            )
        lines.append("")
        return "\n".join(lines)

    def write(self) -> str:
        """Render the summary and append it to ``$GITHUB_STEP_SUMMARY``."""
        md = self.render()
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            try:
                with open(summary_path, "a") as fh:
                    fh.write(md)
                logger.info("Reviewer workflow summary written to %s", summary_path)
            except OSError as exc:
                logger.warning("Failed to write reviewer workflow summary: %s", exc)
        else:
            logger.debug(
                "GITHUB_STEP_SUMMARY not set; reviewer summary not written to file."
            )
        return md


@dataclass
class FuzzerRunSummaryRow:
    """One analyzed fuzzer workflow run for summary rendering."""

    run_id: int
    run_url: str
    conclusion: str
    overall_status: str
    scenario_id: str | None
    seed: str | None
    anomaly_count: int
    normal_signal_count: int
    summary: str
    reproduction_hint: str | None = None


@dataclass
class FuzzerWorkflowSummary:
    """Workflow summary tailored for centralized fuzzer-run analysis."""

    rows: list[FuzzerRunSummaryRow] = field(default_factory=list)

    def add_row(self, row: FuzzerRunSummaryRow) -> None:
        """Record one analyzed run."""
        self.rows.append(row)

    def render(self) -> str:
        """Render the centralized fuzzer analysis summary."""
        lines = ["## Valkey Fuzzer Analysis\n"]
        if not self.rows:
            lines.append("No fuzzer runs analyzed.\n")
            return "\n".join(lines)

        lines.append(
            "| Run | Conclusion | Status | Scenario | Seed | Anomalies | Normal Signals |"
        )
        lines.append(
            "|-----|------------|--------|----------|------|-----------|----------------|"
        )
        for row in self.rows:
            lines.append(
                "| "
                f"[{row.run_id}]({row.run_url}) | "
                f"{row.conclusion or 'unknown'} | "
                f"{row.overall_status} | "
                f"{row.scenario_id or 'unknown'} | "
                f"{row.seed or 'unknown'} | "
                f"{row.anomaly_count} | "
                f"{row.normal_signal_count} |"
            )

        for row in self.rows:
            lines.append("")
            lines.append(f"### Run {row.run_id}")
            lines.append(f"- Summary: {row.summary}")
            if row.reproduction_hint:
                lines.append(f"- Reproduction: `{row.reproduction_hint}`")
        lines.append("")
        return "\n".join(lines)

    def write(self) -> str:
        """Render the summary and append it to ``$GITHUB_STEP_SUMMARY``."""
        md = self.render()
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            try:
                with open(summary_path, "a") as fh:
                    fh.write(md)
                logger.info("Fuzzer workflow summary written to %s", summary_path)
            except OSError as exc:
                logger.warning("Failed to write fuzzer workflow summary: %s", exc)
        else:
            logger.debug(
                "GITHUB_STEP_SUMMARY not set; fuzzer summary not written to file."
            )
        return md

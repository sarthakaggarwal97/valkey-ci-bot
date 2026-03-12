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

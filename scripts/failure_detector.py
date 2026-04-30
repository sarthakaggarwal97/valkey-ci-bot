"""Detects failed jobs from a GitHub Actions workflow run."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from scripts.models import FailedJob, WorkflowRun

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)

# Known infrastructure failure patterns (case-insensitive)
_INFRA_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"runner\s+timeout", re.IGNORECASE),
    re.compile(r"the hosted runner lost communication", re.IGNORECASE),
    re.compile(r"runner\s+has\s+received\s+a\s+shutdown\s+signal", re.IGNORECASE),
    re.compile(r"network\s+error", re.IGNORECASE),
    re.compile(r"rate\s+limit", re.IGNORECASE),
    re.compile(r"ETIMEDOUT", re.IGNORECASE),
    re.compile(r"ECONNRESET", re.IGNORECASE),
    re.compile(r"service\s+unavailable", re.IGNORECASE),
    re.compile(r"runner\s+provisioning\s+error", re.IGNORECASE),
    re.compile(r"no\s+space\s+left\s+on\s+device", re.IGNORECASE),
    # GitHub job/workflow cancellation — not actionable as a code failure.
    re.compile(r"The operation was cancelled\.?", re.IGNORECASE),
    re.compile(r"The run was cancel(?:ed|led)", re.IGNORECASE),
    re.compile(r"Error:\s+The operation was can?celled", re.IGNORECASE),
]


class FailureDetector:
    """Detects and filters failed jobs from a workflow run."""

    def __init__(self, github_client: Github) -> None:
        self._gh = github_client

    def detect(self, workflow_run: WorkflowRun) -> list[FailedJob]:
        """Return failed jobs from a workflow run, excluding infra failures."""
        logger.info(
            "Detection started for workflow run %d (%s) on %s",
            workflow_run.id, workflow_run.name, workflow_run.head_repository,
        )
        repo = self._gh.get_repo(workflow_run.head_repository)
        run = repo.get_workflow_run(workflow_run.id)
        failed_jobs: list[FailedJob] = []

        for job in run.jobs():
            if job.conclusion != "failure":
                continue

            # Build a text blob from job name + annotations for infra check
            check_text = job.name or ""
            try:
                for annotation in job.get_annotations() if hasattr(job, "get_annotations") else []:
                    check_text += " " + (getattr(annotation, "message", "") or "")
            except Exception as exc:
                logger.debug("Could not fetch annotations for job %s: %s", job.name, exc)

            if self.is_infrastructure_failure(check_text):
                logger.info("Skipping infrastructure failure: %s", job.name)
                continue

            matrix_params = self.extract_matrix_params(job.name or "")

            # Find the failed step name
            step_name: str | None = None
            if hasattr(job, "steps"):
                for step in job.steps:
                    if getattr(step, "conclusion", None) == "failure":
                        step_name = step.name
                        break

            failed_jobs.append(FailedJob(
                id=job.id,
                name=job.name,
                conclusion=job.conclusion,
                step_name=step_name,
                matrix_params=matrix_params,
            ))

        logger.info(
            "Detection complete for run %d: %d actionable failure(s) found.",
            workflow_run.id, len(failed_jobs),
        )
        return failed_jobs

    @staticmethod
    def is_infrastructure_failure(text: str) -> bool:
        """Return True if the text matches known infrastructure error patterns."""
        return any(p.search(text) for p in _INFRA_PATTERNS)

    @staticmethod
    def extract_matrix_params(job_name: str) -> dict[str, str]:
        """Extract matrix params from a GitHub Actions job name."""
        matrix_params: dict[str, str] = {}
        match = re.search(r"\((.+)\)", job_name)
        if match:
            for index, part in enumerate(match.group(1).split(",")):
                matrix_params[f"param_{index}"] = part.strip()
        return matrix_params

    @staticmethod
    def classify_trust(workflow_run: WorkflowRun, consumer_repo: str) -> str:
        """Return 'trusted' or 'untrusted-fork' based on head repository."""
        if workflow_run.head_repository == consumer_repo and not workflow_run.is_fork:
            return "trusted"
        return "untrusted-fork"

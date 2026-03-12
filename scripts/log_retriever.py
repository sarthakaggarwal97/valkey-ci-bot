"""Fetches job logs from the GitHub Actions API."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)


class LogRetriever:
    """Retrieves raw log output for a failed GitHub Actions job."""

    def __init__(self, github_client: Github) -> None:
        self._gh = github_client

    def get_job_log(self, repo_full_name: str, job_id: int) -> str:
        """Fetch the full log for a job via the GitHub API.

        Returns the log content as a string, or empty string on failure.
        """
        try:
            repo = self._gh.get_repo(repo_full_name)
            # PyGithub doesn't have a direct job log method; use the
            # requester to call the REST endpoint.
            url = f"/repos/{repo_full_name}/actions/jobs/{job_id}/logs"
            headers, data = repo._requester.requestBlobAndCheck("GET", url)
            if isinstance(data, bytes):
                return data.decode("utf-8", errors="replace")
            if isinstance(data, str):
                return data
            logger.error("Unexpected log payload type for job %d: %s", job_id, type(data).__name__)
            return ""
        except Exception as exc:
            logger.error("Failed to retrieve log for job %d: %s", job_id, exc)
            return ""

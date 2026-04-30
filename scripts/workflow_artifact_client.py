"""Helpers for GitHub Actions workflow artifact discovery and download."""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts.github_client import retry_github_call

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowArtifact:
    """One workflow artifact returned by the GitHub Actions API."""

    artifact_id: int
    name: str
    size_in_bytes: int
    expired: bool


class WorkflowArtifactClient:
    """Downloads workflow artifacts via the GitHub REST API."""

    def __init__(
        self,
        github_client: Github,
        *,
        token: str | None = None,
        retries: int = 3,
    ) -> None:
        self._gh = github_client
        self._token = token
        self._retries = max(0, retries)

    def list_run_artifacts(
        self,
        repo_full_name: str,
        run_id: int,
    ) -> list[WorkflowArtifact]:
        """List available artifacts for a workflow run."""
        repo = self._gh.get_repo(repo_full_name)

        def _operation() -> object:
            _headers, data = repo._requester.requestJsonAndCheck(
                "GET",
                f"/repos/{repo_full_name}/actions/runs/{run_id}/artifacts",
            )
            return data

        payload = retry_github_call(
            _operation,
            retries=self._retries,
            description=f"list artifacts for run {run_id}",
        )
        if not isinstance(payload, dict):
            return []

        artifacts_raw = payload.get("artifacts")
        if not isinstance(artifacts_raw, list):
            return []

        artifacts: list[WorkflowArtifact] = []
        for raw_artifact in artifacts_raw:
            if not isinstance(raw_artifact, dict):
                continue
            artifact_id = raw_artifact.get("id")
            name = raw_artifact.get("name")
            if not isinstance(artifact_id, int) or not isinstance(name, str):
                continue
            artifacts.append(
                WorkflowArtifact(
                    artifact_id=artifact_id,
                    name=name,
                    size_in_bytes=int(raw_artifact.get("size_in_bytes", 0)),
                    expired=bool(raw_artifact.get("expired", False)),
                )
            )
        return artifacts

    def download_artifact_files(
        self,
        repo_full_name: str,
        artifact_id: int,
    ) -> dict[str, bytes]:
        """Download one artifact zip and return extracted file contents."""
        repo = self._gh.get_repo(repo_full_name)

        def _operation() -> object:
            path = f"/repos/{repo_full_name}/actions/artifacts/{artifact_id}/zip"
            if self._token:
                return _download_bytes_via_http(path, self._token)
            _headers, data = repo._requester.requestBlobAndCheck("GET", path)
            return data

        blob = retry_github_call(
            _operation,
            retries=self._retries,
            description=f"download artifact {artifact_id}",
        )
        return _extract_zip_files(blob, description=f"artifact {artifact_id}")

    def download_run_log_files(
        self,
        repo_full_name: str,
        run_id: int,
    ) -> dict[str, bytes]:
        """Download the workflow-run log archive and return extracted files."""
        repo = self._gh.get_repo(repo_full_name)

        def _operation() -> object:
            path = f"/repos/{repo_full_name}/actions/runs/{run_id}/logs"
            if self._token:
                return _download_bytes_via_http(path, self._token)
            _headers, data = repo._requester.requestBlobAndCheck("GET", path)
            return data

        blob = retry_github_call(
            _operation,
            retries=self._retries,
            description=f"download run logs for {run_id}",
        )
        return _extract_zip_files(blob, description=f"run logs {run_id}")


def _extract_zip_files(blob: object, *, description: str) -> dict[str, bytes]:
    if isinstance(blob, str):
        archive_bytes = blob.encode("utf-8")
    elif isinstance(blob, bytes):
        archive_bytes = blob
    else:
        logger.warning(
            "Unexpected zip payload type for %s: %s",
            description,
            type(blob).__name__,
        )
        return {}

    extracted: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            extracted[member.filename] = archive.read(member)
    return extracted


class _StripAuthRedirectHandler(HTTPRedirectHandler):
    """Drop the Authorization header when following redirects.

    GitHub's artifact/log download endpoints return a 302 to Azure Blob
    Storage.  If the ``Authorization: Bearer`` header is forwarded, Azure
    rejects the request with HTTP 401.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None and new_req.host != req.host:
            new_req.remove_header("Authorization")
        return new_req


def _download_bytes_via_http(path: str, token: str) -> bytes:
    request = Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "valkey-ci-agent",
        },
    )
    opener = build_opener(_StripAuthRedirectHandler)
    with opener.open(request, timeout=60) as response:
        return response.read()

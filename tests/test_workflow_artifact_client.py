"""Tests for workflow artifact download helpers."""

from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

from scripts.workflow_artifact_client import WorkflowArtifactClient


def test_list_run_artifacts_parses_github_payload() -> None:
    github_client = MagicMock()
    repo = github_client.get_repo.return_value
    repo._requester.requestJsonAndCheck.return_value = (
        {},
        {
            "artifacts": [
                {
                    "id": 11,
                    "name": "fuzzer-run-artifacts-11",
                    "size_in_bytes": 1234,
                    "expired": False,
                },
                {
                    "id": 12,
                    "name": "other",
                    "size_in_bytes": 5,
                    "expired": True,
                },
            ]
        },
    )

    client = WorkflowArtifactClient(github_client)
    artifacts = client.list_run_artifacts("valkey-io/valkey-fuzzer", 99)

    assert [artifact.name for artifact in artifacts] == [
        "fuzzer-run-artifacts-11",
        "other",
    ]
    assert artifacts[0].artifact_id == 11
    assert artifacts[0].size_in_bytes == 1234
    assert artifacts[1].expired is True


def test_download_artifact_files_extracts_zip_entries() -> None:
    github_client = MagicMock()
    repo = github_client.get_repo.return_value
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("bundle/manifest.json", '{"schema_version": 1}')
        archive.writestr("bundle/logs/node-1.log", "hello")
    repo._requester.requestBlobAndCheck.return_value = ({}, zip_buffer.getvalue())

    client = WorkflowArtifactClient(github_client)
    files = client.download_artifact_files("valkey-io/valkey-fuzzer", 42)

    assert files["bundle/manifest.json"] == b'{"schema_version": 1}'
    assert files["bundle/logs/node-1.log"] == b"hello"


def test_download_run_log_files_extracts_archive_entries() -> None:
    github_client = MagicMock()
    repo = github_client.get_repo.return_value
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("logs/0_random-fuzzer.txt", "scenario output")
    repo._requester.requestBlobAndCheck.return_value = ({}, zip_buffer.getvalue())

    client = WorkflowArtifactClient(github_client)
    files = client.download_run_log_files("valkey-io/valkey-fuzzer", 77)

    assert files["logs/0_random-fuzzer.txt"] == b"scenario output"


def test_download_artifact_files_can_use_http_fallback(monkeypatch) -> None:
    github_client = MagicMock()
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("bundle/results.json", "{}")

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return zip_buffer.getvalue()

    monkeypatch.setattr(
        "scripts.workflow_artifact_client.urlopen",
        lambda request, timeout=60: _Response(),
    )

    client = WorkflowArtifactClient(github_client, token="secret-token")
    files = client.download_artifact_files("valkey-io/valkey-fuzzer", 42)

    assert files["bundle/results.json"] == b"{}"

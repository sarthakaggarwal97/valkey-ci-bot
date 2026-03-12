"""Tests for GitHub Actions job log retrieval."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts.log_retriever import LogRetriever


def test_get_job_log_decodes_requester_bytes() -> None:
    github_client = MagicMock()
    repo = github_client.get_repo.return_value
    repo._requester.requestBlobAndCheck.return_value = ({}, b"hello\nworld")

    retriever = LogRetriever(github_client)

    assert retriever.get_job_log("owner/repo", 7) == "hello\nworld"


def test_get_job_log_uses_http_fallback_when_token_present(monkeypatch) -> None:
    github_client = MagicMock()

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b"from-http"

    monkeypatch.setattr(
        "scripts.log_retriever.urlopen",
        lambda request, timeout=60: _Response(),
    )

    retriever = LogRetriever(github_client, token="secret-token")

    assert retriever.get_job_log("owner/repo", 8) == "from-http"

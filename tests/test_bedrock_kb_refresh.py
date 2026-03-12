"""Tests for the Bedrock KB refresh helper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.bedrock_kb_refresh import (
    RefreshArgs,
    build_custom_document,
    infer_repo_slug,
    refresh,
)


def _args(**overrides) -> RefreshArgs:
    defaults = dict(
        region="us-east-1",
        repo_url="https://github.com/valkey-io/valkey.git",
        branch="unstable",
        code_kb_id="CODEKB",
        code_data_source_name="valkey-code-custom",
        web_kb_id="DOCSKB",
        web_data_source_id=None,
        web_data_source_name="valkey-docs-web",
        web_seed_urls=["https://valkey.io/"],
        skip_web_sync=False,
        missing_only=False,
        dry_run=True,
        verbose=False,
    )
    defaults.update(overrides)
    return RefreshArgs(**defaults)


def test_infer_repo_slug_handles_https_and_ssh_urls() -> None:
    assert infer_repo_slug("https://github.com/valkey-io/valkey.git") == "valkey-io/valkey"
    assert infer_repo_slug("git@github.com:valkey-io/valkey.git") == "valkey-io/valkey"


def test_build_custom_document_includes_path_and_metadata(tmp_path: Path) -> None:
    root = tmp_path
    path = root / "src" / "server.c"
    path.parent.mkdir(parents=True)
    path.write_text("int main(void) { return 0; }\n")

    document = build_custom_document(
        path,
        root,
        "valkey-io/valkey",
        "unstable",
        "abc123",
    )

    assert document.document_id == "unstable:src/server.c"
    metadata = document.payload["metadata"]["inlineAttributes"]  # type: ignore[index]
    assert any(item["key"] == "path" and item["value"]["stringValue"] == "src/server.c" for item in metadata)  # type: ignore[index]
    assert any(item["key"] == "commit_sha" and item["value"]["stringValue"] == "abc123" for item in metadata)  # type: ignore[index]
    text = document.payload["content"]["custom"]["inlineContent"]["textContent"]["data"]  # type: ignore[index]
    assert "Repository: valkey-io/valkey" in text
    assert "Path: src/server.c" in text


@patch("scripts.bedrock_kb_refresh.find_data_source_id")
@patch("scripts.bedrock_kb_refresh.prepare_corpus")
@patch("scripts.bedrock_kb_refresh.boto3.Session")
def test_refresh_dry_run_returns_plan_without_mutation(
    mock_session_cls,
    mock_prepare_corpus,
    mock_find_data_source_id,
) -> None:
    mock_prepare_corpus.return_value = ("abc123", 5, 1024, [])
    mock_find_data_source_id.side_effect = ["CODEDS", "WEBDS"]
    mock_agent_client = MagicMock()
    mock_session_cls.return_value.client.return_value = mock_agent_client

    result = refresh(_args())

    assert result["dry_run"] is True
    assert result["code_data_source_id"] == "CODEDS"
    assert result["web_data_source_id"] == "WEBDS"
    assert result["code_data_source_action"] == "update-existing"
    assert result["web_data_source_action"] == "update-existing"
    mock_agent_client.create_data_source.assert_not_called()
    mock_agent_client.update_data_source.assert_not_called()
    mock_agent_client.ingest_knowledge_base_documents.assert_not_called()
    mock_agent_client.start_ingestion_job.assert_not_called()


@patch("scripts.bedrock_kb_refresh.find_data_source_id")
@patch("scripts.bedrock_kb_refresh.prepare_corpus")
@patch("scripts.bedrock_kb_refresh.boto3.Session")
def test_refresh_dry_run_skips_web_lookup_when_requested(
    mock_session_cls,
    mock_prepare_corpus,
    mock_find_data_source_id,
) -> None:
    mock_prepare_corpus.return_value = ("abc123", 5, 1024, [])
    mock_find_data_source_id.return_value = "CODEDS"
    mock_session_cls.return_value.client.return_value = MagicMock()

    result = refresh(_args(skip_web_sync=True))

    assert result["web_data_source_id"] is None
    assert "web_data_source_action" not in result
    assert mock_find_data_source_id.call_count == 1

from __future__ import annotations

from pathlib import Path

from scripts.bedrock_kb_refresh import (
    CUSTOM_BATCH_DOC_LIMIT,
    CuratedDocument,
    build_custom_document,
    build_document_id,
    chunk_documents,
    make_document_text,
    should_include_file,
)


def test_should_include_text_source_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "src" / "server.c"
    source.parent.mkdir()
    source.write_text("int main(void) { return 0; }\n")

    assert should_include_file(source, root) is True


def test_should_exclude_git_internal_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    internal = root / ".git" / "HEAD"
    internal.parent.mkdir()
    internal.write_text("ref: refs/heads/unstable\n")

    assert should_include_file(internal, root) is False


def test_should_exclude_binary_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    image = root / "docs" / "diagram.png"
    image.parent.mkdir()
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    assert should_include_file(image, root) is False


def test_build_document_id_is_stable_and_branch_scoped() -> None:
    assert build_document_id("src/server.c", "unstable") == "unstable:src/server.c"


def test_make_document_text_includes_repo_metadata_and_code_fence() -> None:
    rendered = make_document_text(
        "src/server.c",
        "int main(void) { return 0; }\n",
        "valkey-io/valkey",
        "unstable",
        "abc123",
    )

    assert "Repository: valkey-io/valkey" in rendered
    assert "Branch: unstable" in rendered
    assert "Commit SHA: abc123" in rendered
    assert "Path: src/server.c" in rendered
    assert "```c" in rendered


def test_build_custom_document_sets_inline_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "src" / "server.c"
    source.parent.mkdir()
    source.write_text("int main(void) { return 0; }\n")

    document = build_custom_document(source, root, "valkey-io/valkey", "unstable", "abc123")

    assert document.document_id == "unstable:src/server.c"
    assert document.relative_path == "src/server.c"
    assert document.payload["content"]["dataSourceType"] == "CUSTOM"
    assert document.payload["content"]["custom"]["sourceType"] == "IN_LINE"
    assert document.payload["metadata"]["type"] == "IN_LINE_ATTRIBUTE"

    attrs = {
        item["key"]: item["value"]["stringValue"]
        for item in document.payload["metadata"]["inlineAttributes"]
        if item["value"]["type"] == "STRING"
    }
    assert attrs["repo"] == "valkey-io/valkey"
    assert attrs["branch"] == "unstable"
    assert attrs["commit_sha"] == "abc123"
    assert attrs["path"] == "src/server.c"
    assert attrs["language"] == "c"
    assert attrs["top_level_dir"] == "src"


def test_build_custom_document_omits_empty_extension_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    makefile = root / "Makefile"
    makefile.write_text("all:\n\t@true\n")

    document = build_custom_document(makefile, root, "valkey-io/valkey", "unstable", "abc123")

    attrs = {item["key"]: item["value"]["stringValue"] for item in document.payload["metadata"]["inlineAttributes"]}
    assert "extension" not in attrs


def test_chunk_documents_splits_at_doc_limit() -> None:
    documents = [
        CuratedDocument(
            document_id=f"doc-{index}",
            relative_path=f"src/file_{index}.c",
            text_bytes=10,
            payload={"content": {"dataSourceType": "CUSTOM", "custom": {"inlineContent": {"type": "TEXT"}}}},
        )
        for index in range(CUSTOM_BATCH_DOC_LIMIT + 1)
    ]

    batches = list(chunk_documents(documents))

    assert len(batches) == 2
    assert len(batches[0]) == CUSTOM_BATCH_DOC_LIMIT
    assert len(batches[1]) == 1

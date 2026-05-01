"""Tests for Claude Code-based conflict resolver."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from scripts.backport_models import BackportPRContext, ConflictedFile
from scripts.claude_conflict_resolver import resolve_conflicts_with_claude


def _pr_context() -> BackportPRContext:
    return BackportPRContext(
        source_pr_number=1234,
        source_pr_title="Fix memory leak in cluster.c",
        source_pr_body="Fixes a leak in clusterProcessPacket",
        source_pr_url="https://github.com/valkey-io/valkey/pull/1234",
        source_pr_diff="",
        target_branch="8.1",
        commits=["abc123"],
        repo_full_name="valkey-io/valkey",
    )


def test_whitespace_only_conflict_skips_claude(tmp_path: Path) -> None:
    cf = ConflictedFile(
        path="src/server.c",
        content_with_markers="<<<<<<< HEAD\nfoo  \n=======\nfoo\n>>>>>>> abc123",
        target_branch_content="foo  ",
        source_branch_content="foo",
    )
    results = resolve_conflicts_with_claude(str(tmp_path), [cf], _pr_context())
    assert len(results) == 1
    assert results[0].resolved_content == "foo"
    assert "whitespace" in results[0].resolution_summary


def test_claude_resolves_conflict(tmp_path: Path) -> None:
    # Write a conflicted file to disk
    src = tmp_path / "src"
    src.mkdir()
    conflicted = src / "cluster.c"
    conflicted.write_text("<<<<<<< HEAD\nold code\n=======\nnew code\n>>>>>>> abc123\n")

    cf = ConflictedFile(
        path="src/cluster.c",
        content_with_markers=conflicted.read_text(),
        target_branch_content="old code",
        source_branch_content="new code",
    )

    # Mock Claude Code to edit the file (simulate resolution)
    def mock_claude(prompt, **kw):
        # Simulate Claude editing the file
        conflicted.write_text("new code\n")
        result_event = json.dumps({"type": "result", "result": "Resolved conflict in src/cluster.c"})
        return f'{{"type":"system","subtype":"init"}}\n{result_event}', "", 0

    with patch("scripts.claude_conflict_resolver.run_claude_code", side_effect=mock_claude):
        results = resolve_conflicts_with_claude(str(tmp_path), [cf], _pr_context())

    assert len(results) == 1
    assert results[0].resolved_content == "new code\n"
    assert "Claude Code" in results[0].resolution_summary


def test_unresolved_conflict_returns_none(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    conflicted = src / "cluster.c"
    conflicted.write_text("<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> abc\n")

    cf = ConflictedFile(
        path="src/cluster.c",
        content_with_markers=conflicted.read_text(),
        target_branch_content="old",
        source_branch_content="new",
    )

    # Mock Claude Code that fails to resolve (markers remain)
    def mock_claude(prompt, **kw):
        # Claude didn't edit the file — markers remain
        return '{"type":"result","result":"I could not resolve this"}', "", 0

    with patch("scripts.claude_conflict_resolver.run_claude_code", side_effect=mock_claude):
        results = resolve_conflicts_with_claude(str(tmp_path), [cf], _pr_context())

    assert len(results) == 1
    assert results[0].resolved_content is None
    assert "markers remain" in results[0].resolution_summary


def test_mixed_whitespace_and_real_conflicts(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    real_conflict = src / "cluster.c"
    real_conflict.write_text("<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> abc\n")

    ws_file = ConflictedFile(
        path="src/server.c",
        content_with_markers="...",
        target_branch_content="foo  \n",
        source_branch_content="foo\n",
    )
    real_file = ConflictedFile(
        path="src/cluster.c",
        content_with_markers=real_conflict.read_text(),
        target_branch_content="old",
        source_branch_content="new",
    )

    def mock_claude(prompt, **kw):
        real_conflict.write_text("new\n")
        return '{"type":"result","result":"Resolved"}', "", 0

    with patch("scripts.claude_conflict_resolver.run_claude_code", side_effect=mock_claude):
        results = resolve_conflicts_with_claude(str(tmp_path), [ws_file, real_file], _pr_context())

    assert len(results) == 2
    ws_result = next(r for r in results if r.path == "src/server.c")
    real_result = next(r for r in results if r.path == "src/cluster.c")
    assert "whitespace" in ws_result.resolution_summary
    assert real_result.resolved_content == "new\n"

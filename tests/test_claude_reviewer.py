"""Tests for Claude Code-based PR reviewer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import patch

from scripts.claude_reviewer import (
    _extract_result_text,
    _parse_findings_json,
    _validate_finding,
    review_pr,
    summarize_pr,
)


@dataclass
class _FakeFile:
    path: str = "src/server.c"
    status: str = "modified"
    additions: int = 10
    deletions: int = 5
    patch: str = "@@ -1,5 +1,10 @@\n+new line"
    contents: str | None = None
    is_binary: bool = False


@dataclass
class _FakeDiffScope:
    files: list[_FakeFile] = field(default_factory=lambda: [_FakeFile()])
    incremental: bool = False


@dataclass
class _FakePR:
    number: int = 42
    title: str = "Fix memory leak"
    body: str = "Fixes a leak in cluster.c"
    base_branch: str = "unstable"
    head_sha: str = "abc123"
    files: list[_FakeFile] = field(default_factory=list)


def _agent_result(stdout: str, stderr: str = "", rc: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


def test_extract_result_text_from_jsonl():
    stream = '{"type":"system","subtype":"init"}\n{"type":"result","result":"hello world"}\n'
    assert _extract_result_text(stream) == "hello world"


def test_extract_result_text_empty():
    assert _extract_result_text("") == ""
    assert _extract_result_text("not json") == ""


def test_parse_findings_json_array():
    text = '[{"path": "a.c", "line": 1, "body": "bug"}]'
    assert len(_parse_findings_json(text)) == 1


def test_parse_findings_json_with_fences():
    text = '```json\n[{"path": "a.c", "body": "bug"}]\n```'
    assert len(_parse_findings_json(text)) == 1


def test_parse_findings_json_wrapped_in_object():
    text = '{"findings": [{"path": "a.c", "body": "bug"}]}'
    assert len(_parse_findings_json(text)) == 1


def test_parse_findings_json_empty_array():
    assert _parse_findings_json("[]") == []


def test_parse_findings_json_with_ellipsis_placeholder():
    """Regression: Claude sometimes emits '...' as a JSON-invalid placeholder.

    This is the exact failure mode from PR #3568 on the 10-PR eval
    (cost $14.10 before the fix). The repair pass should strip the
    ellipsis values and return the two well-formed findings.
    """
    text = """```json
[
    {"path": "tests/unit/geo.tcl", "line": 574, "body": "...", "severity": "low"},
    {"path": "src/hashtable.c", "line": 2326, ...}
]
```"""
    result = _parse_findings_json(text)
    assert len(result) == 2
    assert result[0]["path"] == "tests/unit/geo.tcl"
    assert result[1]["path"] == "src/hashtable.c"


def test_parse_findings_json_with_trailing_comma():
    text = '[{"path": "a.c", "body": "bug"},]'
    assert len(_parse_findings_json(text)) == 1


def test_parse_findings_json_with_comments():
    text = """[
        // This is a comment Claude added
        {"path": "a.c", "body": "bug"}
    ]"""
    assert len(_parse_findings_json(text)) == 1


def test_parse_findings_json_partial_malformed_array():
    """Only some findings in the array are malformed. Recover what we can.

    Real-world case: Claude emits two valid findings followed by a third
    object with an ellipsis-style shorthand. Repair-pass + object extraction
    should recover the well-formed ones.
    """
    text = """[
        {"path": "a.c", "line": 1, "body": "good"},
        {"path": "b.c", "line": 2, "body": "also good"},
        {"path": "c.c", "line": 3, ...}
    ]"""
    result = _parse_findings_json(text)
    assert len(result) >= 2
    paths = {r["path"] for r in result}
    assert "a.c" in paths
    assert "b.c" in paths


def test_parse_findings_json_with_preamble_text():
    text = """Here are my review findings:

```json
[{"path": "a.c", "body": "bug"}]
```"""
    assert len(_parse_findings_json(text)) == 1


def test_validate_finding_good():
    raw = {"path": "src/server.c", "line": 10, "body": "Bug here", "severity": "high", "title": "Leak"}
    result = _validate_finding(raw, {"src/server.c"})
    assert result is not None
    assert result.path == "src/server.c"
    assert result.severity == "high"


def test_validate_finding_rejects_hallucinated_path():
    raw = {"path": "src/nonexistent.c", "line": 10, "body": "Bug", "severity": "high"}
    assert _validate_finding(raw, {"src/server.c"}) is None


def test_validate_finding_normalizes_severity():
    raw = {"path": "src/server.c", "body": "Bug", "severity": "CRITICAL"}
    result = _validate_finding(raw, {"src/server.c"})
    assert result is not None
    assert result.severity == "critical"


def test_validate_finding_rejects_empty_body():
    raw = {"path": "src/server.c", "body": "", "severity": "high"}
    assert _validate_finding(raw, {"src/server.c"}) is None


def test_review_pr_returns_findings(tmp_path):
    findings_json = json.dumps([
        {"path": "src/server.c", "line": 1, "body": "This leaks memory on the error path.", "severity": "high", "title": "Leak", "confidence": "high"},
    ])
    result_event = json.dumps({"type": "result", "result": findings_json})
    stream = f'{{"type":"system","subtype":"init"}}\n{result_event}'

    with patch("scripts.claude_reviewer.run_agent", return_value=_agent_result(stream)):
        results = review_pr(_FakePR(), _FakeDiffScope(), str(tmp_path))

    assert len(results) == 1
    assert results[0].path == "src/server.c"
    assert results[0].severity == "high"


def test_review_pr_empty_diff(tmp_path):
    with patch("scripts.claude_reviewer.run_agent") as mock:
        results = review_pr(_FakePR(), _FakeDiffScope(files=[]), str(tmp_path))
    assert results == []
    mock.assert_not_called()


def test_review_pr_skeptic_drops_finding(tmp_path):
    """Stage 1 returns 2 findings, skeptic drops one as speculative."""
    stage1_json = json.dumps([
        {"path": "src/server.c", "line": 1, "body": "Real memory leak on the error return path.", "severity": "high", "title": "Leak", "confidence": "high"},
        {"path": "src/server.c", "line": 1, "body": "Also speculative race condition in handler.", "severity": "medium", "title": "Maybe race", "confidence": "medium"},
    ])
    skeptic_json = json.dumps([
        {"index": 0, "verdict": "keep", "severity": "high", "reason": "verified"},
        {"index": 1, "verdict": "drop", "severity": "medium", "reason": "speculative, no concrete trigger"},
    ])

    # The skeptic prompt contains "## Candidate findings" (unique marker),
    # not "skeptic" which leaks into both prompts.
    def mock_run_agent(*args, **kwargs):
        prompt = args[1] if len(args) > 1 else kwargs.get("prompt", "")
        if "## Candidate findings" in prompt:
            payload = skeptic_json
        else:
            payload = stage1_json
        result_event = json.dumps({"type": "result", "result": payload})
        stream = f'{{"type":"system","subtype":"init"}}\n{result_event}'
        return _agent_result(stream)

    with patch("scripts.claude_reviewer.run_agent", side_effect=mock_run_agent):
        results = review_pr(_FakePR(), _FakeDiffScope(), str(tmp_path))

    # Skeptic should have dropped the second finding
    assert len(results) == 1
    assert results[0].path == "src/server.c"
    assert results[0].body == "Real memory leak on the error return path."


def test_review_pr_skeptic_keeps_all_when_parse_fails(tmp_path):
    """If skeptic output is unparseable, keep all original findings (fail open)."""
    stage1_json = json.dumps([
        {"path": "src/server.c", "line": 1, "body": "Memory leak on the error return path.", "severity": "high", "title": "Leak", "confidence": "high"},
    ])
    skeptic_garbage = "I couldn't parse anything useful"

    def mock_run_agent(*args, **kwargs):
        prompt = args[1] if len(args) > 1 else kwargs.get("prompt", "")
        if "## Candidate findings" in prompt:
            payload = skeptic_garbage
        else:
            payload = stage1_json
        result_event = json.dumps({"type": "result", "result": payload})
        stream = f'{{"type":"system","subtype":"init"}}\n{result_event}'
        return _agent_result(stream)

    with patch("scripts.claude_reviewer.run_agent", side_effect=mock_run_agent):
        results = review_pr(_FakePR(), _FakeDiffScope(), str(tmp_path))

    # Skeptic failed, but we keep the original finding (fail open)
    assert len(results) == 1


def test_summarize_pr(tmp_path):
    result_event = json.dumps({"type": "result", "result": "This PR fixes a memory leak."})
    stream = f'{{"type":"system","subtype":"init"}}\n{result_event}'

    with patch("scripts.claude_reviewer.run_agent", return_value=_agent_result(stream)):
        summary = summarize_pr(_FakePR(), _FakeDiffScope(), str(tmp_path))

    assert "memory leak" in summary.lower()

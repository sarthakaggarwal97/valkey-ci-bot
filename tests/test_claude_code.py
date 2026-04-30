from __future__ import annotations

import subprocess

from scripts import claude_code


def test_run_claude_code_uses_edit_tools_and_bedrock_env(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)

    stdout, stderr, rc = claude_code.run_claude_code("fix this", cwd="/tmp/checkout")

    assert (stdout, stderr, rc) == ("done", "", 0)
    assert captured["cmd"][:4] == ["claude", "--print", "--max-turns", "80"]
    assert captured["cmd"][-2:] == ["--model", "opus"]
    allowed_tools = captured["cmd"][captured["cmd"].index("--allowedTools") + 1]
    assert "Edit" in allowed_tools
    assert "MultiEdit" in allowed_tools
    assert captured["kwargs"]["input"] == "fix this"
    assert captured["kwargs"]["cwd"] == "/tmp/checkout"
    assert captured["kwargs"]["env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert captured["kwargs"]["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "us.anthropic.claude-opus-4-7"
    assert captured["kwargs"]["env"]["AWS_REGION"] == "us-east-1"


def test_run_claude_code_preserves_existing_region_and_model(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="warn")

    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)

    stdout, stderr, rc = claude_code.run_claude_code("prompt", model="model-id")

    assert (stdout, stderr, rc) == ("ok", "warn", 0)
    assert captured["cmd"][-2:] == ["--model", "model-id"]
    assert captured["env"]["AWS_REGION"] == "us-west-2"


def test_run_claude_code_reports_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)

    assert claude_code.run_claude_code("prompt", timeout=3) == ("", "timeout after 3s", 1)


def test_run_claude_code_reports_missing_cli(monkeypatch):
    def fake_run(_cmd, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)

    assert claude_code.run_claude_code("prompt") == ("", "claude not found", 127)


def test_extract_diff_accepts_fenced_and_raw_diff():
    fenced = "analysis\n```diff\n--- a/a.c\n+++ b/a.c\n@@\n-old\n+new\n```\n"
    raw = "notes\n--- a/b.c\n+++ b/b.c\n@@\n-old\n+new\n"

    assert claude_code.extract_diff(fenced).startswith("--- a/a.c")
    assert claude_code.extract_diff(raw).startswith("--- a/b.c")
    assert claude_code.extract_diff("no patch here") is None

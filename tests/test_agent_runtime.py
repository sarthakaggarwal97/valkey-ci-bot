from __future__ import annotations

import json

from scripts import agent_runtime


def test_run_agent_applies_profile_and_writes_hashed_evidence(tmp_path, monkeypatch) -> None:
    calls = {}

    def fake_run_claude_code(prompt, **kwargs):
        calls["prompt"] = prompt
        calls.update(kwargs)
        return "secret stdout", "secret stderr", 0

    monkeypatch.setattr(agent_runtime, "run_claude_code", fake_run_claude_code)

    result = agent_runtime.run_agent(
        "review_readonly",
        "review this",
        cwd="/tmp/repo",
        evidence_dir=tmp_path,
    )

    assert result.returncode == 0
    assert calls["allowed_tools"] == "Read,Grep,Glob"
    assert "GITHUB_TOKEN" not in calls["env_allowlist"]
    assert calls["timeout"] == agent_runtime.AGENT_PROFILES["review_readonly"].timeout
    assert calls["effort"] == "max"

    evidence_files = list(tmp_path.glob("*.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
    assert evidence["profile"]["name"] == "review_readonly"
    assert "stdout" not in evidence["result"]
    assert "stderr" not in evidence["result"]
    assert "secret stdout" not in evidence_files[0].read_text(encoding="utf-8")

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scripts import fix_loop
from scripts.fix_loop import run_fix_loop


@dataclass
class _StubFailureReport:
    job_name: str = "test-job"
    parsed_failures: list = field(default_factory=list)
    run_url: str = ""
    raw_log: str = ""


@dataclass
class _StubRootCause:
    summary: str = "x"
    category: str = "test"
    affected_areas: list = field(default_factory=list)
    patch_hint: str = ""


def _fresh_checkout(tmp_path):
    """Pretend the caller already has a repo checkout."""
    checkout = tmp_path / "preexisting-checkout"
    checkout.mkdir()
    return str(checkout)


@patch("scripts.fix_loop.poll_run", return_value=(True, "success", "https://example/run/1"))
@patch("scripts.fix_loop.dispatch_validation", return_value=42)
@patch("scripts.fix_loop._comment_on_issue")
@patch("scripts.fix_loop._run")
@patch("scripts.fix_loop._generate_fix", return_value="diff --git a/x b/x\n")
def test_push_uses_gitauth_env_when_caller_supplies_checkout(
    _mock_gen, mock_run, _mock_comment, _mock_dispatch, _mock_poll, tmp_path,
) -> None:
    """Regression: when the caller passes ``repo_checkout``, the push must
    still go through ``GitAuth.env()``.

    Previously, ``GitAuth`` was only created in the else branch that clones,
    so a caller-supplied checkout left ``git_auth=None`` and the push fell
    back to ``env=None``. The token was neither in the URL (clean fork URL)
    nor in the env, so the push failed for every caller-supplied checkout.
    """
    checkout = _fresh_checkout(tmp_path)

    run_fix_loop(
        report=_StubFailureReport(),
        root_cause=_StubRootCause(),
        fork_repo="owner/repo",
        fork_token="ghp_test_token",
        base_sha="deadbeef1234",
        test_file="tests/unit/a.tcl",
        job_name="test-job",
        repo_checkout=checkout,
        max_attempts=1,
    )

    # Find the ``git push`` invocation and assert it carried an env with GIT_ASKPASS
    push_calls = [
        c for c in mock_run.call_args_list
        if c.args and isinstance(c.args[0], list) and len(c.args[0]) >= 2
        and c.args[0][0] == "git" and c.args[0][1] == "push"
    ]
    assert push_calls, "expected at least one `git push` invocation"
    push_env = push_calls[0].kwargs.get("env")
    assert push_env is not None, "push was made without GitAuth env"
    assert push_env.get("GIT_ASKPASS"), "GIT_ASKPASS missing from push env"
    assert push_env.get("GIT_TERMINAL_PROMPT") == "0"


@patch("scripts.fix_loop.poll_run", return_value=(True, "success", "https://example/run/1"))
@patch("scripts.fix_loop.dispatch_validation", return_value=42)
@patch("scripts.fix_loop._comment_on_issue")
@patch("scripts.fix_loop._run")
@patch("scripts.fix_loop._generate_fix", return_value="diff --git a/x b/x\n")
def test_push_uses_gitauth_env_when_fix_loop_clones(
    _mock_gen, mock_run, _mock_comment, _mock_dispatch, _mock_poll, tmp_path,
) -> None:
    """GitAuth env must also be used when fix_loop does the clone itself."""
    run_fix_loop(
        report=_StubFailureReport(),
        root_cause=_StubRootCause(),
        fork_repo="owner/repo",
        fork_token="ghp_test_token",
        base_sha="deadbeef1234",
        test_file="tests/unit/a.tcl",
        job_name="test-job",
        repo_checkout="",  # no caller checkout -- fix_loop will clone
        max_attempts=1,
    )

    push_calls = [
        c for c in mock_run.call_args_list
        if c.args and isinstance(c.args[0], list) and len(c.args[0]) >= 2
        and c.args[0][0] == "git" and c.args[0][1] == "push"
    ]
    assert push_calls, "expected at least one `git push` invocation"
    push_env = push_calls[0].kwargs.get("env")
    assert push_env is not None and push_env.get("GIT_ASKPASS")


def test_generate_fix_captures_new_files_in_patch(monkeypatch, tmp_path) -> None:
    checkout = _fresh_checkout(tmp_path)
    git_calls: list[list[str]] = []

    monkeypatch.setattr(fix_loop, "_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        fix_loop,
        "run_agent",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="created src/new.c",
            stderr="",
            returncode=0,
        ),
    )

    def fake_git(cmd, **_kwargs):
        git_calls.append(cmd)
        if cmd == ["git", "add", "-N", "."]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        assert cmd == ["git", "diff", "--binary"]
        return SimpleNamespace(
            stdout=(
                "diff --git a/src/new.c b/src/new.c\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/src/new.c\n"
                "@@ -0,0 +1 @@\n"
                "+int fixed;\n"
            ),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(fix_loop.subprocess, "run", fake_git)

    patch = fix_loop._generate_fix(
        SimpleNamespace(job_name="test-job", parsed_failures=[]),
        SimpleNamespace(description="missing helper", files_to_change=["src/new.c"]),
        "",
        checkout,
    )

    assert "new file mode" in patch
    assert ["git", "add", "-N", "."] in git_calls
    assert ["git", "diff", "--binary"] in git_calls


def test_generate_fix_rejects_nonzero_claude_even_with_edits(monkeypatch, tmp_path) -> None:
    checkout = _fresh_checkout(tmp_path)

    monkeypatch.setattr(fix_loop, "_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        fix_loop,
        "run_agent",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="edited then failed",
            stderr="tool failed",
            returncode=1,
        ),
    )

    def fake_git(cmd, **_kwargs):
        return SimpleNamespace(
            stdout="diff --git a/src/a.c b/src/a.c\n--- a/src/a.c\n+++ b/src/a.c\n",
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(fix_loop.subprocess, "run", fake_git)

    patch = fix_loop._generate_fix(
        SimpleNamespace(job_name="test-job", parsed_failures=[]),
        SimpleNamespace(description="missing helper", files_to_change=["src/a.c"]),
        "",
        checkout,
    )

    assert patch is None

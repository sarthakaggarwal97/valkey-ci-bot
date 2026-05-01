from __future__ import annotations

from types import SimpleNamespace

from scripts import claude_fix
from scripts.models import ParsedFailure


def _parsed_failure() -> ParsedFailure:
    return ParsedFailure(
        failure_identifier="suite.test",
        test_name="suite.test",
        file_path="tests/unit.tcl",
        error_message="expected OK",
        assertion_details=None,
        line_number=12,
        stack_trace=None,
        parser_type="tcl",
    )


class _FakeIssue:
    def __init__(self):
        self.comments: list[str] = []

    def create_comment(self, body: str):
        self.comments.append(body)


class _FakeRepo:
    def __init__(self):
        self.issue = _FakeIssue()

    def get_issue(self, _number: int):
        return self.issue


class _FakeGithub:
    def __init__(self):
        self.repo = _FakeRepo()

    def get_repo(self, _repo: str):
        return self.repo


def test_fix_from_log_fetches_full_log_and_synthesizes_failure(monkeypatch):
    prompts: list[str] = []
    issue_calls: list[dict] = []
    retriever_tokens: list[str | None] = []

    class FakeLogRetriever:
        def __init__(self, _gh, *, token=None):
            retriever_tokens.append(token)

        def get_job_log(self, repo_full_name: str, job_id: int) -> str:
            assert repo_full_name == "valkey-io/valkey"
            assert job_id == 99
            return "setup\nERROR: replica crash in integration test\nfull job log tail\n"

    def fake_run_agent(_profile, prompt, **_kwargs):
        prompts.append(prompt)
        return SimpleNamespace(stdout="edited files", stderr="", returncode=0)

    def fake_git_diff(cmd, **_kwargs):
        assert cmd == ["git", "diff"]
        return SimpleNamespace(stdout="diff --git a/test b/test\n+fix\n", stderr="", returncode=0)

    def fake_issue(_gh, repo, parsed_failure, report, run_url):
        issue_calls.append(
            {
                "repo": repo,
                "failure": parsed_failure,
                "report": report,
                "run_url": run_url,
            }
        )
        return "https://example.test/issues/1", 1, True

    monkeypatch.setattr(claude_fix, "LogRetriever", FakeLogRetriever)
    monkeypatch.setattr(claude_fix, "run_agent", fake_run_agent)
    monkeypatch.setattr(claude_fix, "_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claude_fix.subprocess, "run", fake_git_diff)
    monkeypatch.setattr(claude_fix, "create_or_update_issue", fake_issue)
    monkeypatch.setattr(claude_fix, "_open_draft_pr", lambda **_kwargs: "https://example.test/pr/1")

    result = claude_fix.fix_from_log(
        job_name="test fedora tls",
        log_excerpt="short parser excerpt",
        parsed_failures=[],
        fork_repo="me/valkey",
        fork_token="fork-token",
        base_sha="abcdef123456",
        target_branch="unstable",
        run_url="https://example.test/run",
        gh=_FakeGithub(),
        job_id=99,
        repo_full_name="valkey-io/valkey",
        target_token="target-token",
    )

    assert result["outcome"] == "pr-created"
    assert result["pr_url"] == "https://example.test/pr/1"
    assert retriever_tokens == ["target-token"]
    assert "full job log tail" in prompts[0]
    assert "Treat this as a hint only" in prompts[0]
    assert issue_calls[0]["failure"].parser_type == "claude-log"
    assert issue_calls[0]["failure"].test_name == "test fedora tls"


def test_fix_from_log_creates_issue_with_stderr_when_claude_edits_nothing(monkeypatch):
    gh = _FakeGithub()

    monkeypatch.setattr(claude_fix, "_fetch_job_log", lambda **_kwargs: "")
    monkeypatch.setattr(
        claude_fix,
        "run_agent",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="max turns",
            stderr="stderr text",
            returncode=1,
        ),
    )
    monkeypatch.setattr(claude_fix, "_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        claude_fix.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="", stderr="", returncode=0),
    )
    monkeypatch.setattr(
        claude_fix,
        "create_or_update_issue",
        lambda *_args, **_kwargs: ("https://example.test/issues/2", 2, False),
    )

    result = claude_fix.fix_from_log(
        job_name="test valgrind",
        log_excerpt="valgrind failure",
        parsed_failures=[_parsed_failure()],
        fork_repo="me/valkey",
        fork_token="fork-token",
        base_sha="abcdef123456",
        target_branch="unstable",
        run_url="https://example.test/run",
        gh=gh,
    )

    assert result["outcome"] == "no-fix-generated"
    assert result["claude_exit_code"] == 1
    assert "without editing files" in result["error"]
    assert "stderr text" in gh.repo.issue.comments[0]
    assert "Exit code: `1`" in gh.repo.issue.comments[0]


def test_compact_log_keeps_failure_markers_and_tail():
    long_log = "\n".join(
        ["setup line"] * 500
        + ["ERROR: exact failure marker"]
        + ["middle line"] * 500
        + ["tail line"] * 500
    )

    compact = claude_fix._compact_log_for_prompt(long_log, max_chars=5000)

    assert len(compact) <= 5000
    assert "ERROR: exact failure marker" in compact
    assert "tail line" in compact


def test_open_draft_pr_builds_body(monkeypatch):
    captured = {}

    def fake_retry(fn, **_kwargs):
        return fn()

    def fake_upsert(repo_obj, **kwargs):
        captured["repo"] = repo_obj
        captured["kwargs"] = kwargs
        return SimpleNamespace(html_url="https://example.test/pr/3")

    import scripts.github_client as github_client
    import scripts.pr_manager as pr_manager

    monkeypatch.setattr(claude_fix, "check_publish_allowed", lambda **_kwargs: None)
    monkeypatch.setattr(github_client, "retry_github_call", fake_retry)
    monkeypatch.setattr(pr_manager, "upsert_pull_request", fake_upsert)

    url = claude_fix._open_draft_pr(
        gh=_FakeGithub(),
        fork_repo="me/valkey",
        branch_name="bot/fix/test-abcdef12",
        target_branch="unstable",
        job_name="test job",
        run_url="https://example.test/run",
        base_sha="abcdef123456",
        first_pf=_parsed_failure(),
        issue_number=2,
        stdout="analysis",
    )

    assert url == "https://example.test/pr/3"
    assert captured["kwargs"]["draft"] is True
    assert captured["kwargs"]["base"] == "unstable"
    assert "Fixes #2" in captured["kwargs"]["body"]
    assert "analysis" in captured["kwargs"]["body"]

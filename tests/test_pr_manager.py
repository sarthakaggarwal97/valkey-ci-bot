"""Tests for the PR Manager module."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from github.GithubException import GithubException

from scripts.commit_signoff import CommitSigner
from scripts.failure_store import FailureStore
from scripts.models import FailureReport, ParsedFailure, RootCauseReport
from scripts.pr_manager import (
    PRManager,
    _apply_hunks,
    _build_commit_message,
    _build_pr_body,
    _compute_fingerprint,
    _parse_unified_diff,
    upsert_pull_request,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_parsed_failure(**overrides) -> ParsedFailure:
    defaults = dict(
        failure_identifier="TestSuite.TestCase",
        test_name="TestCase",
        file_path="src/foo.c",
        error_message="Expected 1 got 0",
        assertion_details="ASSERT_EQ(1, 0)",
        line_number=42,
        stack_trace=None,
        parser_type="gtest",
    )
    defaults.update(overrides)
    return ParsedFailure(**defaults)


def _make_failure_report(**overrides) -> FailureReport:
    defaults = dict(
        workflow_name="CI",
        job_name="test-ubuntu-latest",
        matrix_params={"os": "ubuntu-latest"},
        commit_sha="abc123def456",
        failure_source="trusted",
        repo_full_name="owner/repo",
        workflow_run_id=123,
        target_branch="unstable",
        parsed_failures=[_make_parsed_failure()],
        raw_log_excerpt=None,
        is_unparseable=False,
    )
    defaults.update(overrides)
    return FailureReport(**defaults)


def _make_root_cause(**overrides) -> RootCauseReport:
    defaults = dict(
        description="Off-by-one error in loop boundary",
        files_to_change=["src/foo.c"],
        confidence="high",
        rationale="The loop iterates one too many times",
        is_flaky=False,
        flakiness_indicators=None,
    )
    defaults.update(overrides)
    return RootCauseReport(**defaults)


SAMPLE_PATCH = """\
--- a/src/foo.c
+++ b/src/foo.c
@@ -1,3 +1,3 @@
 void foo() {
-    for (int i = 0; i <= n; i++) {
+    for (int i = 0; i < n; i++) {
     }
"""

MULTI_FILE_PATCH = """\
--- a/src/foo.c
+++ b/src/foo.c
@@ -1,3 +1,3 @@
 void foo() {
-    for (int i = 0; i <= n; i++) {
+    for (int i = 0; i < n; i++) {
     }
--- a/src/bar.c
+++ b/src/bar.c
@@ -1,2 +1,3 @@
 void bar() {
+    return;
 }
"""


def _make_mock_repo(full_name: str = "owner/repo", owner_login: str = "owner"):
    """Create a mock GitHub repo with the methods PRManager needs."""
    repo = MagicMock()
    repo.full_name = full_name
    repo.owner.login = owner_login

    base_ref = MagicMock()
    base_ref.object.sha = "aabbccdd11223344"
    branch_ref = MagicMock()

    def get_git_ref(name: str):
        if name == "heads/unstable":
            return base_ref
        if name.startswith("heads/bot/fix/"):
            return branch_ref
        raise AssertionError(f"Unexpected ref lookup: {name}")

    repo.get_git_ref.side_effect = get_git_ref
    repo.base_ref = base_ref
    repo.branch_ref = branch_ref

    # create_git_ref succeeds
    repo.create_git_ref.return_value = MagicMock()

    def get_contents(path: str, ref: str | None = None):
        if path == "src/foo.c":
            contents = MagicMock()
            contents.decoded_content = (
                b"void foo() {\n    for (int i = 0; i <= n; i++) {\n    }\n"
            )
            return contents
        if path == "src/bar.c":
            contents = MagicMock()
            contents.decoded_content = b"void bar() {\n}\n"
            return contents
        raise FileNotFoundError(path)

    repo.get_contents.side_effect = get_contents

    base_commit = MagicMock()
    base_commit.sha = "abc123def456"
    base_commit.tree = MagicMock()
    repo.get_git_commit.return_value = base_commit

    new_tree = MagicMock()
    new_tree.sha = "tree123"
    repo.create_git_tree.return_value = new_tree

    new_commit = MagicMock()
    new_commit.sha = "commit123"
    repo.create_git_commit.return_value = new_commit

    repo.base_commit = base_commit
    repo.new_commit = new_commit

    # create_pull returns a PR mock
    pr = MagicMock()
    pr.number = 42
    pr.html_url = "https://github.com/owner/repo/pull/42"
    repo.create_pull.return_value = pr

    return repo


def _make_pr_manager(repo=None, failure_store=None):
    """Create a PRManager with mocked dependencies."""
    gh = MagicMock()
    if repo is None:
        repo = _make_mock_repo()
    gh.get_repo.return_value = repo

    if failure_store is None:
        failure_store = FailureStore()

    return PRManager(gh, "owner/repo", failure_store), repo, failure_store


# ---------------------------------------------------------------------------
# Unit tests: helper functions
# ---------------------------------------------------------------------------

class TestComputeFingerprint:
    def test_uses_first_parsed_failure(self):
        report = _make_failure_report()
        fp = _compute_fingerprint(report)
        expected = FailureStore.compute_incident_key(
            "TestSuite.TestCase",
            "src/foo.c",
            test_name="TestCase",
        )
        assert fp == expected

    def test_fallback_for_unparseable(self):
        report = _make_failure_report(
            parsed_failures=[], raw_log_excerpt="some log", is_unparseable=True
        )
        fp = _compute_fingerprint(report)
        expected = FailureStore.compute_incident_key(
            "test-ubuntu-latest",
            "",
        )
        assert fp == expected


class TestBuildCommitMessage:
    def test_includes_test_name_and_job(self):
        report = _make_failure_report()
        root_cause = _make_root_cause()
        msg = _build_commit_message(report, root_cause)
        assert "TestCase" in msg
        assert "test-ubuntu-latest" in msg
        assert "Off-by-one" in msg

    def test_uses_failure_identifier_when_no_test_name(self):
        pf = _make_parsed_failure(test_name=None)
        report = _make_failure_report(parsed_failures=[pf])
        root_cause = _make_root_cause()
        msg = _build_commit_message(report, root_cause)
        assert "TestSuite.TestCase" in msg

    def test_fallback_to_job_name_for_unparseable(self):
        report = _make_failure_report(parsed_failures=[])
        root_cause = _make_root_cause()
        msg = _build_commit_message(report, root_cause)
        assert "test-ubuntu-latest" in msg

    def test_appends_dco_signoff_when_signer_is_provided(self):
        report = _make_failure_report()
        root_cause = _make_root_cause()

        msg = _build_commit_message(
            report,
            root_cause,
            CommitSigner(name="Val Key", email="valkey@example.com"),
            require_dco_signoff=True,
        )

        assert "Signed-off-by: Val Key <valkey@example.com>" in msg


class TestBuildPRBody:
    def test_contains_required_sections(self):
        report = _make_failure_report()
        root_cause = _make_root_cause()
        body = _build_pr_body(report, root_cause, "https://github.com/o/r/actions/runs/123")

        # Req 6.4: link to failing CI run
        assert "https://github.com/o/r/actions/runs/123" in body
        # Req 6.4: parsed failure summary
        assert "TestSuite.TestCase" in body
        assert "src/foo.c" in body
        # Req 6.4: root cause analysis
        assert "Off-by-one error" in body
        # Req 6.4: confidence level
        assert "high" in body
        # Req 6.4: AI disclaimer
        assert "AI agent" in body
        assert "human review" in body

    def test_unparseable_failure_body(self):
        report = _make_failure_report(
            parsed_failures=[], raw_log_excerpt="error: boom", is_unparseable=True
        )
        root_cause = _make_root_cause()
        body = _build_pr_body(report, root_cause, "https://example.com/run/1")
        assert "error: boom" in body
        assert "could not be parsed" in body.lower()


# ---------------------------------------------------------------------------
# Unit tests: diff parsing and application
# ---------------------------------------------------------------------------

class TestParseUnifiedDiff:
    def test_parses_single_file_single_hunk(self):
        result = _parse_unified_diff(SAMPLE_PATCH)
        assert "src/foo.c" in result
        hunks = result["src/foo.c"]
        assert len(hunks) == 1
        assert hunks[0]["old_start"] == 1

    def test_parses_multiple_files(self):
        multi_patch = (
            "--- a/src/a.c\n+++ b/src/a.c\n@@ -1,1 +1,1 @@\n-old\n+new\n"
            "--- a/src/b.c\n+++ b/src/b.c\n@@ -5,1 +5,1 @@\n-x\n+y\n"
        )
        result = _parse_unified_diff(multi_patch)
        assert "src/a.c" in result
        assert "src/b.c" in result

    def test_empty_patch(self):
        result = _parse_unified_diff("")
        assert result == {}


class TestApplyHunks:
    def test_applies_simple_replacement(self):
        original = "line1\nline2\nline3\n"
        hunks = [{
            "old_start": 2,
            "old_count": 1,
            "new_start": 2,
            "new_count": 1,
            "lines": ["-line2", "+replaced"],
        }]
        result = _apply_hunks(original, hunks)
        assert "replaced" in result
        assert "line2" not in result

    def test_applies_addition(self):
        original = "a\nb\n"
        hunks = [{
            "old_start": 2,
            "old_count": 1,
            "new_start": 2,
            "new_count": 2,
            "lines": [" b", "+c"],
        }]
        result = _apply_hunks(original, hunks)
        assert "c" in result

    def test_empty_hunks_returns_original(self):
        assert _apply_hunks("hello", []) == "hello"

    def test_new_file(self):
        hunks = [{
            "old_start": 0,
            "old_count": 0,
            "new_start": 1,
            "new_count": 2,
            "lines": ["+line1", "+line2"],
        }]
        result = _apply_hunks("", hunks)
        assert "line1" in result
        assert "line2" in result

    def test_context_mismatch_fails_closed(self):
        original = "line1\nactual\nline3\n"
        hunks = [{
            "old_start": 2,
            "old_count": 1,
            "new_start": 2,
            "new_count": 1,
            "lines": [" expected"],
        }]

        with pytest.raises(ValueError, match="Patch context mismatch"):
            _apply_hunks(original, hunks)

    def test_deletion_mismatch_fails_closed(self):
        original = "line1\nactual\nline3\n"
        hunks = [{
            "old_start": 2,
            "old_count": 1,
            "new_start": 2,
            "new_count": 0,
            "lines": ["-expected"],
        }]

        with pytest.raises(ValueError, match="Patch deletion mismatch"):
            _apply_hunks(original, hunks)


# ---------------------------------------------------------------------------
# Unit tests: PRManager.create_pr
# ---------------------------------------------------------------------------

class TestCreatePR:
    def test_successful_pr_creation(self):
        """Full happy path: branch, commit, PR, label, store."""
        mgr, repo, store = _make_pr_manager()
        report = _make_failure_report()
        root_cause = _make_root_cause()

        url = mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        assert url == "https://github.com/owner/repo/pull/42"

        # Branch created from target
        repo.get_git_ref.assert_any_call("heads/unstable")
        repo.create_git_ref.assert_called_once()
        ref_arg = repo.create_git_ref.call_args
        assert "bot/fix/" in ref_arg.kwargs.get("ref", ref_arg[1].get("ref", ""))
        assert ref_arg.kwargs.get("sha", ref_arg[1].get("sha")) == report.commit_sha
        repo.create_git_commit.assert_called_once()
        repo.branch_ref.edit.assert_called_once_with(repo.new_commit.sha)

        # PR opened
        repo.create_pull.assert_called_once()
        call_kwargs = repo.create_pull.call_args.kwargs
        assert call_kwargs["base"] == "unstable"
        assert "[bot-fix]" in call_kwargs["title"]

        # Label applied
        pr_mock = repo.create_pull.return_value
        pr_mock.add_to_labels.assert_called_once_with("bot-fix")

        # Recorded in failure store
        fp = _compute_fingerprint(report)
        assert fp in store.entries
        assert store.entries[fp].status == "open"
        assert store.entries[fp].pr_url == url

    def test_branch_name_uses_fingerprint(self):
        """Req 6.1: branch named bot/fix/<fingerprint>."""
        mgr, repo, _ = _make_pr_manager()
        report = _make_failure_report()
        root_cause = _make_root_cause()

        mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        fp = _compute_fingerprint(report)
        ref_call = repo.create_git_ref.call_args
        created_ref = ref_call.kwargs.get("ref") or ref_call[1].get("ref")
        assert created_ref == f"refs/heads/bot/fix/{fp}"

    def test_can_open_draft_pr(self):
        mgr, repo, _ = _make_pr_manager()
        report = _make_failure_report()
        root_cause = _make_root_cause()

        mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable", draft=True)

        assert repo.create_pull.call_args.kwargs["draft"] is True

    def test_pr_body_contains_required_content(self):
        """Req 6.4: PR body has CI link, summary, root cause, confidence, disclaimer."""
        mgr, repo, _ = _make_pr_manager()
        report = _make_failure_report()
        root_cause = _make_root_cause()

        mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        call_kwargs = repo.create_pull.call_args.kwargs
        body = call_kwargs["body"]
        assert "actions/runs/123" in body
        assert "abc123def456" not in body
        assert "TestSuite.TestCase" in body
        assert "Off-by-one" in body
        assert "high" in body
        assert "AI agent" in body

    def test_commit_message_content(self):
        """Req 6.2: commit message includes identifier, job name, root cause."""
        mgr, repo, _ = _make_pr_manager()
        report = _make_failure_report()
        root_cause = _make_root_cause()

        mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        call_args = repo.create_git_commit.call_args
        commit_msg = call_args.args[0]
        assert "TestCase" in commit_msg
        assert "test-ubuntu-latest" in commit_msg
        assert "Off-by-one" in commit_msg

    def test_multi_file_patch_creates_single_commit(self):
        mgr, repo, _ = _make_pr_manager()
        report = _make_failure_report()
        root_cause = _make_root_cause(files_to_change=["src/foo.c", "src/bar.c"])

        mgr.create_pr(MULTI_FILE_PATCH, report, root_cause, "unstable")

        repo.create_git_tree.assert_called_once()
        repo.create_git_commit.assert_called_once()
        tree_elements = repo.create_git_tree.call_args.args[0]
        assert len(tree_elements) == 2
        assert {
            element._InputGitTreeElement__path for element in tree_elements
        } == {"src/foo.c", "src/bar.c"}

    def test_falls_back_to_fork_when_upstream_write_is_denied(self):
        upstream_repo = _make_mock_repo(full_name="owner/repo", owner_login="owner")
        fork_repo = _make_mock_repo(full_name="forker/repo", owner_login="forker")
        upstream_repo.create_git_ref.side_effect = GithubException(
            403,
            {"message": "Resource not accessible by integration"},
        )
        upstream_repo.create_fork.return_value = fork_repo
        mgr, repo, store = _make_pr_manager(repo=upstream_repo)
        report = _make_failure_report()
        root_cause = _make_root_cause()

        url = mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        assert url == "https://github.com/owner/repo/pull/42"
        upstream_repo.create_fork.assert_called_once()
        fork_repo.create_git_ref.assert_called_once()
        fork_repo.create_git_commit.assert_called_once()
        upstream_repo.create_pull.assert_called_once()
        assert upstream_repo.create_pull.call_args.kwargs["head"].startswith("forker:bot/fix/")
        fp = _compute_fingerprint(report)
        assert store.entries[fp].pr_url == url

    def test_reuses_existing_open_pr_for_same_head(self):
        mgr, repo, store = _make_pr_manager()
        existing_pr = MagicMock()
        existing_pr.number = 42
        existing_pr.html_url = "https://github.com/owner/repo/pull/42"
        repo.get_pulls.return_value = [existing_pr]
        report = _make_failure_report()
        root_cause = _make_root_cause()

        url = mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        assert url == existing_pr.html_url
        repo.create_pull.assert_not_called()
        fp = _compute_fingerprint(report)
        assert store.entries[fp].pr_url == existing_pr.html_url

    def test_existing_branch_is_reset_before_patch_application(self):
        repo = _make_mock_repo()
        repo.create_git_ref.side_effect = GithubException(
            422,
            {"message": "Reference already exists"},
        )
        mgr, _, _ = _make_pr_manager(repo=repo)
        report = _make_failure_report()
        root_cause = _make_root_cause()

        mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        repo.branch_ref.edit.assert_any_call(report.commit_sha, force=True)
        repo.branch_ref.edit.assert_any_call(repo.new_commit.sha)


class TestForkPRSkip:
    def test_skips_fork_pr_failures(self):
        """Req 6.3: skip PR creation for fork failures, log fork-pr-no-write-access."""
        mgr, repo, _ = _make_pr_manager()
        report = _make_failure_report(failure_source="untrusted-fork")
        root_cause = _make_root_cause()

        with pytest.raises(ValueError, match="fork-pr-no-write-access"):
            mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        # No GitHub API calls should have been made
        repo.get_git_ref.assert_not_called()
        repo.create_pull.assert_not_called()

    def test_fork_skip_is_logged(self, caplog):
        """Verify the fork skip reason is logged."""
        mgr, _, _ = _make_pr_manager()
        report = _make_failure_report(failure_source="untrusted-fork")
        root_cause = _make_root_cause()

        with caplog.at_level(logging.WARNING):
            with pytest.raises(ValueError):
                mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        assert any("fork-pr-no-write-access" in r.message for r in caplog.records)


class TestGitHubAPIRejection:
    def test_handles_api_error_gracefully(self):
        """Req 6.7: GitHub API rejection → pr-creation-failed."""
        repo = _make_mock_repo()
        repo.create_pull.side_effect = Exception("Branch protection rule violation")
        mgr, _, store = _make_pr_manager(repo=repo)
        report = _make_failure_report()
        root_cause = _make_root_cause()

        with pytest.raises(RuntimeError, match="pr-creation-failed"):
            mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        # Failure recorded in store
        fp = _compute_fingerprint(report)
        assert fp in store.entries
        assert store.entries[fp].status == "pr-creation-failed"

    def test_handles_branch_creation_error(self):
        """API error during branch creation is handled."""
        repo = _make_mock_repo()
        repo.create_git_ref.side_effect = Exception("Permission denied")
        mgr, _, store = _make_pr_manager(repo=repo)
        report = _make_failure_report()
        root_cause = _make_root_cause()

        with pytest.raises(RuntimeError, match="pr-creation-failed"):
            mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        fp = _compute_fingerprint(report)
        assert store.entries[fp].status == "pr-creation-failed"

    def test_api_error_is_logged(self, caplog):
        """Verify API errors are logged with pr-creation-failed."""
        repo = _make_mock_repo()
        repo.create_pull.side_effect = Exception("403 Forbidden")
        mgr, _, _ = _make_pr_manager(repo=repo)
        report = _make_failure_report()
        root_cause = _make_root_cause()

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        assert any("pr-creation-failed" in r.message for r in caplog.records)

    def test_non_404_file_load_error_does_not_become_new_file(self):
        repo = _make_mock_repo()
        repo.get_contents.side_effect = GithubException(500, {"message": "boom"})
        mgr, _, store = _make_pr_manager(repo=repo)
        report = _make_failure_report()
        root_cause = _make_root_cause()

        with pytest.raises(RuntimeError, match="pr-creation-failed"):
            mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")

        repo.create_git_commit.assert_not_called()
        fp = _compute_fingerprint(report)
        assert store.entries[fp].status == "pr-creation-failed"


class TestLabelApplication:
    def test_label_failure_is_non_fatal(self):
        """Label application failure should not prevent PR creation."""
        repo = _make_mock_repo()
        pr_mock = repo.create_pull.return_value
        pr_mock.add_to_labels.side_effect = Exception("Label not found")
        mgr, _, _ = _make_pr_manager(repo=repo)
        report = _make_failure_report()
        root_cause = _make_root_cause()

        # Should succeed despite label failure
        url = mgr.create_pr(SAMPLE_PATCH, report, root_cause, "unstable")
        assert url == pr_mock.html_url


class TestUpsertPullRequest:
    def test_creates_pull_request_when_none_exists(self):
        repo = _make_mock_repo()
        repo.get_pulls.return_value = []

        pr = upsert_pull_request(
            repo,
            head="owner:bot/fix/fp1",
            base="unstable",
            title="Title",
            body="Body",
            draft=False,
            labels=("bot-fix",),
        )

        assert pr is repo.create_pull.return_value
        repo.create_pull.assert_called_once()
        repo.create_pull.return_value.add_to_labels.assert_called_once_with("bot-fix")

    def test_reuses_and_updates_existing_pull_request(self):
        repo = _make_mock_repo()
        existing_pr = MagicMock()
        existing_pr.number = 42
        existing_pr.title = "Old title"
        existing_pr.body = "Old body"
        repo.get_pulls.return_value = [existing_pr]

        pr = upsert_pull_request(
            repo,
            head="owner:bot/fix/fp1",
            base="unstable",
            title="New title",
            body="New body",
            draft=False,
        )

        assert pr is existing_pr
        repo.create_pull.assert_not_called()
        existing_pr.edit.assert_called_once_with(title="New title", body="New body")


class TestPostSummaryComment:
    """Tests for PRManager.post_summary_comment() — Requirement 11.2."""

    def test_posts_comment_on_pr(self):
        from scripts.summary import PRSummaryComment

        mock_pr = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        store = MagicMock(spec=FailureStore)
        pm = PRManager(mock_gh, "owner/repo", store)

        comment = PRSummaryComment()
        comment.add_step("detection", 1.0)
        comment.add_step("parsing", 0.5)

        pm.post_summary_comment("https://github.com/owner/repo/pull/42", comment)

        mock_repo.get_pull.assert_called_once_with(42)
        mock_pr.create_issue_comment.assert_called_once()
        body = mock_pr.create_issue_comment.call_args[1]["body"]
        assert "Processing Summary" in body
        assert "detection" in body
        assert "parsing" in body

    def test_comment_includes_retries(self):
        from scripts.summary import PRSummaryComment

        mock_pr = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        store = MagicMock(spec=FailureStore)
        pm = PRManager(mock_gh, "owner/repo", store)

        comment = PRSummaryComment(fix_retries=2, validation_retries=1)
        comment.add_step("generation", 5.0)

        pm.post_summary_comment("https://github.com/owner/repo/pull/10", comment)

        body = mock_pr.create_issue_comment.call_args[1]["body"]
        assert "Fix generation retries:** 2" in body
        assert "Validation retries:** 1" in body

    def test_api_failure_is_non_fatal(self, caplog):
        from scripts.summary import PRSummaryComment

        mock_repo = MagicMock()
        mock_repo.get_pull.side_effect = RuntimeError("API error")

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        store = MagicMock(spec=FailureStore)
        pm = PRManager(mock_gh, "owner/repo", store)

        comment = PRSummaryComment()
        comment.add_step("detection", 1.0)

        # Should not raise
        pm.post_summary_comment("https://github.com/owner/repo/pull/99", comment)

        assert "Failed to post summary comment" in caplog.text

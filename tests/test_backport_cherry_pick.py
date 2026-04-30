"""Unit tests for CherryPickExecutor.

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, call, mock_open, patch

import pytest

from scripts.backport_models import CherryPickResult, ConflictedFile
from scripts.cherry_pick import CherryPickExecutor


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess."""
    return subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout=stdout, stderr=stderr,
    )


def _fail(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Return a failed CompletedProcess."""
    return subprocess.CompletedProcess(
        args=["git"], returncode=1, stdout=stdout, stderr=stderr,
    )


def _merge_commit() -> subprocess.CompletedProcess[str]:
    """Return rev-list output for a two-parent merge commit."""
    return _ok(stdout="merge_sha parent_a parent_b\n")


def _single_parent_commit() -> subprocess.CompletedProcess[str]:
    """Return rev-list output for a normal single-parent commit."""
    return _ok(stdout="commit_sha parent_a\n")


class TestCleanCherryPickWithMergeCommit:
    """Scenario 1: Clean cherry-pick using merge commit SHA."""

    @patch("scripts.cherry_pick.subprocess.run")
    def test_returns_success(self, mock_run: MagicMock) -> None:
        # checkout succeeds, cherry-pick -m 1 succeeds
        mock_run.side_effect = [_ok(), _merge_commit(), _ok()]

        executor = CherryPickExecutor("/repo")
        result = executor.execute("8.1", "abc123merge", ["sha1", "sha2"])

        assert result.success is True
        assert result.applied_commits == ["abc123merge"]
        assert result.conflicting_files == []

    @patch("scripts.cherry_pick.subprocess.run")
    def test_calls_checkout_then_cherry_pick(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [_ok(), _merge_commit(), _ok()]

        executor = CherryPickExecutor("/repo")
        executor.execute("8.1", "abc123merge", ["sha1"])

        calls = mock_run.call_args_list
        # First call: git checkout 8.1
        assert calls[0][0][0] == ["git", "checkout", "8.1"]
        # Third call: git cherry-pick -m 1 <merge_sha>
        assert calls[2][0][0] == ["git", "cherry-pick", "-m", "1", "abc123merge"]


class TestCleanCherryPickSequential:
    """Scenario 2: Clean cherry-pick with sequential commits."""

    @patch("scripts.cherry_pick.subprocess.run")
    def test_returns_success_all_commits(self, mock_run: MagicMock) -> None:
        # checkout + 3 cherry-picks all succeed
        mock_run.side_effect = [_ok(), _ok(), _ok(), _ok()]

        executor = CherryPickExecutor("/repo")
        result = executor.execute("7.2", None, ["sha1", "sha2", "sha3"])

        assert result.success is True
        assert result.applied_commits == ["sha1", "sha2", "sha3"]
        assert result.conflicting_files == []

    @patch("scripts.cherry_pick.subprocess.run")
    def test_calls_cherry_pick_per_commit(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [_ok(), _ok(), _ok()]

        executor = CherryPickExecutor("/repo")
        executor.execute("8.1", None, ["sha1", "sha2"])

        calls = mock_run.call_args_list
        assert calls[0][0][0] == ["git", "checkout", "8.1"]
        assert calls[1][0][0] == ["git", "cherry-pick", "sha1"]
        assert calls[2][0][0] == ["git", "cherry-pick", "sha2"]


class TestConflictDetection:
    """Scenario 3: Cherry-pick with conflicts — conflict detection and file parsing."""

    @patch("builtins.open", mock_open(read_data="<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> abc123\n"))
    @patch("scripts.cherry_pick.subprocess.run")
    def test_merge_commit_conflict_returns_conflicted_files(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [
            _ok(),                                      # checkout
            _merge_commit(),                            # rev-list parents
            _fail(stderr="conflict"),                    # cherry-pick -m 1 fails
            _ok(stdout="src/server.c\nsrc/config.c\n"), # git diff --name-only --diff-filter=U
            _ok(stdout="target content"),                # git show 8.1:src/server.c
            _ok(stdout="source content"),                # git show CHERRY_PICK_HEAD:src/server.c
            _ok(stdout="target content 2"),              # git show 8.1:src/config.c
            _ok(stdout="source content 2"),              # git show CHERRY_PICK_HEAD:src/config.c
        ]

        executor = CherryPickExecutor("/repo")
        result = executor.execute("8.1", "mergesha", ["sha1"])

        assert result.success is False
        assert len(result.conflicting_files) == 2
        assert result.conflicting_files[0].path == "src/server.c"
        assert result.conflicting_files[1].path == "src/config.c"
        assert result.applied_commits == ["mergesha"]

    @patch("builtins.open", mock_open(read_data="conflict content"))
    @patch("scripts.cherry_pick.subprocess.run")
    def test_sequential_conflict_stops_at_failing_commit(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [
            _ok(),                              # checkout
            _ok(),                              # cherry-pick sha1 succeeds
            _fail(stderr="conflict"),           # cherry-pick sha2 fails
            _ok(stdout="file.c\n"),             # git diff --name-only --diff-filter=U
            _ok(stdout="target ver"),           # git show 8.1:file.c
            _ok(stdout="source ver"),           # git show CHERRY_PICK_HEAD:file.c
        ]

        executor = CherryPickExecutor("/repo")
        result = executor.execute("8.1", None, ["sha1", "sha2", "sha3"])

        assert result.success is False
        assert result.applied_commits == ["sha1", "sha2"]
        assert len(result.conflicting_files) == 1
        assert result.conflicting_files[0].path == "file.c"

    @patch("builtins.open", mock_open(read_data="markers here"))
    @patch("scripts.cherry_pick.subprocess.run")
    def test_conflicted_file_reads_all_versions(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [
            _ok(),                                  # checkout
            _merge_commit(),                        # rev-list parents
            _fail(),                                # cherry-pick fails
            _ok(stdout="src/main.c\n"),             # git diff
            _ok(stdout="target branch content"),    # git show 8.1:src/main.c
            _ok(stdout="source branch content"),    # git show CHERRY_PICK_HEAD:src/main.c
        ]

        executor = CherryPickExecutor("/repo")
        result = executor.execute("8.1", "mergesha", [])

        cf = result.conflicting_files[0]
        assert cf.path == "src/main.c"
        assert cf.content_with_markers == "markers here"
        assert cf.target_branch_content == "target branch content"
        assert cf.source_branch_content == "source branch content"

    @patch("builtins.open", mock_open(read_data="content"))
    @patch("scripts.cherry_pick.subprocess.run")
    def test_git_show_failure_returns_empty_string(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [
            _ok(),                          # checkout
            _merge_commit(),                # rev-list parents
            _fail(),                        # cherry-pick fails
            _ok(stdout="new_file.c\n"),     # git diff
            _fail(stderr="not found"),      # git show target branch fails
            _fail(stderr="not found"),      # git show CHERRY_PICK_HEAD fails
        ]

        executor = CherryPickExecutor("/repo")
        result = executor.execute("8.1", "mergesha", [])

        cf = result.conflicting_files[0]
        assert cf.target_branch_content == ""
        assert cf.source_branch_content == ""


class TestMergeCommitPreference:
    """Scenario 4 & 5: Merge commit SHA is preferred; sequential fallback."""

    @patch("scripts.cherry_pick.subprocess.run")
    def test_uses_m1_flag_when_merge_sha_provided(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [_ok(), _merge_commit(), _ok()]

        executor = CherryPickExecutor("/repo")
        executor.execute("8.1", "merge_sha_abc", ["sha1", "sha2"])

        cherry_pick_call = mock_run.call_args_list[2]
        cmd = cherry_pick_call[0][0]
        assert cmd == ["git", "cherry-pick", "-m", "1", "merge_sha_abc"]

    @patch("scripts.cherry_pick.subprocess.run")
    def test_ignores_individual_commits_when_merge_sha_provided(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [_ok(), _merge_commit(), _ok()]

        executor = CherryPickExecutor("/repo")
        result = executor.execute("8.1", "merge_sha", ["sha1", "sha2", "sha3"])

        # checkout + parent inspection + single cherry-pick
        assert mock_run.call_count == 3
        assert result.applied_commits == ["merge_sha"]

    @patch("scripts.cherry_pick.subprocess.run")
    def test_cherry_picks_squash_merge_commit_without_m1(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [_ok(), _single_parent_commit(), _ok()]

        executor = CherryPickExecutor("/repo")
        result = executor.execute("8.1", "squash_sha", ["sha1", "sha2"])

        assert mock_run.call_args_list[2][0][0] == ["git", "cherry-pick", "squash_sha"]
        assert result.applied_commits == ["squash_sha"]

    @patch("scripts.cherry_pick.subprocess.run")
    def test_rebase_merge_cherry_picks_each_pr_commit(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [_ok(), _single_parent_commit(), _ok(), _ok()]

        executor = CherryPickExecutor("/repo")
        result = executor.execute("8.1", "sha2", ["sha1", "sha2"])

        assert mock_run.call_args_list[2][0][0] == ["git", "cherry-pick", "sha1"]
        assert mock_run.call_args_list[3][0][0] == ["git", "cherry-pick", "sha2"]
        assert result.applied_commits == ["sha1", "sha2"]

    @patch("scripts.cherry_pick.subprocess.run")
    def test_falls_back_to_sequential_when_no_merge_sha(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [_ok(), _ok(), _ok()]

        executor = CherryPickExecutor("/repo")
        result = executor.execute("8.1", None, ["sha1", "sha2"])

        # checkout + 2 individual cherry-picks
        assert mock_run.call_count == 3
        calls = mock_run.call_args_list
        assert calls[1][0][0] == ["git", "cherry-pick", "sha1"]
        assert calls[2][0][0] == ["git", "cherry-pick", "sha2"]
        assert result.applied_commits == ["sha1", "sha2"]

    @patch("scripts.cherry_pick.subprocess.run")
    def test_empty_merge_sha_string_treated_as_none(
        self, mock_run: MagicMock,
    ) -> None:
        """An empty string for merge_commit_sha is falsy, so sequential path is used."""
        mock_run.side_effect = [_ok(), _ok()]

        executor = CherryPickExecutor("/repo")
        result = executor.execute("8.1", "", ["sha1"])

        calls = mock_run.call_args_list
        # Should use sequential path (no -m 1)
        assert calls[1][0][0] == ["git", "cherry-pick", "sha1"]
        assert result.applied_commits == ["sha1"]


class TestSubprocessCwd:
    """Verify that all git commands use the configured repo_dir as cwd."""

    @patch("scripts.cherry_pick.subprocess.run")
    def test_all_calls_use_repo_dir(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [_ok(), _merge_commit(), _ok()]

        executor = CherryPickExecutor("/my/repo/path")
        executor.execute("8.1", "sha", [])

        for c in mock_run.call_args_list:
            assert c[1]["cwd"] == "/my/repo/path"

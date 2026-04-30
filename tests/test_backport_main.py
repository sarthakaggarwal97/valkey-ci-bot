"""Tests for backport_main.py — property tests and unit tests.

Covers:
- Property 11: Summary contains all required metrics (Task 8.2)
- Property 9: Rate limiter enforces daily PR limit (Task 8.3)
- Unit tests for run_backport pipeline flow (Task 8.4)
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.backport_main import build_summary, run_backport
from scripts.backport_models import (
    BackportConfig,
    BackportPRContext,
    BackportResult,
    CherryPickResult,
    ConflictedFile,
    ResolutionResult,
)
from scripts.config import BotConfig
from scripts.rate_limiter import RateLimiter


@pytest.fixture(autouse=True)
def _mock_event_ledger():
    with patch("scripts.backport_main.EventLedger") as mock_cls:
        yield mock_cls.return_value


# ======================================================================
# Feature: backport-agent, Property 11: Summary contains all required metrics
# **Validates: Requirements 9.2, 9.4**
# ======================================================================


class TestBuildSummaryProperty:
    """Property 11: For any BackportResult, the generated summary string
    should contain: the number of commits cherry-picked, the number of
    conflicting files, the number of files resolved by the LLM, the number
    of files left unresolved, and the total Bedrock token usage."""

    @given(
        commits=st.integers(min_value=0, max_value=10_000),
        conflicted=st.integers(min_value=0, max_value=10_000),
        resolved=st.integers(min_value=0, max_value=10_000),
        unresolved=st.integers(min_value=0, max_value=10_000),
        tokens=st.integers(min_value=0, max_value=10_000_000),
        outcome=st.sampled_from([
            "success", "conflicts-unresolved", "duplicate",
            "rate-limited", "branch-missing", "pr-not-merged", "error",
        ]),
    )
    @settings(max_examples=100, deadline=None)
    def test_summary_contains_all_metrics(
        self,
        commits: int,
        conflicted: int,
        resolved: int,
        unresolved: int,
        tokens: int,
        outcome: str,
    ) -> None:
        result = BackportResult(
            outcome=outcome,
            commits_cherry_picked=commits,
            files_conflicted=conflicted,
            files_resolved=resolved,
            files_unresolved=unresolved,
            total_tokens_used=tokens,
        )
        summary = build_summary(result)

        assert str(commits) in summary, f"commits {commits} not in summary"
        assert str(conflicted) in summary, f"conflicted {conflicted} not in summary"
        assert str(resolved) in summary, f"resolved {resolved} not in summary"
        assert str(unresolved) in summary, f"unresolved {unresolved} not in summary"
        assert str(tokens) in summary, f"tokens {tokens} not in summary"



# ======================================================================
# Feature: backport-agent, Property 9: Rate limiter enforces daily PR limit
# **Validates: Requirements 8.1**
# ======================================================================


class TestRateLimiterProperty:
    """Property 9: For any sequence of record_pr_created calls within a
    24-hour window, once the count reaches the configured max_prs_per_day,
    can_create_pr should return False."""

    @given(
        max_prs=st.integers(min_value=1, max_value=50),
        extra_calls=st.integers(min_value=0, max_value=20),
    )
    @settings(max_examples=100, deadline=None)
    def test_rate_limiter_blocks_after_limit(
        self,
        max_prs: int,
        extra_calls: int,
    ) -> None:
        config = BotConfig(max_prs_per_day=max_prs)
        limiter = RateLimiter(config, None, "")

        # Record exactly max_prs creations
        for _ in range(max_prs):
            limiter.record_pr_created()

        # After reaching the limit, can_create_pr must be False
        assert limiter.can_create_pr() is False

        # Any additional recordings should keep it blocked
        for _ in range(extra_calls):
            limiter.record_pr_created()
            assert limiter.can_create_pr() is False

    @given(
        max_prs=st.integers(min_value=2, max_value=50),
    )
    @settings(max_examples=100, deadline=None)
    def test_rate_limiter_allows_below_limit(
        self,
        max_prs: int,
    ) -> None:
        config = BotConfig(max_prs_per_day=max_prs)
        limiter = RateLimiter(config, None, "")

        # Record fewer than max_prs creations
        for i in range(max_prs - 1):
            limiter.record_pr_created()
            assert limiter.can_create_pr() is True, (
                f"Should allow PR creation at count {i + 1}/{max_prs}"
            )

    @given(
        max_prs=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=100, deadline=None)
    def test_daily_count_matches_recordings(
        self,
        max_prs: int,
    ) -> None:
        config = BotConfig(max_prs_per_day=max_prs)
        limiter = RateLimiter(config, None, "")

        for i in range(max_prs):
            assert limiter.get_daily_pr_count() == i
            limiter.record_pr_created()

        assert limiter.get_daily_pr_count() == max_prs



# ======================================================================
# Unit tests for run_backport pipeline flow (Task 8.4)
# **Validates: Requirements 1.2, 1.3, 1.5, 6.1, 8.1, 9.3**
# ======================================================================


def _default_config() -> BackportConfig:
    return BackportConfig()


def _make_mock_pr(
    title: str = "Fix bug",
    body: str = "Fixes a bug",
    html_url: str = "https://github.com/valkey-io/valkey/pull/100",
    merge_commit_sha: str = "merge_sha_abc",
    merged: bool = True,
    commits: list | None = None,
) -> MagicMock:
    """Create a mock source PR object."""
    pr = MagicMock()
    pr.title = title
    pr.body = body
    pr.html_url = html_url
    pr.merge_commit_sha = merge_commit_sha
    pr.merged = merged

    if commits is None:
        commit1 = MagicMock()
        commit1.sha = "commit_sha_1"
        commits = [commit1]

    pr.get_commits.return_value = commits
    return pr


# Shared patch targets
_PATCH_PREFIX = "scripts.backport_main"


class TestRunBackportCleanCherryPick:
    """Test clean cherry-pick flow — no conflicts, PR created successfully."""

    @patch(f"{_PATCH_PREFIX}.boto3")
    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}._run_git")
    @patch(f"{_PATCH_PREFIX}.RateLimiter")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.ConflictResolver")
    @patch(f"{_PATCH_PREFIX}.CherryPickExecutor")
    @patch(f"{_PATCH_PREFIX}.load_backport_config_from_repo")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_clean_cherry_pick_returns_success(
        self,
        mock_gh_cls: MagicMock,
        mock_load_config: MagicMock,
        mock_executor_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_rate_limiter_cls: MagicMock,
        mock_run_git: MagicMock,
        mock_clone: MagicMock,
        mock_boto3: MagicMock,
        _mock_event_ledger: MagicMock,
    ) -> None:
        # Setup GitHub mock
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        # Branch exists
        mock_repo.get_branch.return_value = MagicMock()

        # Source PR
        source_pr = _make_mock_pr()
        mock_repo.get_pull.return_value = source_pr

        # Merge commit message
        mock_git_commit = MagicMock()
        mock_git_commit.raw_data = {"message": "merge commit msg"}
        mock_repo.get_git_commit.return_value = mock_git_commit

        # No duplicate
        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None
        mock_pr_creator.create_backport_pr.return_value = "https://github.com/valkey-io/valkey/pull/200"

        # Rate limiter allows
        mock_rate_limiter = MagicMock()
        mock_rate_limiter_cls.return_value = mock_rate_limiter
        mock_rate_limiter.can_create_pr.return_value = True

        # Clean cherry-pick
        mock_executor = MagicMock()
        mock_executor_cls.return_value = mock_executor
        mock_executor.execute.return_value = CherryPickResult(
            success=True,
            conflicting_files=[],
            applied_commits=["commit_sha_1"],
        )

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            aws_region="us-east-1",
        )

        assert result.outcome == "success"
        assert result.backport_pr_url == "https://github.com/valkey-io/valkey/pull/200"
        assert result.commits_cherry_picked == 1
        assert result.files_conflicted == 0
        assert result.files_resolved == 0
        assert result.files_unresolved == 0
        mock_pr_creator.create_backport_pr.assert_called_once()
        mock_pr_creator_cls.assert_called_once_with(
            mock_gh,
            "valkey-io/valkey",
            backport_label="backport",
            llm_conflict_label="llm-resolved-conflicts",
        )
        mock_rate_limiter.record_pr_created.assert_called_once()
        _mock_event_ledger.record.assert_any_call(
            "backport.pr_created",
            "valkey-io/valkey#100->8.1",
            backport_pr_url="https://github.com/valkey-io/valkey/pull/200",
            outcome="success",
            commits_cherry_picked=1,
            files_conflicted=0,
            files_resolved=0,
            files_unresolved=0,
            total_tokens_used=0,
        )
        _mock_event_ledger.save.assert_called_once()


class TestRunBackportConflictedCherryPick:
    """Test conflicted cherry-pick flow with LLM resolution."""

    @patch(f"{_PATCH_PREFIX}.boto3")
    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}._run_git")
    @patch(f"{_PATCH_PREFIX}._apply_resolutions")
    @patch(f"{_PATCH_PREFIX}.RateLimiter")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.ConflictResolver")
    @patch(f"{_PATCH_PREFIX}.BedrockClient")
    @patch(f"{_PATCH_PREFIX}.CherryPickExecutor")
    @patch(f"{_PATCH_PREFIX}.load_backport_config_from_repo")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_conflicted_cherry_pick_with_resolution(
        self,
        mock_gh_cls: MagicMock,
        mock_load_config: MagicMock,
        mock_executor_cls: MagicMock,
        mock_bedrock_client_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_rate_limiter_cls: MagicMock,
        mock_apply_resolutions: MagicMock,
        mock_run_git: MagicMock,
        mock_clone: MagicMock,
        mock_boto3: MagicMock,
        _mock_event_ledger: MagicMock,
    ) -> None:
        # Setup GitHub mock
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()

        source_pr = _make_mock_pr()
        mock_repo.get_pull.return_value = source_pr
        mock_git_commit = MagicMock()
        mock_git_commit.raw_data = {"message": ""}
        mock_repo.get_git_commit.return_value = mock_git_commit

        # No duplicate
        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None
        mock_pr_creator.create_backport_pr.return_value = "https://github.com/valkey-io/valkey/pull/201"

        # Rate limiter allows
        mock_rate_limiter = MagicMock()
        mock_rate_limiter_cls.return_value = mock_rate_limiter
        mock_rate_limiter.can_create_pr.return_value = True

        # Cherry-pick with conflicts
        conflicted_file = ConflictedFile(
            path="src/server.c",
            content_with_markers="<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>>",
            target_branch_content="old",
            source_branch_content="new",
        )
        mock_executor = MagicMock()
        mock_executor_cls.return_value = mock_executor
        mock_executor.execute.return_value = CherryPickResult(
            success=False,
            conflicting_files=[conflicted_file],
            applied_commits=["merge_sha_abc"],
        )

        # Resolver resolves the file
        mock_resolver = MagicMock()
        mock_resolver_cls.return_value = mock_resolver
        mock_resolver.resolve_conflicts.return_value = [
            ResolutionResult(
                path="src/server.c",
                resolved_content="resolved content",
                resolution_summary="Applied fix",
                tokens_used=500,
                attempts=1,
            ),
        ]

        mock_boto3.client.return_value = MagicMock()

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            aws_region="us-east-1",
        )

        assert result.outcome == "success"
        assert result.files_conflicted == 1
        assert result.files_resolved == 1
        assert result.files_unresolved == 0
        assert result.total_tokens_used == 500
        mock_resolver.resolve_conflicts.assert_called_once()
        _, bedrock_kwargs = mock_bedrock_client_cls.call_args
        assert bedrock_kwargs["rate_limiter"] is mock_rate_limiter
        _mock_event_ledger.record.assert_any_call(
            "backport.conflicts_detected",
            "valkey-io/valkey#100->8.1",
            conflicting_files=1,
        )


class TestRunBackportDuplicateDetection:
    """Test duplicate detection skip."""

    @patch(f"{_PATCH_PREFIX}.boto3")
    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}.RateLimiter")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.ConflictResolver")
    @patch(f"{_PATCH_PREFIX}.CherryPickExecutor")
    @patch(f"{_PATCH_PREFIX}.load_backport_config_from_repo")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_duplicate_pr_skips_processing(
        self,
        mock_gh_cls: MagicMock,
        mock_load_config: MagicMock,
        mock_executor_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_rate_limiter_cls: MagicMock,
        mock_clone: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()

        # Duplicate exists
        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = "https://github.com/valkey-io/valkey/pull/99"

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            aws_region="us-east-1",
        )

        assert result.outcome == "duplicate"
        assert result.backport_pr_url == "https://github.com/valkey-io/valkey/pull/99"
        # Cherry-pick should NOT have been called
        mock_executor_cls.assert_not_called()


class TestRunBackportRateLimitSkip:
    """Test rate limit skip."""

    @patch(f"{_PATCH_PREFIX}.boto3")
    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}.RateLimiter")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.ConflictResolver")
    @patch(f"{_PATCH_PREFIX}.CherryPickExecutor")
    @patch(f"{_PATCH_PREFIX}.load_backport_config_from_repo")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_rate_limited_skips_processing(
        self,
        mock_gh_cls: MagicMock,
        mock_load_config: MagicMock,
        mock_executor_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_rate_limiter_cls: MagicMock,
        mock_clone: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()

        # No duplicate
        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None

        # Rate limit exceeded
        mock_rate_limiter = MagicMock()
        mock_rate_limiter_cls.return_value = mock_rate_limiter
        mock_rate_limiter.reserve_pr_creation.return_value = False

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            aws_region="us-east-1",
        )

        assert result.outcome == "rate-limited"
        # Cherry-pick should NOT have been called
        mock_executor_cls.assert_not_called()


class TestRunBackportMergedPrValidation:
    """Test unmerged source PR skip."""

    @patch(f"{_PATCH_PREFIX}.boto3")
    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}.RateLimiter")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.ConflictResolver")
    @patch(f"{_PATCH_PREFIX}.CherryPickExecutor")
    @patch(f"{_PATCH_PREFIX}.load_backport_config_from_repo")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_unmerged_pr_skips_processing(
        self,
        mock_gh_cls: MagicMock,
        mock_load_config: MagicMock,
        mock_executor_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_rate_limiter_cls: MagicMock,
        mock_clone: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()
        mock_repo.get_pull.return_value = _make_mock_pr(merged=False)

        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None

        mock_rate_limiter = MagicMock()
        mock_rate_limiter_cls.return_value = mock_rate_limiter
        mock_rate_limiter.can_create_pr.return_value = True

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            aws_region="us-east-1",
        )

        assert result.outcome == "pr-not-merged"
        assert "not merged" in (result.error_message or "")
        mock_executor_cls.assert_not_called()
        mock_clone.assert_not_called()



class TestRunBackportMissingBranch:
    """Test missing branch skip."""

    @patch(f"{_PATCH_PREFIX}.boto3")
    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}.RateLimiter")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.ConflictResolver")
    @patch(f"{_PATCH_PREFIX}.CherryPickExecutor")
    @patch(f"{_PATCH_PREFIX}.load_backport_config_from_repo")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_missing_branch_skips_processing(
        self,
        mock_gh_cls: MagicMock,
        mock_load_config: MagicMock,
        mock_executor_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_rate_limiter_cls: MagicMock,
        mock_clone: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        from github.GithubException import GithubException

        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        # Branch does not exist — 404
        mock_repo.get_branch.side_effect = GithubException(
            status=404, data={"message": "Branch not found"}, headers={},
        )

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="nonexistent",
            config=_default_config(),
            github_token="fake-token",
            aws_region="us-east-1",
        )

        assert result.outcome == "branch-missing"
        assert "nonexistent" in (result.error_message or "")
        mock_executor_cls.assert_not_called()


class TestRunBackportGitHubAPIError:
    """Test GitHub API error handling."""

    @patch(f"{_PATCH_PREFIX}.boto3")
    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}._run_git")
    @patch(f"{_PATCH_PREFIX}.RateLimiter")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.ConflictResolver")
    @patch(f"{_PATCH_PREFIX}.CherryPickExecutor")
    @patch(f"{_PATCH_PREFIX}.load_backport_config_from_repo")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_pr_creation_failure_returns_error(
        self,
        mock_gh_cls: MagicMock,
        mock_load_config: MagicMock,
        mock_executor_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_rate_limiter_cls: MagicMock,
        mock_run_git: MagicMock,
        mock_clone: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()

        source_pr = _make_mock_pr()
        mock_repo.get_pull.return_value = source_pr
        mock_git_commit = MagicMock()
        mock_git_commit.raw_data = {"message": ""}
        mock_repo.get_git_commit.return_value = mock_git_commit

        # No duplicate
        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None
        mock_pr_creator.create_backport_pr.side_effect = Exception("GitHub API error")

        # Rate limiter allows
        mock_rate_limiter = MagicMock()
        mock_rate_limiter_cls.return_value = mock_rate_limiter
        mock_rate_limiter.can_create_pr.return_value = True

        # Clean cherry-pick
        mock_executor = MagicMock()
        mock_executor_cls.return_value = mock_executor
        mock_executor.execute.return_value = CherryPickResult(
            success=True,
            conflicting_files=[],
            applied_commits=["sha1"],
        )

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            aws_region="us-east-1",
        )

        assert result.outcome == "error"
        assert "GitHub API error" in (result.error_message or "")


def test_run_backport_requires_commit_identity_when_dco_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI_BOT_REQUIRE_DCO_SIGNOFF", "true")
    monkeypatch.delenv("CI_BOT_COMMIT_NAME", raising=False)
    monkeypatch.delenv("CI_BOT_COMMIT_EMAIL", raising=False)

    result = run_backport(
        repo_full_name="valkey-io/valkey",
        source_pr_number=100,
        target_branch="8.1",
        config=_default_config(),
        github_token="fake-token",
        aws_region="us-east-1",
    )

    assert result.outcome == "error"
    assert "CI_BOT_COMMIT_NAME" in (result.error_message or "")

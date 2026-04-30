"""Unit tests for the RateLimiter class.

Tests daily PR limit tracking, open bot PR limit, daily token budget,
failure queuing, and serialization round-trip.

**Validates: Requirements 10.1, 10.4, 10.5**
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from github.GithubException import GithubException

from scripts.config import BotConfig
from scripts.rate_limiter import RateLimiter

# --- Fixtures ---

@pytest.fixture
def config() -> BotConfig:
    return BotConfig(
        max_prs_per_day=3,
        max_open_bot_prs=2,
        daily_token_budget=10_000,
    )


@pytest.fixture
def limiter(config: BotConfig) -> RateLimiter:
    return RateLimiter(config)


# --- Daily PR limit tests ---


class TestDailyPRLimit:
    def test_can_create_pr_when_under_limit(self, limiter: RateLimiter) -> None:
        """No PRs created yet — should allow creation."""
        assert limiter.can_create_pr() is True

    def test_cannot_create_pr_when_at_limit(self, limiter: RateLimiter) -> None:
        """After max_prs_per_day PRs, should deny creation."""
        for _ in range(3):
            limiter.record_pr_created()
        assert limiter.can_create_pr() is False

    def test_can_create_pr_after_window_expires(self, config: BotConfig) -> None:
        """Old timestamps outside 24h window should be pruned."""
        limiter = RateLimiter(config)
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        limiter._pr_timestamps = [old_time] * 3
        assert limiter.can_create_pr() is True
        assert limiter.get_daily_pr_count() == 0

    def test_record_pr_created_increments_count(self, limiter: RateLimiter) -> None:
        assert limiter.get_daily_pr_count() == 0
        limiter.record_pr_created()
        assert limiter.get_daily_pr_count() == 1
        limiter.record_pr_created()
        assert limiter.get_daily_pr_count() == 2

    def test_mixed_old_and_new_timestamps(self, config: BotConfig) -> None:
        """Only recent timestamps count toward the limit."""
        limiter = RateLimiter(config)
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()
        limiter._pr_timestamps = [old, old, recent]
        assert limiter.get_daily_pr_count() == 1
        assert limiter.can_create_pr() is True


# --- Open bot PR limit tests ---


class TestOpenBotPRLimit:
    def test_no_github_client_allows_creation(self, limiter: RateLimiter) -> None:
        """Without a GitHub client, open PR check is skipped."""
        assert limiter.can_create_pr() is True

    def test_exceeds_open_pr_limit(self, config: BotConfig) -> None:
        """When open bot PRs >= max_open_bot_prs, deny creation."""
        gh = MagicMock()
        repo = MagicMock()
        gh.get_repo.return_value = repo

        # Simulate 2 open PRs with bot-fix label
        label = MagicMock()
        label.name = "bot-fix"
        pr1 = MagicMock()
        pr1.labels = [label]
        pr2 = MagicMock()
        pr2.labels = [label]
        repo.get_pulls.return_value = [pr1, pr2]

        limiter = RateLimiter(config, github_client=gh, repo_full_name="owner/repo")
        assert limiter.can_create_pr() is False

    def test_under_open_pr_limit(self, config: BotConfig) -> None:
        """When open bot PRs < max_open_bot_prs, allow creation."""
        gh = MagicMock()
        repo = MagicMock()
        gh.get_repo.return_value = repo

        label = MagicMock()
        label.name = "bot-fix"
        pr1 = MagicMock()
        pr1.labels = [label]
        repo.get_pulls.return_value = [pr1]

        limiter = RateLimiter(config, github_client=gh, repo_full_name="owner/repo")
        assert limiter.can_create_pr() is True

    def test_github_api_error_allows_creation(self, config: BotConfig) -> None:
        """If GitHub API fails, we don't block PR creation."""
        gh = MagicMock()
        gh.get_repo.side_effect = Exception("API error")

        limiter = RateLimiter(config, github_client=gh, repo_full_name="owner/repo")
        assert limiter.can_create_pr() is True


# --- Token budget tests ---


class TestTokenBudget:
    def test_can_use_tokens_under_budget(self, limiter: RateLimiter) -> None:
        assert limiter.can_use_tokens(5000) is True

    def test_cannot_use_tokens_over_budget(self, limiter: RateLimiter) -> None:
        limiter.record_token_usage(9000)
        assert limiter.can_use_tokens(2000) is False

    def test_can_use_tokens_exactly_at_budget(self, limiter: RateLimiter) -> None:
        limiter.record_token_usage(9000)
        assert limiter.can_use_tokens(1000) is True

    def test_token_usage_accumulates(self, limiter: RateLimiter) -> None:
        limiter.record_token_usage(3000)
        limiter.record_token_usage(4000)
        assert limiter.get_token_usage() == 7000

    def test_token_window_resets_after_24h(self, config: BotConfig) -> None:
        """Token usage resets when the 24h window expires."""
        limiter = RateLimiter(config)
        limiter.record_token_usage(9999)
        # Move the window start to 25 hours ago
        limiter._token_window_start = (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).isoformat()
        assert limiter.can_use_tokens(5000) is True
        assert limiter.get_token_usage() == 0

    def test_zero_amount_check(self, limiter: RateLimiter) -> None:
        """Checking with 0 tokens should always succeed under budget."""
        limiter.record_token_usage(10_000)
        # At exactly the budget, 0 more is fine
        assert limiter.can_use_tokens(0) is True

    def test_budget_exhausted_blocks_zero_check(self, limiter: RateLimiter) -> None:
        """When over budget, even 0 additional tokens should be blocked."""
        limiter.record_token_usage(10_001)
        assert limiter.can_use_tokens(0) is False


# --- Failure queue tests ---


class TestFailureQueue:
    def test_queue_and_retrieve(self, limiter: RateLimiter) -> None:
        limiter.queue_failure("fp-abc123")
        assert limiter.get_queued_failures() == ["fp-abc123"]

    def test_queue_dedup(self, limiter: RateLimiter) -> None:
        """Queuing the same fingerprint twice should not duplicate."""
        limiter.queue_failure("fp-abc123")
        limiter.queue_failure("fp-abc123")
        assert limiter.get_queued_failures() == ["fp-abc123"]

    def test_dequeue_failure(self, limiter: RateLimiter) -> None:
        limiter.queue_failure("fp-abc123")
        limiter.queue_failure("fp-def456")
        limiter.dequeue_failure("fp-abc123")
        assert limiter.get_queued_failures() == ["fp-def456"]

    def test_dequeue_nonexistent_is_noop(self, limiter: RateLimiter) -> None:
        limiter.dequeue_failure("fp-nonexistent")
        assert limiter.get_queued_failures() == []

    def test_get_queued_returns_copy(self, limiter: RateLimiter) -> None:
        """Modifying the returned list should not affect internal state."""
        limiter.queue_failure("fp-abc123")
        queued = limiter.get_queued_failures()
        queued.clear()
        assert limiter.get_queued_failures() == ["fp-abc123"]


# --- Serialization round-trip tests ---


class TestSerialization:
    def test_round_trip(self, config: BotConfig) -> None:
        """Serialize and deserialize should preserve all state."""
        limiter = RateLimiter(config)
        limiter.record_pr_created()
        limiter.record_pr_created()
        limiter.record_token_usage(5000)
        limiter.record_ai_metric("bedrock.invoke_schema.calls")
        limiter.record_ai_metric("bedrock.invoke_schema.success", 2)
        limiter.queue_failure("fp-abc123")
        limiter.queue_failure("fp-def456")

        data = limiter.to_dict()

        limiter2 = RateLimiter(config)
        limiter2.from_dict(data)

        assert limiter2.get_daily_pr_count() == 2
        assert limiter2.get_token_usage() == 5000
        assert limiter2.get_ai_metrics() == {
            "bedrock.invoke_schema.calls": 1,
            "bedrock.invoke_schema.success": 2,
        }
        assert limiter2.get_queued_failures() == ["fp-abc123", "fp-def456"]

    def test_from_dict_with_empty_data(self, config: BotConfig) -> None:
        """Loading from empty dict should use defaults."""
        limiter = RateLimiter(config)
        limiter.from_dict({})
        assert limiter.get_daily_pr_count() == 0
        assert limiter.get_token_usage() == 0
        assert limiter.get_ai_metrics() == {}
        assert limiter.get_queued_failures() == []

    def test_to_dict_structure(self, limiter: RateLimiter) -> None:
        """Verify the dict structure has expected keys."""
        data = limiter.to_dict()
        assert "pr_timestamps" in data
        assert "token_usage" in data
        assert "token_window_start" in data
        assert "queued_failures" in data
        assert "ai_metrics" in data

    def test_save_creates_bot_data_branch_when_missing(self, config: BotConfig) -> None:
        repo = MagicMock()
        repo.default_branch = "main"
        repo.get_git_ref.side_effect = [
            GithubException(404, {"message": "missing bot-data"}),
            MagicMock(object=MagicMock(sha="base-sha")),
        ]
        gh = MagicMock()
        gh.get_repo.return_value = repo

        limiter = RateLimiter(config, github_client=gh, repo_full_name="owner/repo")
        limiter.save()

        repo.create_git_ref.assert_called_once_with(
            ref="refs/heads/bot-data",
            sha="base-sha",
        )

    def test_save_does_not_fallback_to_create_on_non_404_lookup_error(
        self,
        config: BotConfig,
    ) -> None:
        repo = MagicMock()
        repo.default_branch = "main"
        repo.get_git_ref.return_value = MagicMock()
        repo.get_contents.side_effect = GithubException(500, {"message": "boom"})
        gh = MagicMock()
        gh.get_repo.return_value = repo

        limiter = RateLimiter(config, github_client=gh, repo_full_name="owner/repo")
        limiter.save()

        repo.create_file.assert_not_called()

    def test_save_uses_separate_state_repository_when_configured(
        self,
        config: BotConfig,
    ) -> None:
        target_gh = MagicMock()
        state_repo = MagicMock()
        state_repo.default_branch = "main"
        state_repo.get_git_ref.side_effect = [
            GithubException(404, {"message": "missing bot-data"}),
            MagicMock(object=MagicMock(sha="base-sha")),
        ]
        state_gh = MagicMock()
        state_gh.get_repo.return_value = state_repo

        limiter = RateLimiter(
            config,
            github_client=target_gh,
            repo_full_name="valkey-io/valkey",
            state_github_client=state_gh,
            state_repo_full_name="owner/valkey-ci-agent",
        )
        limiter.save()

        state_gh.get_repo.assert_called_once_with("owner/valkey-ci-agent")
        target_gh.get_repo.assert_not_called()

    def test_load_raises_on_non_missing_remote_error(self, config: BotConfig) -> None:
        repo = MagicMock()
        repo.get_contents.side_effect = GithubException(500, {"message": "boom"})
        gh = MagicMock()
        gh.get_repo.return_value = repo

        limiter = RateLimiter(config, github_client=gh, repo_full_name="owner/repo")

        with pytest.raises(RuntimeError, match="failed to load rate limiter state"):
            limiter.load()

    def test_save_retries_on_write_conflict_and_merges_remote_updates(
        self,
        config: BotConfig,
    ) -> None:
        repo = MagicMock()
        repo.default_branch = "main"
        repo.get_git_ref.return_value = MagicMock()

        now = datetime.now(timezone.utc)
        window_start = datetime.now(timezone.utc).isoformat()
        initial_payload = {
            "pr_timestamps": [(now - timedelta(minutes=2)).isoformat()],
            "token_usage": 5,
            "token_window_start": window_start,
            "queued_failures": ["fp-a"],
            "ai_metrics": {"bedrock.invoke.calls": 3},
        }
        concurrent_payload = {
            "pr_timestamps": [
                (now - timedelta(minutes=2)).isoformat(),
                (now - timedelta(minutes=1)).isoformat(),
            ],
            "token_usage": 9,
            "token_window_start": window_start,
            "queued_failures": ["fp-a", "fp-c"],
            "ai_metrics": {
                "bedrock.invoke.calls": 5,
                "bedrock.invoke_schema.calls": 1,
            },
        }
        initial_contents = MagicMock(
            decoded_content=json.dumps(initial_payload).encode(),
            sha="sha-1",
        )
        concurrent_contents = MagicMock(
            decoded_content=json.dumps(concurrent_payload).encode(),
            sha="sha-2",
        )
        repo.get_contents.side_effect = [
            initial_contents,
            initial_contents,
            concurrent_contents,
        ]
        repo.update_file.side_effect = [
            GithubException(409, {"message": "sha conflict"}),
            None,
        ]
        gh = MagicMock()
        gh.get_repo.return_value = repo

        limiter = RateLimiter(config, github_client=gh, repo_full_name="owner/repo")
        limiter.load()
        limiter.record_pr_created()
        local_timestamp = limiter.get_daily_pr_count()
        limiter.record_token_usage(7)
        limiter.record_ai_metric("bedrock.invoke.calls", 1)
        limiter.record_ai_metric("bedrock.retries", 2)
        limiter.queue_failure("fp-b")
        limiter.save()

        assert local_timestamp == 2
        assert repo.update_file.call_count == 2
        merged_payload = json.loads(repo.update_file.call_args_list[-1].args[2])
        assert len(merged_payload["pr_timestamps"]) == 3
        assert merged_payload["token_usage"] == 16
        assert merged_payload["ai_metrics"] == {
            "bedrock.invoke.calls": 6,
            "bedrock.invoke_schema.calls": 1,
            "bedrock.retries": 2,
        }
        assert set(merged_payload["queued_failures"]) == {"fp-a", "fp-b", "fp-c"}
        assert repo.update_file.call_args_list[-1].args[3] == "sha-2"

    def test_reserve_pr_creation_uses_latest_remote_limit(self) -> None:
        config = BotConfig(max_prs_per_day=1)
        now = datetime.now(timezone.utc)

        def contents(payload: dict, sha: str) -> MagicMock:
            return MagicMock(decoded_content=json.dumps(payload).encode(), sha=sha)

        base_payload = {
            "pr_timestamps": [],
            "token_usage": 0,
            "token_window_start": now.isoformat(),
            "queued_failures": [],
            "ai_metrics": {},
        }
        full_payload = {
            **base_payload,
            "pr_timestamps": [now.isoformat()],
        }

        repo = MagicMock()
        repo.default_branch = "main"
        repo.get_git_ref.return_value = MagicMock()
        repo.get_contents.side_effect = [
            contents(base_payload, "sha-1"),
            contents(full_payload, "sha-2"),
        ]
        gh = MagicMock()
        gh.get_repo.return_value = repo

        first = RateLimiter(config, github_client=gh, repo_full_name="owner/repo")
        second = RateLimiter(config, github_client=gh, repo_full_name="owner/repo")

        assert first.reserve_pr_creation() is True
        assert second.reserve_pr_creation() is False
        assert repo.update_file.call_count == 1

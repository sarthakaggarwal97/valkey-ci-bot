"""Rate limiting and safety limits for the CI Failure Bot.

Tracks daily PR creation count, open bot PR count, and daily token
budget to prevent repository flooding and excessive LLM resource usage.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from github.GithubException import GithubException

if TYPE_CHECKING:
    from github import Github

from scripts.config import BotConfig

logger = logging.getLogger(__name__)

_RATE_STATE_BRANCH = "bot-data"
_RATE_STATE_FILE = "rate-state.json"


class RateLimiter:
    """Enforces daily PR limits, open bot PR caps, and token budgets.

    State is persisted as a JSON file on the bot-data branch so it
    survives across workflow runs.
    """

    def __init__(
        self,
        config: BotConfig,
        github_client: "Github | None" = None,
        repo_full_name: str = "",
        *,
        state_github_client: "Github | None" = None,
        state_repo_full_name: str | None = None,
    ) -> None:
        self._config = config
        self._gh = github_client
        self._repo_name = repo_full_name
        self._state_gh = state_github_client or github_client
        self._state_repo_name = state_repo_full_name or repo_full_name

        # Daily PR tracking
        self._pr_timestamps: list[str] = []  # ISO timestamps of PRs created

        # Token budget tracking
        self._token_usage: int = 0
        self._token_window_start: str = datetime.now(timezone.utc).isoformat()

        # Queued failures (fingerprints waiting for rate limit reset)
        self._queued_failures: list[str] = []

    # --- Daily PR limit ---

    def _prune_old_timestamps(self) -> None:
        """Remove PR timestamps older than 24 hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        self._pr_timestamps = [
            ts for ts in self._pr_timestamps
            if datetime.fromisoformat(ts) > cutoff
        ]

    def get_daily_pr_count(self) -> int:
        """Return the number of PRs created in the last 24 hours."""
        self._prune_old_timestamps()
        return len(self._pr_timestamps)

    def can_create_pr(self) -> bool:
        """Check if a new PR can be created within the daily limit.

        Also checks the open bot PR limit via GitHub API if available.
        """
        self._prune_old_timestamps()
        if len(self._pr_timestamps) >= self._config.max_prs_per_day:
            logger.info(
                "Daily PR limit reached (%d/%d).",
                len(self._pr_timestamps), self._config.max_prs_per_day,
            )
            return False

        if self._exceeds_open_pr_limit():
            return False

        return True

    def _exceeds_open_pr_limit(self) -> bool:
        """Check if the number of open bot PRs meets or exceeds the cap."""
        if not self._gh or not self._repo_name:
            return False

        try:
            repo = self._gh.get_repo(self._repo_name)
            open_prs = repo.get_pulls(state="open", sort="created", direction="desc")
            bot_pr_count = sum(
                1 for pr in open_prs
                if any(label.name == "bot-fix" for label in pr.labels)
            )
            if bot_pr_count >= self._config.max_open_bot_prs:
                logger.info(
                    "Open bot PR limit reached (%d/%d).",
                    bot_pr_count, self._config.max_open_bot_prs,
                )
                return True
        except Exception as exc:
            logger.warning("Failed to check open bot PRs: %s", exc)

        return False

    def record_pr_created(self) -> None:
        """Record that a PR was just created."""
        self._pr_timestamps.append(datetime.now(timezone.utc).isoformat())

    # --- Token budget ---

    def _prune_token_window(self) -> None:
        """Reset token usage if the 24-hour window has elapsed."""
        window_start = datetime.fromisoformat(self._token_window_start)
        if datetime.now(timezone.utc) - window_start > timedelta(hours=24):
            self._token_usage = 0
            self._token_window_start = datetime.now(timezone.utc).isoformat()

    def can_use_tokens(self, amount: int) -> bool:
        """Check if the token budget allows using `amount` more tokens."""
        self._prune_token_window()
        if self._token_usage + amount > self._config.daily_token_budget:
            logger.info(
                "Token budget would be exceeded: %d + %d > %d.",
                self._token_usage, amount, self._config.daily_token_budget,
            )
            return False
        return True

    def record_token_usage(self, amount: int) -> None:
        """Record that `amount` tokens were consumed."""
        self._prune_token_window()
        self._token_usage += amount

    def get_token_usage(self) -> int:
        """Return cumulative token usage in the current 24-hour window."""
        self._prune_token_window()
        return self._token_usage

    # --- Failure queue ---

    def queue_failure(self, fingerprint: str) -> None:
        """Queue a failure fingerprint for processing after rate limits reset."""
        if fingerprint not in self._queued_failures:
            self._queued_failures.append(fingerprint)
            logger.info("Queued failure %s for later processing.", fingerprint[:12])

    def get_queued_failures(self) -> list[str]:
        """Return the list of queued failure fingerprints."""
        return list(self._queued_failures)

    def dequeue_failure(self, fingerprint: str) -> None:
        """Remove a fingerprint from the queue after successful processing."""
        if fingerprint in self._queued_failures:
            self._queued_failures.remove(fingerprint)

    # --- Persistence ---

    def to_dict(self) -> dict:
        """Serialize rate limiter state to a JSON-compatible dict."""
        return {
            "pr_timestamps": self._pr_timestamps,
            "token_usage": self._token_usage,
            "token_window_start": self._token_window_start,
            "queued_failures": self._queued_failures,
        }

    def from_dict(self, data: dict) -> None:
        """Restore rate limiter state from a dict."""
        self._pr_timestamps = data.get("pr_timestamps", [])
        self._token_usage = data.get("token_usage", 0)
        self._token_window_start = data.get(
            "token_window_start", datetime.now(timezone.utc).isoformat()
        )
        self._queued_failures = data.get("queued_failures", [])

    def _ensure_state_branch(self, repo) -> None:
        """Create the data branch from the default branch when missing."""
        try:
            repo.get_git_ref(f"heads/{_RATE_STATE_BRANCH}")
            return
        except GithubException as exc:
            if exc.status != 404:
                raise
        except FileNotFoundError:
            pass

        base_ref = repo.get_git_ref(f"heads/{repo.default_branch}")
        repo.create_git_ref(
            ref=f"refs/heads/{_RATE_STATE_BRANCH}",
            sha=base_ref.object.sha,
        )

    def load(self) -> None:
        """Load rate limiter state from the bot-data branch."""
        if not self._state_gh or not self._state_repo_name:
            logger.info("No GitHub client; starting with fresh rate limiter state.")
            return
        try:
            repo = self._state_gh.get_repo(self._state_repo_name)
            contents = repo.get_contents(_RATE_STATE_FILE, ref=_RATE_STATE_BRANCH)
            if isinstance(contents, list):
                raise ValueError("Rate limiter state path resolved to a directory.")
            data = json.loads(contents.decoded_content.decode())
            self.from_dict(data)
            logger.info("Loaded rate limiter state.")
        except Exception as exc:
            logger.info("Could not load rate limiter state (may not exist yet): %s", exc)

    def save(self) -> None:
        """Save rate limiter state to the bot-data branch."""
        if not self._state_gh or not self._state_repo_name:
            logger.warning("Cannot save rate limiter state: no GitHub client or repo.")
            return
        try:
            repo = self._state_gh.get_repo(self._state_repo_name)
            self._ensure_state_branch(repo)
            content = json.dumps(self.to_dict(), indent=2)
            try:
                existing = repo.get_contents(_RATE_STATE_FILE, ref=_RATE_STATE_BRANCH)
            except GithubException as exc:
                if exc.status != 404:
                    raise
                existing = None
            except FileNotFoundError:
                existing = None

            if isinstance(existing, list):
                raise ValueError("Rate limiter state path resolved to a directory.")
            if existing is None:
                repo.create_file(
                    _RATE_STATE_FILE, "Initialize rate limiter state", content,
                    branch=_RATE_STATE_BRANCH,
                )
            else:
                repo.update_file(
                    _RATE_STATE_FILE, "Update rate limiter state", content,
                    existing.sha, branch=_RATE_STATE_BRANCH,
                )
            logger.info("Saved rate limiter state.")
        except Exception as exc:
            logger.error("Failed to save rate limiter state: %s", exc)

"""Rate limiting and safety limits for the CI Failure Agent.

Tracks daily PR creation count, open agent PR count, and daily token
budget to prevent repository flooding and excessive LLM resource usage.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from github.GithubException import GithubException

if TYPE_CHECKING:
    from github import Github

from scripts.config import BotConfig

logger = logging.getLogger(__name__)

_RATE_STATE_BRANCH = "bot-data"
_RATE_STATE_FILE = "rate-state.json"
_MAX_PERSIST_ATTEMPTS = 3


def _is_write_conflict(exc: Exception) -> bool:
    """Return True when a GitHub write failed due to a stale file SHA."""
    if not isinstance(exc, GithubException):
        return False
    if exc.status in {409, 422}:
        return True
    message = str(exc).lower()
    return "sha" in message or "already exists" in message or "conflict" in message


def _is_missing_state_error(exc: Exception) -> bool:
    """Return True when the remote state branch or file is absent."""
    if isinstance(exc, GithubException):
        return exc.status == 404
    return isinstance(exc, FileNotFoundError)


class RateLimiter:
    """Enforces daily PR limits, open agent PR caps, and token budgets.

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
        self._ai_metrics: dict[str, int] = {}
        self._pending_pr_timestamps: list[str] = []
        self._pending_token_delta: int = 0
        self._pending_token_window_start: str | None = None
        self._pending_queue_additions: set[str] = set()
        self._pending_queue_removals: set[str] = set()
        self._pending_ai_metrics: Counter[str] = Counter()
        self._reserved_pr_timestamps: list[str] = []

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

        Also checks the open agent PR limit via GitHub API if available.
        A limit of 0 means unlimited (no restriction).
        """
        self._prune_old_timestamps()
        if self._config.max_prs_per_day > 0 and len(self._pr_timestamps) >= self._config.max_prs_per_day:
            logger.info(
                "Daily PR limit reached (%d/%d).",
                len(self._pr_timestamps), self._config.max_prs_per_day,
            )
            return False

        if self._exceeds_open_pr_limit():
            return False

        return True

    def _exceeds_open_pr_limit(self) -> bool:
        """Check if the number of open agent PRs meets or exceeds the cap.

        A cap of 0 means unlimited (no restriction).
        """
        if self._config.max_open_bot_prs == 0:
            return False
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
                    "Open agent PR limit reached (%d/%d).",
                    bot_pr_count, self._config.max_open_bot_prs,
                )
                return True
        except Exception as exc:
            logger.warning("Failed to check open agent PRs: %s", exc)

        return False

    def record_pr_created(self) -> None:
        """Record that a PR was just created."""
        if self._reserved_pr_timestamps:
            self._reserved_pr_timestamps.pop(0)
            return
        timestamp = datetime.now(timezone.utc).isoformat()
        self._pr_timestamps.append(timestamp)
        self._pending_pr_timestamps.append(timestamp)

    def reserve_pr_creation(self) -> bool:
        """Atomically reserve one PR slot before creating a PR.

        The reservation is intentionally kept even if later PR creation fails:
        it is safer to under-use the daily allowance than to exceed the cap
        when multiple workflow jobs race.
        """
        if self._exceeds_open_pr_limit():
            return False

        if not self._state_gh or not self._state_repo_name:
            self._prune_old_timestamps()
            if self._config.max_prs_per_day > 0 and len(self._pr_timestamps) >= self._config.max_prs_per_day:
                logger.info(
                    "Daily PR limit reached (%d/%d).",
                    len(self._pr_timestamps), self._config.max_prs_per_day,
                )
                return False
            timestamp = datetime.now(timezone.utc).isoformat()
            self._pr_timestamps.append(timestamp)
            self._reserved_pr_timestamps.append(timestamp)
            return True

        repo = self._state_gh.get_repo(self._state_repo_name)
        self._ensure_state_branch(repo)
        for attempt in range(1, _MAX_PERSIST_ATTEMPTS + 1):
            remote_data, existing = self._read_remote_state(repo)
            merged = self._merge_remote_state(remote_data)
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=24)
            pr_timestamps = [
                ts for ts in list(merged.get("pr_timestamps", []))
                if datetime.fromisoformat(ts) > cutoff
            ]
            if self._config.max_prs_per_day > 0 and len(pr_timestamps) >= self._config.max_prs_per_day:
                logger.info(
                    "Daily PR limit reached (%d/%d).",
                    len(pr_timestamps), self._config.max_prs_per_day,
                )
                self.from_dict({**merged, "pr_timestamps": pr_timestamps})
                return False
            timestamp = now.isoformat()
            pr_timestamps.append(timestamp)
            merged["pr_timestamps"] = pr_timestamps
            content = json.dumps(merged, indent=2)
            try:
                if existing is None:
                    repo.create_file(
                        _RATE_STATE_FILE,
                        "Reserve PR rate limit slot",
                        content,
                        branch=_RATE_STATE_BRANCH,
                    )
                else:
                    repo.update_file(
                        _RATE_STATE_FILE,
                        "Reserve PR rate limit slot",
                        content,
                        getattr(existing, "sha", ""),
                        branch=_RATE_STATE_BRANCH,
                    )
                self.from_dict(merged)
                self._reserved_pr_timestamps.append(timestamp)
                logger.info("Reserved PR rate limit slot.")
                return True
            except Exception as exc:
                if attempt < _MAX_PERSIST_ATTEMPTS and _is_write_conflict(exc):
                    logger.info(
                        "Rate limiter reservation conflict on attempt %d/%d; reloading and retrying.",
                        attempt,
                        _MAX_PERSIST_ATTEMPTS,
                    )
                    continue
                raise RuntimeError(f"failed to reserve PR rate limit slot: {exc}") from exc
        raise RuntimeError("failed to reserve PR rate limit slot")

    # --- Token budget ---

    def _prune_token_window(self) -> None:
        """Reset token usage if the 24-hour window has elapsed."""
        window_start = datetime.fromisoformat(self._token_window_start)
        if datetime.now(timezone.utc) - window_start > timedelta(hours=24):
            self._token_usage = 0
            self._token_window_start = datetime.now(timezone.utc).isoformat()

    def can_use_tokens(self, amount: int) -> bool:
        """Check if the token budget allows using `amount` more tokens.

        A budget of 0 means unlimited (no restriction).
        """
        if self._config.daily_token_budget == 0:
            return True
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
        if self._pending_token_window_start != self._token_window_start:
            self._pending_token_delta = 0
            self._pending_token_window_start = self._token_window_start
        self._pending_token_delta += amount

    def get_token_usage(self) -> int:
        """Return cumulative token usage in the current 24-hour window."""
        self._prune_token_window()
        return self._token_usage

    # --- Failure queue ---

    def queue_failure(self, fingerprint: str) -> None:
        """Queue a failure fingerprint for processing after rate limits reset."""
        if fingerprint not in self._queued_failures:
            self._queued_failures.append(fingerprint)
            self._pending_queue_additions.add(fingerprint)
            self._pending_queue_removals.discard(fingerprint)
            logger.info("Queued failure %s for later processing.", fingerprint[:12])

    def get_queued_failures(self) -> list[str]:
        """Return the list of queued failure fingerprints."""
        return list(self._queued_failures)

    def dequeue_failure(self, fingerprint: str) -> None:
        """Remove a fingerprint from the queue after successful processing."""
        if fingerprint in self._queued_failures:
            self._queued_failures.remove(fingerprint)
            if fingerprint in self._pending_queue_additions:
                self._pending_queue_additions.remove(fingerprint)
            else:
                self._pending_queue_removals.add(fingerprint)

    # --- AI execution metrics ---

    def record_ai_metric(self, name: str, amount: int = 1) -> None:
        """Accumulate a named AI execution counter in the durable state."""
        metric_name = str(name).strip()
        if not metric_name:
            return
        metric_amount = int(amount)
        if metric_amount == 0:
            return
        self._ai_metrics[metric_name] = (
            self._ai_metrics.get(metric_name, 0) + metric_amount
        )
        self._pending_ai_metrics[metric_name] += metric_amount

    def get_ai_metrics(self) -> dict[str, int]:
        """Return a copy of accumulated AI execution counters."""
        return dict(self._ai_metrics)

    # --- Persistence ---

    def to_dict(self) -> dict:
        """Serialize rate limiter state to a JSON-compatible dict."""
        return {
            "pr_timestamps": self._pr_timestamps,
            "token_usage": self._token_usage,
            "token_window_start": self._token_window_start,
            "queued_failures": self._queued_failures,
            "ai_metrics": self._ai_metrics,
        }

    def from_dict(self, data: dict) -> None:
        """Restore rate limiter state from a dict."""
        self._pr_timestamps = data.get("pr_timestamps", [])
        self._token_usage = data.get("token_usage", 0)
        self._token_window_start = data.get(
            "token_window_start", datetime.now(timezone.utc).isoformat()
        )
        self._queued_failures = data.get("queued_failures", [])
        raw_ai_metrics = data.get("ai_metrics", {})
        self._ai_metrics = {}
        if isinstance(raw_ai_metrics, dict):
            for key, value in raw_ai_metrics.items():
                self._ai_metrics[str(key)] = int(value)
        self._pending_pr_timestamps = []
        self._pending_token_delta = 0
        self._pending_token_window_start = self._token_window_start
        self._pending_queue_additions = set()
        self._pending_queue_removals = set()
        self._pending_ai_metrics = Counter()
        self._reserved_pr_timestamps = []

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
            data, _contents = self._read_remote_state(repo)
            self.from_dict(data)
            logger.info("Loaded rate limiter state.")
        except Exception as exc:
            if not _is_missing_state_error(exc):
                raise RuntimeError(f"failed to load rate limiter state: {exc}") from exc
            logger.info("Could not load rate limiter state (may not exist yet): %s", exc)

    def save(self) -> None:
        """Save rate limiter state to the bot-data branch."""
        if not self._state_gh or not self._state_repo_name:
            logger.warning("Cannot save rate limiter state: no GitHub client or repo.")
            return
        try:
            repo = self._state_gh.get_repo(self._state_repo_name)
            self._ensure_state_branch(repo)
            for attempt in range(1, _MAX_PERSIST_ATTEMPTS + 1):
                remote_data, existing = self._read_remote_state(repo)
                merged = self._merge_remote_state(remote_data)
                content = json.dumps(merged, indent=2)
                try:
                    if existing is None:
                        repo.create_file(
                            _RATE_STATE_FILE,
                            "Initialize rate limiter state",
                            content,
                            branch=_RATE_STATE_BRANCH,
                        )
                    else:
                        existing_sha = getattr(existing, "sha", "")
                        repo.update_file(
                            _RATE_STATE_FILE,
                            "Update rate limiter state",
                            content,
                            existing_sha,
                            branch=_RATE_STATE_BRANCH,
                        )
                    self.from_dict(merged)
                    logger.info("Saved rate limiter state.")
                    return
                except Exception as exc:
                    if attempt < _MAX_PERSIST_ATTEMPTS and _is_write_conflict(exc):
                        logger.info(
                            "Rate limiter write conflict on attempt %d/%d; reloading and retrying.",
                            attempt,
                            _MAX_PERSIST_ATTEMPTS,
                        )
                        continue
                    raise
        except Exception as exc:
            logger.error("Failed to save rate limiter state: %s", exc)

    def _read_remote_state(self, repo) -> tuple[dict, object | None]:
        """Load the remote state payload and its GitHub contents object."""
        try:
            contents = repo.get_contents(_RATE_STATE_FILE, ref=_RATE_STATE_BRANCH)
        except GithubException as exc:
            if exc.status == 404:
                return {}, None
            raise
        except FileNotFoundError:
            return {}, None

        if isinstance(contents, list):
            raise ValueError("Rate limiter state path resolved to a directory.")
        return json.loads(contents.decoded_content.decode()), contents

    def _merge_remote_state(self, remote_data: dict) -> dict:
        """Merge this process's pending mutations into the latest remote snapshot."""
        now = datetime.now(timezone.utc)
        merged = dict(remote_data)

        remote_pr_timestamps = list(merged.get("pr_timestamps", []))
        remote_pr_timestamps.extend(self._pending_pr_timestamps)
        cutoff = now - timedelta(hours=24)
        merged["pr_timestamps"] = [
            ts for ts in remote_pr_timestamps
            if datetime.fromisoformat(ts) > cutoff
        ]

        remote_window_start = str(
            merged.get("token_window_start", now.isoformat())
        )
        remote_start = datetime.fromisoformat(remote_window_start)
        remote_usage = int(merged.get("token_usage", 0))
        if now - remote_start > timedelta(hours=24):
            remote_usage = 0
            remote_window_start = now.isoformat()
            remote_start = now

        pending_window_start = self._pending_token_window_start or self._token_window_start
        pending_start = datetime.fromisoformat(pending_window_start)
        if now - pending_start > timedelta(hours=24):
            merged["token_usage"] = remote_usage
            merged["token_window_start"] = remote_window_start
        elif pending_start > remote_start:
            merged["token_usage"] = max(0, self._pending_token_delta)
            merged["token_window_start"] = pending_window_start
        elif pending_start < remote_start:
            logger.info(
                "Skipping stale local token delta because the remote token window is newer.",
            )
            merged["token_usage"] = remote_usage
            merged["token_window_start"] = remote_window_start
        else:
            merged["token_usage"] = max(0, remote_usage + self._pending_token_delta)
            merged["token_window_start"] = remote_window_start

        queue = list(merged.get("queued_failures", []))
        for fingerprint in self._pending_queue_additions:
            if fingerprint not in queue:
                queue.append(fingerprint)
        queue = [
            fingerprint for fingerprint in queue
            if fingerprint not in self._pending_queue_removals
        ]
        merged["queued_failures"] = queue

        raw_ai_metrics = merged.get("ai_metrics", {})
        ai_metrics: dict[str, int] = {}
        if isinstance(raw_ai_metrics, dict):
            for key, value in raw_ai_metrics.items():
                ai_metrics[str(key)] = int(value)
        for key, amount in self._pending_ai_metrics.items():
            ai_metrics[key] = ai_metrics.get(key, 0) + int(amount)
        merged["ai_metrics"] = ai_metrics
        return merged

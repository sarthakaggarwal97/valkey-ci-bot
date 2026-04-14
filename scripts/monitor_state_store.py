"""Persistent state for centralized workflow-run monitoring."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from github.GithubException import GithubException

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)

_STATE_BRANCH = "bot-data"
_STATE_FILE = "monitor-state.json"
_MAX_PERSIST_ATTEMPTS = 3


def _github_status(exc: Exception) -> int | None:
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


def _is_missing_state_error(exc: Exception) -> bool:
    """Return whether the state branch or file is simply absent."""
    if isinstance(exc, GithubException):
        return exc.status == 404
    return isinstance(exc, FileNotFoundError)


def _is_write_conflict(exc: Exception) -> bool:
    """Return whether the remote state changed underneath this writer."""
    status = _github_status(exc)
    if status in {409, 422}:
        return True
    message = str(exc).lower()
    return "sha" in message or "already exists" in message or "conflict" in message


class MonitorStateStore:
    """Persists workflow-run monitoring watermarks on the data branch."""

    def __init__(
        self,
        github_client: "Github | None" = None,
        repo_full_name: str = "",
    ) -> None:
        self._gh = github_client
        self._repo_name = repo_full_name
        self._entries: dict[str, dict[str, str | int]] = {}

    def get_last_seen_run_id(self, key: str) -> int:
        """Return the stored watermark for a monitor key."""
        entry = self._entries.get(key, {})
        run_id = entry.get("last_seen_run_id", 0)
        return int(run_id) if isinstance(run_id, int) else 0

    def mark_seen(
        self,
        key: str,
        *,
        last_seen_run_id: int,
        target_repo: str,
        workflow_file: str,
        event: str,
    ) -> None:
        """Update the watermark and metadata for a monitor key."""
        self._entries[key] = {
            "last_seen_run_id": last_seen_run_id,
            "target_repo": target_repo,
            "workflow_file": workflow_file,
            "event": event,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def to_dict(self) -> dict[str, dict[str, str | int]]:
        """Serialize state to a JSON-compatible mapping."""
        return dict(self._entries)

    def from_dict(self, data: dict) -> None:
        """Restore state from a previously serialized mapping."""
        self._entries.clear()
        for key, raw in data.items():
            if not isinstance(key, str) or not isinstance(raw, dict):
                continue
            self._entries[key] = {
                "last_seen_run_id": raw.get("last_seen_run_id", 0),
                "target_repo": str(raw.get("target_repo", "")),
                "workflow_file": str(raw.get("workflow_file", "")),
                "event": str(raw.get("event", "")),
                "updated_at": str(raw.get("updated_at", "")),
            }

    def _ensure_state_branch(self, repo) -> None:
        """Create the data branch from the default branch when missing."""
        try:
            repo.get_git_ref(f"heads/{_STATE_BRANCH}")
            return
        except GithubException as exc:
            if exc.status != 404:
                raise
        except FileNotFoundError:
            pass

        base_ref = repo.get_git_ref(f"heads/{repo.default_branch}")
        repo.create_git_ref(
            ref=f"refs/heads/{_STATE_BRANCH}",
            sha=base_ref.object.sha,
        )

    def load(self) -> None:
        """Load monitor state from the data branch."""
        if not self._gh or not self._repo_name:
            logger.info("No GitHub client; starting with empty monitor state.")
            return
        try:
            repo = self._gh.get_repo(self._repo_name)
            contents = repo.get_contents(_STATE_FILE, ref=_STATE_BRANCH)
            if isinstance(contents, list):
                raise ValueError("Monitor state path resolved to a directory.")
            data = json.loads(contents.decoded_content.decode())
            if isinstance(data, dict):
                self.from_dict(data)
            logger.info("Loaded monitor state with %d entries.", len(self._entries))
        except Exception as exc:
            if not _is_missing_state_error(exc):
                raise RuntimeError(f"failed to load monitor state: {exc}") from exc
            logger.info("Could not load monitor state (may not exist yet): %s", exc)
            self._entries.clear()

    def save(self) -> None:
        """Save monitor state to the data branch."""
        if not self._gh or not self._repo_name:
            logger.warning("Cannot save monitor state: no GitHub client or repo.")
            return
        try:
            repo = self._gh.get_repo(self._repo_name)
            self._ensure_state_branch(repo)
            for attempt in range(1, _MAX_PERSIST_ATTEMPTS + 1):
                existing_entries: dict[str, dict[str, str | int]] = {}
                try:
                    existing = repo.get_contents(_STATE_FILE, ref=_STATE_BRANCH)
                except Exception as exc:
                    if not _is_missing_state_error(exc):
                        raise
                    existing = None

                if isinstance(existing, list):
                    raise ValueError("Monitor state path resolved to a directory.")
                if existing is not None:
                    data = json.loads(existing.decoded_content.decode())
                    if isinstance(data, dict):
                        for key, raw in data.items():
                            if isinstance(key, str) and isinstance(raw, dict):
                                existing_entries[key] = {
                                    "last_seen_run_id": raw.get("last_seen_run_id", 0),
                                    "target_repo": str(raw.get("target_repo", "")),
                                    "workflow_file": str(raw.get("workflow_file", "")),
                                    "event": str(raw.get("event", "")),
                                    "updated_at": str(raw.get("updated_at", "")),
                                }

                merged_entries = dict(existing_entries)
                merged_entries.update(self.to_dict())
                content = json.dumps(merged_entries, indent=2)
                try:
                    if existing is None:
                        repo.create_file(
                            _STATE_FILE,
                            "Initialize monitor state",
                            content,
                            branch=_STATE_BRANCH,
                        )
                    else:
                        repo.update_file(
                            _STATE_FILE,
                            "Update monitor state",
                            content,
                            existing.sha,
                            branch=_STATE_BRANCH,
                        )
                    self._entries = merged_entries
                    logger.info("Saved monitor state with %d entries.", len(self._entries))
                    return
                except Exception as exc:
                    if attempt < _MAX_PERSIST_ATTEMPTS and _is_write_conflict(exc):
                        logger.info(
                            "Monitor state write conflict on attempt %d/%d; retrying.",
                            attempt,
                            _MAX_PERSIST_ATTEMPTS,
                        )
                        continue
                    raise
        except Exception as exc:
            raise RuntimeError(f"failed to save monitor state: {exc}") from exc

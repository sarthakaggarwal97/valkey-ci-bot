"""Append-only event ledger for CI agent decisions and outcomes."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)

_LEDGER_BRANCH = "bot-data"
_LEDGER_FILE = "agent-events.jsonl"
_MAX_PERSIST_ATTEMPTS = 3


JsonObject = Dict[str, Any]


def _github_status(exc: Exception) -> int | None:
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


def _is_write_conflict(exc: Exception) -> bool:
    status = _github_status(exc)
    if status in {409, 422}:
        return True
    message = str(exc).lower()
    return "sha" in message or "already exists" in message or "conflict" in message


@dataclass
class AgentEvent:
    """One durable fact emitted by the CI agent."""

    event_id: str
    event_type: str
    created_at: str
    subject: str
    attributes: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "created_at": self.created_at,
            "subject": self.subject,
            "attributes": self.attributes,
        }


def make_event(
    event_type: str,
    subject: str,
    *,
    created_at: str | None = None,
    **attributes: Any,
) -> AgentEvent:
    """Create a stable event object from event data."""
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    normalized_type = str(event_type).strip()
    normalized_subject = str(subject).strip()
    payload = json.dumps(
        {
            "event_type": normalized_type,
            "created_at": timestamp,
            "subject": normalized_subject,
            "attributes": attributes,
        },
        sort_keys=True,
        default=str,
    )
    event_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return AgentEvent(
        event_id=event_id,
        event_type=normalized_type,
        created_at=timestamp,
        subject=normalized_subject,
        attributes=dict(attributes),
    )


def parse_events(text: str) -> list[JsonObject]:
    """Parse JSONL event ledger content, skipping malformed lines."""
    events: list[JsonObject] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def load_events_from_path(path: str | Path) -> list[JsonObject]:
    """Load events from a local JSONL ledger path."""
    ledger_path = Path(path)
    if not ledger_path.exists():
        return []
    return parse_events(ledger_path.read_text(encoding="utf-8"))


class EventLedger:
    """Remote-backed append-only ledger for agent facts.

    Events are stored as JSON lines on the ``bot-data`` branch. Writes append
    pending events to the latest remote content and retry on SHA conflicts.
    """

    def __init__(
        self,
        github_client: "Github | None" = None,
        repo_full_name: str = "",
        *,
        state_github_client: "Github | None" = None,
        state_repo_full_name: str | None = None,
    ) -> None:
        self._gh = github_client
        self._repo_name = repo_full_name
        self._state_gh = state_github_client or github_client
        self._state_repo_name = state_repo_full_name or repo_full_name
        self._pending: list[AgentEvent] = []

    @property
    def pending(self) -> list[AgentEvent]:
        return list(self._pending)

    def record(self, event_type: str, subject: str, **attributes: Any) -> None:
        """Append an event to the pending write buffer."""
        if not event_type or not subject:
            return
        self._pending.append(make_event(event_type, subject, **attributes))

    def _ensure_state_branch(self, repo: Any) -> None:
        try:
            repo.get_git_ref(f"heads/{_LEDGER_BRANCH}")
            return
        except Exception as exc:
            if _github_status(exc) != 404:
                raise
        except FileNotFoundError:
            pass

        base_ref = repo.get_git_ref(f"heads/{repo.default_branch}")
        repo.create_git_ref(
            ref=f"refs/heads/{_LEDGER_BRANCH}",
            sha=base_ref.object.sha,
        )

    def save(self) -> None:
        """Persist pending events to the remote ledger."""
        if not self._pending:
            return
        if not self._state_gh or not self._state_repo_name:
            logger.info("No GitHub client; event ledger has %d unsaved event(s).", len(self._pending))
            return

        try:
            repo = self._state_gh.get_repo(self._state_repo_name)
            self._ensure_state_branch(repo)
            for attempt in range(1, _MAX_PERSIST_ATTEMPTS + 1):
                remote_text, existing = self._read_remote_ledger(repo)
                content = self._merge(remote_text)
                try:
                    if existing is None:
                        repo.create_file(
                            _LEDGER_FILE,
                            "Initialize CI agent event ledger",
                            content,
                            branch=_LEDGER_BRANCH,
                        )
                    else:
                        repo.update_file(
                            _LEDGER_FILE,
                            "Append CI agent events",
                            content,
                            existing.sha,
                            branch=_LEDGER_BRANCH,
                        )
                    logger.info("Saved %d CI agent event(s).", len(self._pending))
                    self._pending.clear()
                    return
                except Exception as exc:
                    if attempt < _MAX_PERSIST_ATTEMPTS and _is_write_conflict(exc):
                        logger.info(
                            "Event ledger write conflict on attempt %d/%d; retrying.",
                            attempt,
                            _MAX_PERSIST_ATTEMPTS,
                        )
                        continue
                    raise
        except Exception as exc:
            logger.error("Failed to save event ledger: %s", exc)

    def _read_remote_ledger(self, repo: Any) -> tuple[str, Any | None]:
        try:
            contents = repo.get_contents(_LEDGER_FILE, ref=_LEDGER_BRANCH)
        except Exception as exc:
            if _github_status(exc) == 404:
                return "", None
            raise
        except FileNotFoundError:
            return "", None

        if isinstance(contents, list):
            raise ValueError("Event ledger path resolved to a directory.")
        return contents.decoded_content.decode("utf-8"), contents

    def _merge(self, remote_text: str) -> str:
        existing_ids = {
            str(event.get("event_id"))
            for event in parse_events(remote_text)
            if event.get("event_id")
        }
        lines = [line for line in remote_text.splitlines() if line.strip()]
        for event in self._pending:
            if event.event_id in existing_ids:
                continue
            lines.append(json.dumps(event.to_dict(), sort_keys=True, default=str))
        return "\n".join(lines) + "\n"

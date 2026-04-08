"""Permission and safety gating for PR reviewer events."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from scripts.config import ReviewerConfig
from scripts.github_client import retry_github_call
from scripts.models import GithubEvent
from scripts.pr_event_router import PREventRouter

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)


class PermissionGate:
    """Enforces collaborator and event-context safety checks."""

    def __init__(
        self,
        github_client: "Github | None",
        *,
        github_retries: int = 5,
    ) -> None:
        self._gh = github_client
        self._router = PREventRouter()
        self._github_retries = github_retries

    def classify_event(self, event: GithubEvent) -> str:
        """Classify the event using the shared PR event router."""
        return self._router.classify_event(event)

    def actor_is_collaborator(self, repo: str, actor: str) -> bool:
        """Return ``True`` when the actor has collaborator-equivalent access."""
        if not self._gh or not repo or not actor:
            return False
        gh = self._gh
        try:
            permission = retry_github_call(
                lambda: gh.get_repo(repo).get_collaborator_permission(actor),
                retries=self._github_retries,
                description=f"check collaborator permission for {actor} on {repo}",
            )
        except Exception as exc:
            logger.warning(
                "Could not determine collaborator permission for %s on %s: %s",
                actor,
                repo,
                exc,
            )
            return False
        return permission in {"write", "admin", "maintain", "triage"}

    def may_process(
        self,
        event: GithubEvent,
        config: ReviewerConfig,
    ) -> tuple[bool, str | None]:
        """Return whether the reviewer may process the event."""
        mode = self.classify_event(event)
        if mode == "skip":
            return False, "unsupported-comment-context"

        collaborator_required = config.collaborator_only or (
            mode == "chat" and config.chat_collaborator_only
        )
        if collaborator_required and not self.actor_is_collaborator(event.repo, event.actor):
            return False, "non-collaborator"

        return True, None

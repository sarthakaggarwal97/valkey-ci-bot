"""GitHub event parsing and mode classification for the PR reviewer."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.models import GithubEvent


class PREventRouter:
    """Classifies reviewer events into review, chat, or skip."""

    def classify_event(self, event: GithubEvent) -> str:
        """Return ``review``, ``chat``, or ``skip`` for a normalized event."""
        if event.pr_number is None:
            return "skip"

        if event.event_name in {"pull_request", "pull_request_target"}:
            return "review"

        if event.event_name == "pull_request_review_comment":
            return "chat" if event.in_reply_to_id is not None else "skip"

        if event.event_name == "issue_comment":
            body = event.body or ""
            return "chat" if "/reviewagent" in body else "skip"

        return "skip"


def load_event_from_path(event_name: str, event_path: str | Path) -> GithubEvent:
    """Load a normalized GitHub event from ``GITHUB_EVENT_PATH`` JSON."""
    payload = json.loads(Path(event_path).read_text())
    repo = str(payload.get("repository", {}).get("full_name", ""))
    actor = str(payload.get("sender", {}).get("login", ""))
    pull_request = payload.get("pull_request", {})
    issue = payload.get("issue", {})
    comment = payload.get("comment", {})

    pr_number = pull_request.get("number")
    if pr_number is None and issue.get("pull_request") is not None:
        pr_number = issue.get("number")

    body: str | None = None
    comment_id: int | None = None
    is_review_comment = False
    comment_path: str | None = None
    comment_line: int | None = None
    in_reply_to_id: int | None = None

    if event_name in {"pull_request", "pull_request_target"}:
        body = pull_request.get("body") or ""
    elif event_name == "pull_request_review_comment":
        body = comment.get("body") or ""
        comment_id = comment.get("id")
        is_review_comment = True
        comment_path = comment.get("path")
        comment_line = comment.get("line") or comment.get("original_line")
        in_reply_to_id = comment.get("in_reply_to_id")
    elif event_name == "issue_comment":
        body = comment.get("body") or ""
        comment_id = comment.get("id")

    return GithubEvent(
        event_name=event_name,
        repo=repo,
        actor=actor,
        pr_number=int(pr_number) if pr_number is not None else None,
        comment_id=int(comment_id) if comment_id is not None else None,
        body=body,
        is_review_comment=is_review_comment,
        comment_path=comment_path,
        comment_line=int(comment_line) if comment_line is not None else None,
        in_reply_to_id=int(in_reply_to_id) if in_reply_to_id is not None else None,
    )

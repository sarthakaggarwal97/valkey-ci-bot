"""Publishing helpers for PR summaries, review comments, and chat replies."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from scripts.github_client import retry_github_call
from scripts.models import ReviewFinding

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)

SUMMARY_MARKER = "<!-- pr-review-bot:summary -->"


class CommentPublisher:
    """Publishes reviewer outputs back to GitHub."""

    def __init__(self, github_client: "Github", *, github_retries: int = 5) -> None:
        self._gh = github_client
        self._github_retries = github_retries
        self._bot_login: str | None = None

    def upsert_summary(
        self,
        repo: str,
        pr_number: int,
        comment_id: int | None,
        body: str,
    ) -> int:
        """Create or update the PR summary comment and return its comment id."""
        pr = retry_github_call(
            lambda: self._gh.get_repo(repo).get_pull(pr_number),
            retries=self._github_retries,
            description=f"load PR {repo}#{pr_number}",
        )
        full_body = self._summary_body(body)
        if comment_id is not None:
            try:
                comment = retry_github_call(
                    lambda: pr.get_issue_comment(comment_id),
                    retries=self._github_retries,
                    description=f"load summary comment {comment_id}",
                )
                if not self._is_bot_authored_comment(comment):
                    logger.info(
                        "Cached summary comment %s is not bot-authored; creating a new summary comment.",
                        comment_id,
                    )
                else:
                    retry_github_call(
                        lambda: comment.edit(full_body),
                        retries=self._github_retries,
                        description=f"edit summary comment {comment_id}",
                    )
                    return comment.id
            except Exception as exc:
                logger.info("Could not update cached summary comment %s: %s", comment_id, exc)

        for comment in retry_github_call(
            lambda: list(pr.get_issue_comments()),
            retries=self._github_retries,
            description=f"list PR issue comments for {repo}#{pr_number}",
        ):
            if (
                SUMMARY_MARKER in (comment.body or "")
                and self._is_bot_authored_comment(comment)
            ):
                retry_github_call(
                    lambda: comment.edit(full_body),
                    retries=self._github_retries,
                    description=f"edit summary comment {comment.id}",
                )
                return comment.id

        return retry_github_call(
            lambda: pr.create_issue_comment(full_body).id,
            retries=self._github_retries,
            description=f"create summary comment for {repo}#{pr_number}",
        )

    def publish_review_comments(
        self,
        repo: str,
        pr_number: int,
        findings: list[ReviewFinding],
        *,
        commit_sha: str | None = None,
    ) -> list[int]:
        """Publish review comments as a single batched review.

        All findings are submitted in one ``create_review`` call so that
        GitHub sends a single notification email instead of one per comment.
        """
        if not findings:
            return []

        pr = retry_github_call(
            lambda: self._gh.get_repo(repo).get_pull(pr_number),
            retries=self._github_retries,
            description=f"load PR {repo}#{pr_number}",
        )
        commit = commit_sha or pr.head.sha

        review_comments: list[dict] = []
        for finding in findings:
            comment_dict: dict = {
                "path": finding.path,
                "body": finding.body,
            }
            if finding.line is not None and finding.line > 0:
                comment_dict["line"] = finding.line
                comment_dict["side"] = "RIGHT"
            else:
                comment_dict["subject_type"] = "file"
            review_comments.append(comment_dict)

        try:
            repo_obj = pr.base.repo

            def _create_review() -> dict:
                _headers, data = repo_obj._requester.requestJsonAndCheck(
                    "POST",
                    f"/repos/{repo}/pulls/{pr_number}/reviews",
                    input={
                        "commit_id": commit,
                        "body": "",
                        "event": "COMMENT",
                        "comments": review_comments,
                    },
                )
                return data  # type: ignore[return-value]

            data = retry_github_call(
                _create_review,
                retries=self._github_retries,
                description=f"create batched review for {repo}#{pr_number}",
            )
            # The review ID is always present; individual comment IDs
            # are not returned inline, so we use the review ID as the
            # tracking identifier.
            review_id = data.get("id") if isinstance(data, dict) else None
            return [review_id] if review_id else []
        except Exception as exc:
            logger.warning(
                "Batched review creation failed for %s#%d: %s. "
                "Falling back to individual comments.",
                repo,
                pr_number,
                exc,
            )
            return self._publish_review_comments_individually(
                pr, commit, findings,
            )

    def _publish_review_comments_individually(
        self,
        pr,
        commit: str,
        findings: list[ReviewFinding],
    ) -> list[int]:
        """Fallback: publish each finding as a standalone review comment."""
        comment_ids: list[int] = []
        for finding in findings:
            try:
                if finding.line is not None and finding.line > 0:
                    line = finding.line
                    comment = retry_github_call(
                        lambda: pr.create_review_comment(
                            finding.body,
                            commit,
                            finding.path,
                            line=line,
                            side="RIGHT",
                        ),
                        retries=self._github_retries,
                        description=f"create line review comment for {finding.path}",
                    )
                else:
                    comment = retry_github_call(
                        lambda: pr.create_review_comment(
                            finding.body,
                            commit,
                            finding.path,
                            subject_type="file",
                        ),
                        retries=self._github_retries,
                        description=f"create file review comment for {finding.path}",
                    )
            except Exception as exc:
                logger.info(
                    "Falling back to file-level review comment for %s:%s: %s",
                    finding.path,
                    finding.line,
                    exc,
                )
                try:
                    comment = retry_github_call(
                        lambda: pr.create_review_comment(
                            finding.body,
                            commit,
                            finding.path,
                            subject_type="file",
                        ),
                        retries=self._github_retries,
                        description=f"fallback file review comment for {finding.path}",
                    )
                except Exception as final_exc:
                    logger.warning(
                        "Failed to publish review comment on %s: %s",
                        finding.path,
                        final_exc,
                    )
                    continue
            comment_ids.append(comment.id)
        return comment_ids

    def publish_chat_reply(
        self,
        repo: str,
        pr_number: int,
        comment_id: int,
        body: str,
        *,
        review_comment: bool,
    ) -> int:
        """Publish a chat reply to a review thread or as a PR issue comment."""
        pr = retry_github_call(
            lambda: self._gh.get_repo(repo).get_pull(pr_number),
            retries=self._github_retries,
            description=f"load PR {repo}#{pr_number}",
        )
        if review_comment:
            return retry_github_call(
                lambda: pr.create_review_comment_reply(comment_id, body).id,
                retries=self._github_retries,
                description=f"create review reply {comment_id}",
            )
        return retry_github_call(
            lambda: pr.create_issue_comment(body).id,
            retries=self._github_retries,
            description=f"create PR issue comment for {repo}#{pr_number}",
        )

    @staticmethod
    def _summary_body(body: str) -> str:
        body = body.strip()
        if SUMMARY_MARKER in body:
            return body
        return f"{SUMMARY_MARKER}\n{body}"

    def _is_bot_authored_comment(self, comment) -> bool:
        """Return True only for comments authored by the authenticated bot."""
        bot_login = self._get_bot_login()
        if not bot_login:
            return False
        user = getattr(comment, "user", None)
        login = getattr(user, "login", None)
        return login == bot_login

    def _get_bot_login(self) -> str | None:
        """Resolve the authenticated GitHub login once and cache it."""
        if self._bot_login is not None:
            return self._bot_login
        try:
            self._bot_login = retry_github_call(
                lambda: self._gh.get_user().login,
                retries=self._github_retries,
                description="load authenticated GitHub user",
            )
        except Exception as exc:
            logger.warning("Could not determine authenticated GitHub user: %s", exc)
            self._bot_login = ""
        return self._bot_login or None

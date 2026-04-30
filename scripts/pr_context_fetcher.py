"""GitHub API fetcher for PR review context."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from scripts.github_client import retry_github_call
from scripts.models import (
    ChangedFile,
    DiffScope,
    ExistingReviewComment,
    PullRequestCommit,
    PullRequestContext,
    ReviewThread,
)
from scripts.path_filter import is_unsupported_review_path

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)


class PRContextFetcher:
    """Fetches PR metadata, changed files, contents, and thread context."""

    def __init__(
        self,
        github_client: "Github",
        *,
        max_file_bytes: int = 512_000,
        github_retries: int = 5,
    ) -> None:
        self._gh = github_client
        self._max_file_bytes = max_file_bytes
        self._github_retries = github_retries
        self._bot_login: str | None = None

    def fetch(self, repo_name: str, pr_number: int) -> PullRequestContext:
        """Load PR metadata and changed files via the GitHub API."""
        repo = retry_github_call(
            lambda: self._gh.get_repo(repo_name),
            retries=self._github_retries,
            description=f"load repository {repo_name}",
        )
        pr = retry_github_call(
            lambda: repo.get_pull(pr_number),
            retries=self._github_retries,
            description=f"load PR {repo_name}#{pr_number}",
        )
        files: list[ChangedFile] = []
        for raw_file in retry_github_call(
            lambda: list(pr.get_files()),
            retries=self._github_retries,
            description=f"list files for {repo_name}#{pr_number}",
        ):
            patch = getattr(raw_file, "patch", None)
            files.append(
                ChangedFile(
                    path=raw_file.filename,
                    status=raw_file.status or "",
                    additions=int(raw_file.additions or 0),
                    deletions=int(raw_file.deletions or 0),
                    patch=patch,
                    contents=None,
                    is_binary=patch is None
                    and is_unsupported_review_path(raw_file.filename),
                )
            )
        review_comments: list[ExistingReviewComment] = []
        try:
            for raw_comment in retry_github_call(
                lambda: list(pr.get_review_comments()),
                retries=self._github_retries,
                description=f"list review comments for {repo_name}#{pr_number}",
            ):
                path = str(getattr(raw_comment, "path", "") or "").strip()
                body = str(getattr(raw_comment, "body", "") or "").strip()
                if not path or not body:
                    continue
                line = getattr(raw_comment, "line", None)
                if line is None:
                    line = getattr(raw_comment, "original_line", None)
                review_comments.append(
                    ExistingReviewComment(
                        path=path,
                        line=int(line) if isinstance(line, int) else None,
                        author=(
                            getattr(getattr(raw_comment, "user", None), "login", "")
                            or "unknown"
                        ),
                        body=body,
                        in_reply_to_id=getattr(raw_comment, "in_reply_to_id", None),
                    )
                )
        except Exception as exc:
            logger.warning(
                "Failed to load review comments for %s#%d: %s",
                repo_name,
                pr_number,
                exc,
            )
        commits: list[PullRequestCommit] = []
        try:
            for raw_commit in retry_github_call(
                lambda: list(pr.get_commits()),
                retries=self._github_retries,
                description=f"list commits for {repo_name}#{pr_number}",
            ):
                commit = getattr(raw_commit, "commit", None)
                commits.append(
                    PullRequestCommit(
                        sha=str(getattr(raw_commit, "sha", "") or ""),
                        message=str(getattr(commit, "message", "") or ""),
                    )
                )
        except Exception as exc:
            logger.warning(
                "Failed to load commits for %s#%d: %s",
                repo_name,
                pr_number,
                exc,
            )
        return PullRequestContext(
            repo=repo_name,
            number=pr_number,
            title=pr.title or "",
            body=pr.body or "",
            base_sha=pr.base.sha,
            head_sha=pr.head.sha,
            author=pr.user.login if pr.user else "",
            files=files,
            review_comments=review_comments,
            commits=commits,
            base_ref=getattr(pr.base, "ref", "") or "",
            head_ref=getattr(pr.head, "ref", "") or "",
            labels=[
                str(getattr(label, "name", "") or "")
                for label in getattr(pr, "labels", [])
                if getattr(label, "name", None)
            ],
        )

    def hydrate_contents(
        self,
        context: PullRequestContext,
        selected_paths: set[str],
    ) -> PullRequestContext:
        """Fetch file contents only for selected, eligible paths."""
        repo = retry_github_call(
            lambda: self._gh.get_repo(context.repo),
            retries=self._github_retries,
            description=f"load repository {context.repo}",
        )
        hydrated: list[ChangedFile] = []
        for changed_file in context.files:
            if (
                changed_file.path not in selected_paths
                or changed_file.is_binary
                or changed_file.status == "removed"
            ):
                hydrated.append(changed_file)
                continue

            try:
                cf_path = changed_file.path

                def _fetch(p: str = cf_path):  # type: ignore[assignment]
                    return repo.get_contents(p, ref=context.head_sha)

                contents = retry_github_call(
                    _fetch,
                    retries=self._github_retries,
                    description=f"load file contents for {changed_file.path}",
                )
                if isinstance(contents, list):
                    hydrated.append(changed_file)
                    continue
                if getattr(contents, "size", 0) > self._max_file_bytes:
                    logger.info(
                        "Skipping oversized file contents for %s (%s bytes).",
                        changed_file.path,
                        contents.size,
                    )
                    hydrated.append(changed_file)
                    continue
                hydrated.append(
                    replace(
                        changed_file,
                        contents=contents.decoded_content.decode(
                            "utf-8", errors="replace"
                        ),
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Failed to fetch contents for %s at %s: %s",
                    changed_file.path,
                    context.head_sha,
                    exc,
                )
                hydrated.append(changed_file)
        return replace(context, files=hydrated)

    def build_diff_scope(
        self,
        context: PullRequestContext,
        previous_head_sha: str | None,
    ) -> DiffScope:
        """Build an incremental diff scope when the previous head is comparable."""
        if not previous_head_sha:
            return DiffScope(
                base_sha=context.base_sha,
                head_sha=context.head_sha,
                files=context.files,
                incremental=False,
            )

        if previous_head_sha == context.head_sha:
            return DiffScope(
                base_sha=previous_head_sha,
                head_sha=context.head_sha,
                files=[],
                incremental=True,
            )

        repo = retry_github_call(
            lambda: self._gh.get_repo(context.repo),
            retries=self._github_retries,
            description=f"load repository {context.repo}",
        )
        try:
            comparison = retry_github_call(
                lambda: repo.compare(previous_head_sha, context.head_sha),
                retries=self._github_retries,
                description=(
                    f"compare {previous_head_sha[:12]}..{context.head_sha[:12]}"
                ),
            )
            status = getattr(comparison, "status", "")
            if status not in {"ahead", "identical"}:
                raise ValueError(f"Untrusted comparison status: {status}")
            comparison_files = {
                raw_file.filename: raw_file for raw_file in comparison.files
            }
        except Exception as exc:
            logger.info(
                "Falling back to full review for %s#%d: %s",
                context.repo,
                context.number,
                exc,
            )
            return DiffScope(
                base_sha=context.base_sha,
                head_sha=context.head_sha,
                files=context.files,
                incremental=False,
            )

        files = []
        for changed_file in context.files:
            raw_file = comparison_files.get(changed_file.path)
            if raw_file is None:
                continue
            patch = getattr(raw_file, "patch", None)
            files.append(
                replace(
                    changed_file,
                    patch=patch,
                    is_binary=patch is None
                    and is_unsupported_review_path(changed_file.path),
                )
            )
        return DiffScope(
            base_sha=previous_head_sha,
            head_sha=context.head_sha,
            files=files,
            incremental=True,
        )

    def fetch_review_thread(
        self,
        repo_name: str,
        pr_number: int,
        comment_id: int,
        *,
        review_comment: bool,
    ) -> ReviewThread:
        """Fetch review-thread or issue-comment context for chat mode."""
        repo = retry_github_call(
            lambda: self._gh.get_repo(repo_name),
            retries=self._github_retries,
            description=f"load repository {repo_name}",
        )
        pr = retry_github_call(
            lambda: repo.get_pull(pr_number),
            retries=self._github_retries,
            description=f"load PR {repo_name}#{pr_number}",
        )

        if review_comment:
            comment = retry_github_call(
                lambda: pr.get_review_comment(comment_id),
                retries=self._github_retries,
                description=f"load review comment {comment_id}",
            )
            conversation: list[str] = []
            reply_to_bot = False
            if getattr(comment, "in_reply_to_id", None):
                parent = retry_github_call(
                    lambda: pr.get_review_comment(comment.in_reply_to_id),
                    retries=self._github_retries,
                    description=f"load review comment parent {comment.in_reply_to_id}",
                )
                conversation.append(parent.body or "")
                reply_to_bot = self._is_bot_authored(parent)
            conversation.append(comment.body or "")
            line = getattr(comment, "line", None) or getattr(
                comment, "original_line", None
            )
            return ReviewThread(
                comment_id=comment_id,
                path=getattr(comment, "path", None),
                line=int(line) if line is not None else None,
                conversation=conversation,
                reply_to_bot=reply_to_bot,
            )

        issue_comment = retry_github_call(
            lambda: pr.get_issue_comment(comment_id),
            retries=self._github_retries,
            description=f"load issue comment {comment_id}",
        )
        return ReviewThread(
            comment_id=comment_id,
            path=None,
            line=None,
            conversation=[issue_comment.body or ""],
        )

    def _is_bot_authored(self, comment) -> bool:
        """Return whether the comment was authored by the authenticated agent."""
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

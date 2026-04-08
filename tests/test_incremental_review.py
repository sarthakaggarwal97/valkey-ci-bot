"""Tests for incremental PR review scope selection."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts.models import ChangedFile, PullRequestContext
from scripts.pr_context_fetcher import PRContextFetcher


def _context() -> PullRequestContext:
    return PullRequestContext(
        repo="owner/repo",
        number=1,
        title="Title",
        body="Body",
        base_sha="base123",
        head_sha="head456",
        author="alice",
        files=[
            ChangedFile(
                path="src/a.c",
                status="modified",
                additions=3,
                deletions=1,
                patch="patch-a",
                contents="contents-a",
                is_binary=False,
            ),
            ChangedFile(
                path="src/b.c",
                status="modified",
                additions=2,
                deletions=2,
                patch="patch-b",
                contents="contents-b",
                is_binary=False,
            ),
        ],
    )


def test_build_diff_scope_limits_to_changed_files_since_last_review() -> None:
    compare = MagicMock()
    compare.status = "ahead"
    compare.files = [MagicMock(filename="src/b.c", patch="incremental-patch")]

    repo = MagicMock()
    repo.compare.return_value = compare
    gh = MagicMock()
    gh.get_repo.return_value = repo

    scope = PRContextFetcher(gh).build_diff_scope(_context(), "oldsha")

    assert scope.incremental is True
    assert [changed_file.path for changed_file in scope.files] == ["src/b.c"]
    assert scope.files[0].patch == "incremental-patch"


def test_build_diff_scope_falls_back_to_full_review_when_compare_is_untrusted() -> None:
    compare = MagicMock()
    compare.status = "diverged"

    repo = MagicMock()
    repo.compare.return_value = compare
    gh = MagicMock()
    gh.get_repo.return_value = repo

    scope = PRContextFetcher(gh).build_diff_scope(_context(), "oldsha")

    assert scope.incremental is False
    assert [changed_file.path for changed_file in scope.files] == [
        "src/a.c",
        "src/b.c",
    ]


def test_fetch_review_thread_marks_bot_reply_context() -> None:
    repo = MagicMock()
    pr = MagicMock()
    gh = MagicMock()
    gh.get_repo.return_value = repo
    gh.get_user.return_value.login = "review-bot[bot]"
    repo.get_pull.return_value = pr

    parent = MagicMock()
    parent.body = "Bot review comment"
    parent.user.login = "review-bot[bot]"

    comment = MagicMock()
    comment.body = "Can you explain this?"
    comment.path = "src/a.c"
    comment.line = 12
    comment.original_line = None
    comment.in_reply_to_id = 55

    pr.get_review_comment.side_effect = [comment, parent]

    thread = PRContextFetcher(gh).fetch_review_thread(
        "owner/repo",
        1,
        99,
        review_comment=True,
    )

    assert thread.reply_to_bot is True
    assert thread.conversation == ["Bot review comment", "Can you explain this?"]


def test_fetch_review_thread_marks_non_bot_reply_context() -> None:
    repo = MagicMock()
    pr = MagicMock()
    gh = MagicMock()
    gh.get_repo.return_value = repo
    gh.get_user.return_value.login = "review-bot[bot]"
    repo.get_pull.return_value = pr

    parent = MagicMock()
    parent.body = "Human review comment"
    parent.user.login = "alice"

    comment = MagicMock()
    comment.body = "Can you explain this?"
    comment.path = "src/a.c"
    comment.line = 12
    comment.original_line = None
    comment.in_reply_to_id = 55

    pr.get_review_comment.side_effect = [comment, parent]

    thread = PRContextFetcher(gh).fetch_review_thread(
        "owner/repo",
        1,
        99,
        review_comment=True,
    )

    assert thread.reply_to_bot is False


def test_fetch_includes_existing_review_comments() -> None:
    repo = MagicMock()
    pr = MagicMock()
    gh = MagicMock()
    gh.get_repo.return_value = repo
    repo.get_pull.return_value = pr

    raw_file = MagicMock()
    raw_file.filename = "src/a.c"
    raw_file.status = "modified"
    raw_file.additions = 3
    raw_file.deletions = 1
    raw_file.patch = "@@ -1 +1 @@\n-old\n+new"

    review_comment = MagicMock()
    review_comment.path = "src/a.c"
    review_comment.line = 12
    review_comment.original_line = None
    review_comment.body = "Please double-check the timeout cleanup path."
    review_comment.in_reply_to_id = None
    review_comment.user.login = "bob"

    pr.title = "Title"
    pr.body = "Body"
    pr.base.sha = "base123"
    pr.head.sha = "head456"
    pr.user.login = "alice"
    pr.get_files.return_value = [raw_file]
    pr.get_review_comments.return_value = [review_comment]
    commit = MagicMock()
    commit.sha = "abc123"
    commit.commit.message = "Fix timeout\n\nSigned-off-by: Alice <alice@example.com>"
    pr.get_commits.return_value = [commit]

    context = PRContextFetcher(gh).fetch("owner/repo", 1)

    assert len(context.review_comments) == 1
    assert context.review_comments[0].path == "src/a.c"
    assert context.review_comments[0].line == 12
    assert context.review_comments[0].author == "bob"
    assert len(context.commits) == 1
    assert context.commits[0].sha == "abc123"
    assert "Signed-off-by" in context.commits[0].message

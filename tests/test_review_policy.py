"""Tests for deterministic PR review policy notes."""

from __future__ import annotations

from scripts.models import ChangedFile, PullRequestCommit, PullRequestContext
from scripts.review_policy import collect_review_policy_note, render_review_policy_note


def test_collect_review_policy_note_flags_maintainer_signals() -> None:
    context = PullRequestContext(
        repo="owner/repo",
        number=17,
        title="Fix security issue in command handling",
        body="Avoids CVE-2026-0001 style input.",
        base_sha="base123",
        head_sha="head456",
        author="alice",
        files=[
            ChangedFile(
                path="src/commands/get.json",
                status="modified",
                additions=2,
                deletions=1,
                patch="@@ -1 +1 @@\n-old\n+new",
                contents=None,
                is_binary=False,
            ),
            ChangedFile(
                path="src/cluster.c",
                status="modified",
                additions=2,
                deletions=1,
                patch="@@ -1 +1 @@\n-old\n+new",
                contents=None,
                is_binary=False,
            ),
        ],
        commits=[
            PullRequestCommit(sha="abc123def456", message="Fix command handling"),
            PullRequestCommit(
                sha="fff123def456",
                message="Add test\n\nSigned-off-by: Alice <alice@example.com>",
            ),
        ],
        base_ref="unstable",
    )

    note = collect_review_policy_note(context)
    rendered = render_review_policy_note(note)

    assert note.missing_dco_commits == ["abc123def456"]
    assert note.needs_core_team is True
    assert note.needs_docs is True
    assert note.security_sensitive is True
    assert note.needs_extra_tests is True
    assert note.suggested_labels == [
        "pending-missing-dco",
        "needs-doc-pr",
        "run-extra-tests",
    ]
    assert "abc123def456"[:12] in rendered
    assert "valkey-doc" in rendered
    assert "security-sensitive" in rendered
    assert "run-extra-tests" in rendered


def test_render_review_policy_note_all_clear() -> None:
    rendered = render_review_policy_note(
        collect_review_policy_note(
            PullRequestContext(
                repo="owner/repo",
                number=17,
                title="Refactor helper",
                body="",
                base_sha="base123",
                head_sha="head456",
                author="alice",
                files=[],
                commits=[],
            )
        )
    )

    assert "No deterministic maintainer-policy signals" in rendered

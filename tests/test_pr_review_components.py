"""Tests for reviewer Bedrock-backed components."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts.code_reviewer import CodeReviewer
from scripts.config import RetrievalConfig, ReviewerConfig
from scripts.models import (
    ChangedFile,
    DiffScope,
    PullRequestContext,
    ReviewThread,
)
from scripts.pr_summarizer import PRSummarizer
from scripts.review_chat import ReviewChat


def _context() -> PullRequestContext:
    return PullRequestContext(
        repo="owner/repo",
        number=17,
        title="Improve failover logic",
        body="This updates failover behavior.",
        base_sha="base123",
        head_sha="head456",
        author="alice",
        files=[
            ChangedFile(
                path="src/failover.c",
                status="modified",
                additions=8,
                deletions=2,
                patch="@@ -10,2 +10,8 @@\n-old\n+new",
                contents="int failover(void) { return 1; }",
                is_binary=False,
            )
        ],
    )


def test_pr_summarizer_uses_light_model() -> None:
    bedrock = MagicMock()
    bedrock.invoke.return_value = """
    {
      "walkthrough": "Updates failover handling.",
      "file_groups_markdown": "- Core: failover logic",
      "release_notes": "Improves failover handling."
    }
    """
    summarizer = PRSummarizer(bedrock)
    config = ReviewerConfig()

    result = summarizer.summarize(_context(), config)

    assert result.walkthrough == "Updates failover handling."
    kwargs = bedrock.invoke.call_args.kwargs
    assert kwargs["model_id"] == config.models.light_model_id


def test_code_reviewer_uses_heavy_model_and_filters_findings() -> None:
    bedrock = MagicMock()
    bedrock.invoke.return_value = """
    {
      "findings": [
        {
          "path": "src/failover.c",
          "line": 14,
          "severity": "high",
          "body": "This can leave failover state stale after timeout."
        },
        {
          "path": "README.md",
          "line": 1,
          "severity": "low",
          "body": "LGTM"
        }
      ]
    }
    """
    reviewer = CodeReviewer(bedrock)
    config = ReviewerConfig(max_review_comments=5)
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=_context().files,
        incremental=False,
    )

    findings = reviewer.review(_context(), scope, config)

    assert len(findings) == 1
    assert findings[0].path == "src/failover.c"
    kwargs = bedrock.invoke.call_args.kwargs
    assert kwargs["model_id"] == config.models.heavy_model_id


def test_code_reviewer_filters_speculative_and_file_level_findings() -> None:
    bedrock = MagicMock()
    bedrock.invoke.return_value = """
    {
      "findings": [
        {
          "path": "src/failover.c",
          "line": 14,
          "severity": "high",
          "body": "This can leave failover state stale after timeout."
        },
        {
          "path": "src/failover.c",
          "line": null,
          "severity": "medium",
          "body": "The workflow file looks truncated in the review."
        },
        {
          "path": "src/failover.c",
          "line": 15,
          "severity": "medium",
          "body": "There is no evidence this field exists in another model. Verify that definition."
        },
        {
          "path": "src/failover.c",
          "line": 16,
          "severity": "medium",
          "body": "If `_helper` returns a sentinel object, this appends it twice."
        }
      ]
    }
    """
    reviewer = CodeReviewer(bedrock)
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=_context().files,
        incremental=False,
    )

    findings = reviewer.review(_context(), scope, ReviewerConfig(max_review_comments=5))

    assert [(finding.path, finding.line, finding.body) for finding in findings] == [
        (
            "src/failover.c",
            10,
            "This can leave failover state stale after timeout.",
        )
    ]
    user_prompt = bedrock.invoke.call_args[0][1]
    assert "patch/content may be truncated" in user_prompt
    assert "Do not report that a file, diff, or workflow looks truncated." in user_prompt


def test_review_chat_uses_heavy_model() -> None:
    bedrock = MagicMock()
    bedrock.invoke.return_value = "Add a targeted failover timeout regression test."
    chat = ReviewChat(bedrock)
    config = ReviewerConfig()

    reply = chat.reply(
        _context(),
        ReviewThread(
            comment_id=1,
            path="src/failover.c",
            line=14,
            conversation=["Can you suggest a test?"],
        ),
        "/reviewbot can you suggest a test?",
        config,
    )

    assert "targeted failover timeout regression test" in reply
    kwargs = bedrock.invoke.call_args.kwargs
    assert kwargs["model_id"] == config.models.heavy_model_id


def test_code_reviewer_raises_on_unparseable_response() -> None:
    bedrock = MagicMock()
    bedrock.invoke.return_value = "not json"
    reviewer = CodeReviewer(bedrock)
    config = ReviewerConfig(max_review_comments=5)
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=_context().files,
        incremental=False,
    )

    with pytest.raises(ValueError, match="Unparseable review response"):
        reviewer.review(_context(), scope, config)


def test_pr_summarizer_includes_retrieved_context() -> None:
    bedrock = MagicMock()
    bedrock.invoke.return_value = """
    {
      "walkthrough": "Updates failover handling.",
      "file_groups_markdown": "- Core: failover logic",
      "release_notes": "Improves failover handling."
    }
    """
    retriever = MagicMock()
    retriever.render_for_prompt.return_value = "## Retrieved Valkey Context\nsentinel docs"
    summarizer = PRSummarizer(
        bedrock,
        retriever=retriever,
        retrieval_config=RetrievalConfig(enabled=True, docs_knowledge_base_id="DOCSKB"),
    )

    summarizer.summarize(_context(), ReviewerConfig())

    user_prompt = bedrock.invoke.call_args[0][1]
    assert "Retrieved Valkey Context" in user_prompt
    assert "sentinel docs" in user_prompt


def test_code_reviewer_includes_retrieved_context() -> None:
    bedrock = MagicMock()
    bedrock.invoke.return_value = '{"findings":[]}'
    retriever = MagicMock()
    retriever.render_for_prompt.return_value = "## Retrieved Valkey Context\nserver notes"
    reviewer = CodeReviewer(
        bedrock,
        retriever=retriever,
        retrieval_config=RetrievalConfig(enabled=True, code_knowledge_base_id="CODEKB"),
    )
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=_context().files,
        incremental=False,
    )

    reviewer.review(_context(), scope, ReviewerConfig(max_review_comments=5))

    user_prompt = bedrock.invoke.call_args[0][1]
    assert "Retrieved Valkey Context" in user_prompt
    assert "server notes" in user_prompt


def test_review_chat_includes_retrieved_context() -> None:
    bedrock = MagicMock()
    bedrock.invoke.return_value = "Answer"
    retriever = MagicMock()
    retriever.render_for_prompt.return_value = "## Retrieved Valkey Context\nthread notes"
    chat = ReviewChat(
        bedrock,
        retriever=retriever,
        retrieval_config=RetrievalConfig(enabled=True, docs_knowledge_base_id="DOCSKB"),
    )

    chat.reply(
        _context(),
        ReviewThread(
            comment_id=1,
            path="src/failover.c",
            line=14,
            conversation=["Can you suggest a test?"],
        ),
        "/reviewbot can you suggest a test?",
        ReviewerConfig(),
    )

    user_prompt = bedrock.invoke.call_args[0][1]
    assert "Retrieved Valkey Context" in user_prompt
    assert "thread notes" in user_prompt

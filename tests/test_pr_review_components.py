"""Tests for reviewer Bedrock-backed components."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from scripts.bedrock_client import BedrockClient
from scripts.code_reviewer import (
    CodeReviewer,
    ReviewCoverage,
    ReviewToolHandler,
    _agentic_review_budgets,
)
from scripts.config import BotConfig, ProjectContext, RetrievalConfig, ReviewerConfig
from scripts.models import (
    ChangedFile,
    DiffScope,
    ExistingReviewComment,
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
    bedrock.invoke.side_effect = [
        """
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
    """,
        '{"results": [{"index": 0, "verdict": "keep", "reason": "valid"}]}',
    ]
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
    # First invoke call is the review pass
    kwargs = bedrock.invoke.call_args_list[0].kwargs
    assert kwargs["model_id"] == config.models.heavy_model_id


def test_code_reviewer_triage_keeps_code_changes_by_default() -> None:
    bedrock = MagicMock()
    bedrock.invoke.return_value = "[TRIAGE]: APPROVED"
    changed_file = ChangedFile(
        path="src/failover.c",
        status="modified",
        additions=1,
        deletions=1,
        patch="@@ -1 +1 @@\n-int x = 0;\n+int x = 1;",
        contents="int x = 1;",
        is_binary=False,
    )

    verdict = CodeReviewer(bedrock).triage_file(
        changed_file,
        _context(),
        ReviewerConfig(),
    )

    assert verdict == "NEEDS_REVIEW"
    bedrock.invoke.assert_not_called()


def test_code_reviewer_triage_can_use_model_when_enabled() -> None:
    bedrock = MagicMock()
    bedrock.invoke.return_value = "[TRIAGE]: APPROVED"
    changed_file = ChangedFile(
        path="src/failover.c",
        status="modified",
        additions=1,
        deletions=1,
        patch="@@ -1 +1 @@\n-int x = 0;\n+int x = 1;",
        contents="int x = 1;",
        is_binary=False,
    )

    verdict = CodeReviewer(bedrock).triage_file(
        changed_file,
        _context(),
        ReviewerConfig(model_file_triage=True),
    )

    assert verdict == "APPROVED"
    bedrock.invoke.assert_called_once()


def test_code_reviewer_triage_skips_comment_only_code_changes() -> None:
    bedrock = MagicMock()
    changed_file = ChangedFile(
        path="src/failover.c",
        status="modified",
        additions=1,
        deletions=1,
        patch="@@ -1 +1 @@\n-// old comment\n+// new comment",
        contents="// new comment",
        is_binary=False,
    )

    verdict = CodeReviewer(bedrock).triage_file(
        changed_file,
        _context(),
        ReviewerConfig(),
    )

    assert verdict == "APPROVED"
    bedrock.invoke.assert_not_called()


def test_code_reviewer_triage_reviews_c_preprocessor_changes() -> None:
    bedrock = MagicMock()
    changed_file = ChangedFile(
        path="src/failover.c",
        status="modified",
        additions=1,
        deletions=1,
        patch='@@ -1 +1 @@\n-#include "old.h"\n+#include "new.h"',
        contents='#include "new.h"',
        is_binary=False,
    )

    verdict = CodeReviewer(bedrock).triage_file(
        changed_file,
        _context(),
        ReviewerConfig(),
    )

    assert verdict == "NEEDS_REVIEW"


def test_code_reviewer_filters_speculative_and_file_level_findings() -> None:
    bedrock = MagicMock()
    # First call: review pass returns findings (some speculative).
    # Second call: verification pass — return keep-all so the test focuses on
    # the speculative filter, not the verification logic.
    bedrock.invoke.side_effect = [
        """
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
    """,
        '{"results": [{"index": 0, "verdict": "keep", "reason": "valid"}]}',
    ]
    reviewer = CodeReviewer(bedrock)
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=_context().files,
        incremental=False,
    )

    findings = reviewer.review(_context(), scope, ReviewerConfig(max_review_comments=5))

    assert len(findings) == 1
    assert findings[0].path == "src/failover.c"
    assert findings[0].line == 10
    assert "stale after timeout" in findings[0].body
    assert "Confidence:" in findings[0].body
    # The first invoke call is the review pass
    review_call_args = bedrock.invoke.call_args_list[0]
    user_prompt = review_call_args[0][1]
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


def test_review_tool_handler_search_code_verifies_hits_at_head_sha() -> None:
    gh = MagicMock()
    repo = MagicMock()
    gh.get_repo.return_value = repo

    search_result = MagicMock()
    search_result.path = "src/failover.c"
    gh.search_code.return_value = [search_result]
    repo.get_contents.return_value = MagicMock(
        decoded_content=(
            b"int failover_timeout(void) {\n    return 1;\n}\n"
        ),
    )

    handler = ReviewToolHandler(gh, "owner/repo", "head456")
    result = handler.execute("search_code", {"query": "failover_timeout"})

    assert "Found 1 verified result(s)" in result
    assert "src/failover.c" in result
    repo.get_contents.assert_called_with("src/failover.c", ref="head456")


def test_review_tool_handler_search_code_drops_unverified_hits() -> None:
    gh = MagicMock()
    repo = MagicMock()
    gh.get_repo.return_value = repo

    search_result = MagicMock()
    search_result.path = "src/failover.c"
    gh.search_code.return_value = [search_result]
    repo.get_contents.return_value = MagicMock(
        decoded_content=b"int unrelated(void) { return 0; }\n",
    )

    handler = ReviewToolHandler(gh, "owner/repo", "head456")
    result = handler.execute("search_code", {"query": "failover_timeout"})

    assert "No results found for 'failover_timeout'" in result


def test_review_tool_handler_search_code_skips_language_qualifier_for_headers() -> None:
    gh = MagicMock()
    repo = MagicMock()
    gh.get_repo.return_value = repo

    search_result = MagicMock()
    search_result.path = "src/failover.h"
    gh.search_code.return_value = [search_result]
    repo.get_contents.return_value = MagicMock(
        decoded_content=b"int failover_timeout;\n",
    )

    handler = ReviewToolHandler(gh, "owner/repo", "head456")
    result = handler.execute(
        "search_code",
        {"query": "failover_timeout", "path_filter": ".h"},
    )

    assert "Found 1 verified result(s)" in result
    gh.search_code.assert_called_once_with("failover_timeout repo:owner/repo")


def test_review_tool_handler_search_code_uses_language_qualifier_for_c_files() -> None:
    gh = MagicMock()
    repo = MagicMock()
    gh.get_repo.return_value = repo

    search_result = MagicMock()
    search_result.path = "src/failover.c"
    gh.search_code.return_value = [search_result]
    repo.get_contents.return_value = MagicMock(
        decoded_content=b"int failover_timeout(void) { return 1; }\n",
    )

    handler = ReviewToolHandler(gh, "owner/repo", "head456")
    result = handler.execute(
        "search_code",
        {"query": "failover_timeout", "path_filter": ".c"},
    )

    assert "Found 1 verified result(s)" in result
    gh.search_code.assert_called_once_with(
        "failover_timeout repo:owner/repo language:c",
    )


def test_review_tool_handler_search_code_prefers_local_head_content() -> None:
    gh = MagicMock()

    handler = ReviewToolHandler(
        gh,
        "owner/repo",
        "head456",
        head_file_texts={
            "src/failover.c": "int failover_timeout(void) { return 1; }\n",
        },
    )
    result = handler.execute("search_code", {"query": "failover_timeout"})

    assert "Found 1 local result(s)" in result
    assert "src/failover.c" in result
    gh.search_code.assert_not_called()


def test_review_tool_handler_disables_github_search_for_fork_after_repeated_misses() -> None:
    gh = MagicMock()
    repo = MagicMock()
    repo.fork = True
    gh.get_repo.return_value = repo
    gh.search_code.return_value = []
    shared_search_state: dict[str, object] = {}

    handler = ReviewToolHandler(
        gh,
        "owner/repo",
        "head456",
        shared_search_state=shared_search_state,
    )

    assert "No results found" in handler.execute("search_code", {"query": "alpha"})
    assert "No results found" in handler.execute("search_code", {"query": "beta"})
    assert "No results found" in handler.execute("search_code", {"query": "gamma"})

    second_handler = ReviewToolHandler(
        gh,
        "owner/repo",
        "head456",
        shared_search_state=shared_search_state,
    )
    disabled = second_handler.execute("search_code", {"query": "delta"})

    assert "GitHub code search appears unavailable" in disabled
    assert gh.search_code.call_count == 3


def test_review_tool_handler_get_base_file_uses_base_sha() -> None:
    gh = MagicMock()
    repo = MagicMock()
    gh.get_repo.return_value = repo
    repo.get_contents.return_value = MagicMock(
        decoded_content=b"int failover(void) { return 0; }\n",
    )

    handler = ReviewToolHandler(gh, "owner/repo", "head456", base_sha="base123")
    result = handler.execute("get_base_file", {"path": "src/failover.c"})

    assert "return 0" in result
    repo.get_contents.assert_called_with("src/failover.c", ref="base123")


def test_review_tool_handler_find_tests_for_path_uses_project_patterns() -> None:
    gh = MagicMock()
    repo = MagicMock()
    gh.get_repo.return_value = repo

    tests_dir = MagicMock()
    tests_dir.path = "tests/unit"
    tests_dir.type = "dir"
    predicted = MagicMock()
    predicted.decoded_content = b"test failover timeout"

    def get_contents(path: str, ref: str | None = None):
        if path == "tests/":
            return [tests_dir]
        if path == "tests/unit":
            return []
        if path == "tests/unit/failover_timeout.tcl":
            return predicted
        raise FileNotFoundError(path)

    repo.get_contents.side_effect = get_contents

    handler = ReviewToolHandler(
        gh,
        "owner/repo",
        "head456",
        project=ProjectContext(
            source_dirs=["src/"],
            test_dirs=["tests/"],
            test_to_source_patterns=[
                {
                    "source_path": "src/{name}.c",
                    "test_path": "tests/unit/{name}.tcl",
                }
            ],
        ),
    )
    result = handler.execute("find_tests_for_path", {"path": "src/failover_timeout.c"})

    assert "tests/unit/failover_timeout.tcl" in result


def test_review_tool_handler_tracks_checked_paths_and_fetch_limit() -> None:
    gh = MagicMock()
    repo = MagicMock()
    gh.get_repo.return_value = repo
    repo.get_contents.return_value = MagicMock(
        decoded_content=b"int failover(void) { return 0; }\n",
    )

    handler = ReviewToolHandler(
        gh,
        "owner/repo",
        "head456",
        max_fetches=1,
    )
    first = handler.execute("get_file", {"path": "src/failover.c"})
    second = handler.execute("get_file", {"path": "src/other.c"})

    assert "return 0" in first
    assert "Fetch limit reached (1)" in second
    assert handler.checked_paths() == ["src/failover.c"]
    assert handler.fetch_limit_hit is True


def test_review_tool_handler_reuses_shared_cache_across_handlers() -> None:
    gh = MagicMock()
    repo = MagicMock()
    gh.get_repo.return_value = repo
    repo.get_contents.return_value = MagicMock(
        decoded_content=b"int failover(void) { return 0; }\n",
    )
    shared_cache: dict[str, str] = {}
    shared_repo_holder: dict[str, object] = {}

    first_handler = ReviewToolHandler(
        gh,
        "owner/repo",
        "head456",
        shared_cache=shared_cache,
        shared_repo_holder=shared_repo_holder,
    )
    second_handler = ReviewToolHandler(
        gh,
        "owner/repo",
        "head456",
        shared_cache=shared_cache,
        shared_repo_holder=shared_repo_holder,
    )

    assert "return 0" in first_handler.execute("get_file", {"path": "src/failover.c"})
    assert "return 0" in second_handler.execute("get_file", {"path": "src/failover.c"})
    assert second_handler.inspected_file_paths() == ["src/failover.c"]
    assert "src/failover.c" in second_handler.render_context()
    gh.get_repo.assert_called_once_with("owner/repo")
    repo.get_contents.assert_called_once_with("src/failover.c", ref="head456")


def test_review_tool_handler_cached_base_file_counts_as_current_inspection() -> None:
    gh = MagicMock()
    repo = MagicMock()
    gh.get_repo.return_value = repo
    repo.get_contents.return_value = MagicMock(
        decoded_content=b"int failover(void) { return 0; }\n",
    )
    shared_cache: dict[str, str] = {}
    shared_repo_holder: dict[str, object] = {}

    first_handler = ReviewToolHandler(
        gh,
        "owner/repo",
        "head456",
        base_sha="base123",
        shared_cache=shared_cache,
        shared_repo_holder=shared_repo_holder,
    )
    second_handler = ReviewToolHandler(
        gh,
        "owner/repo",
        "head456",
        base_sha="base123",
        shared_cache=shared_cache,
        shared_repo_holder=shared_repo_holder,
    )

    assert "return 0" in first_handler.execute("get_base_file", {"path": "src/failover.c"})
    assert "return 0" in second_handler.execute("get_base_file", {"path": "src/failover.c"})
    assert second_handler.inspected_file_paths() == ["src/failover.c"]
    assert "src/failover.c" in second_handler.render_context()
    gh.get_repo.assert_called_once_with("owner/repo")
    repo.get_contents.assert_called_once_with("src/failover.c", ref="base123")


def test_review_tool_handler_requires_explicit_file_fetch_before_submit() -> None:
    gh = MagicMock()
    repo = MagicMock()
    gh.get_repo.return_value = repo

    def get_contents(path: str, ref: str | None = None):
        if path == "src/failover.c":
            return MagicMock(decoded_content=b"int failover(void) { return 0; }\n")
        if path == "tests/failover_timeout.tcl":
            return MagicMock(decoded_content=b"test failover timeout {}\n")
        raise FileNotFoundError(path)

    repo.get_contents.side_effect = get_contents
    required_files = [
        ChangedFile(
            path="src/failover.c",
            status="modified",
            additions=3,
            deletions=1,
            patch="@@ -1 +1 @@\n-old\n+new",
            contents="int failover(void) { return 0; }",
            is_binary=False,
        ),
        ChangedFile(
            path="tests/failover_timeout.tcl",
            status="modified",
            additions=2,
            deletions=0,
            patch="@@ -1 +1 @@\n-old\n+new",
            contents="test failover timeout {}",
            is_binary=False,
        ),
    ]

    handler = ReviewToolHandler(
        gh,
        "owner/repo",
        "head456",
        required_files=required_files,
    )
    handler.execute("get_file", {"path": "src/failover.c"})

    accepted, message = handler.validate_terminal_tool(
        "submit_review",
        {
            "reviews": [],
            "lgtm": True,
            "checked_files": ["src/failover.c"],
            "skipped_files": [{
                "path": "tests/failover_timeout.tcl",
                "reason": "covered by the implementation review",
            }],
        },
    )

    assert accepted is False
    assert "tests/failover_timeout.tcl" in message
    assert "must be fetched explicitly" in message

    handler.execute("get_file", {"path": "tests/failover_timeout.tcl"})
    accepted, message = handler.validate_terminal_tool(
        "submit_review",
        {
            "reviews": [],
            "lgtm": True,
            "checked_files": ["src/failover.c", "tests/failover_timeout.tcl"],
            "skipped_files": [],
        },
    )

    assert accepted is True
    assert message == "Review submitted."


def test_review_tool_handler_prioritizes_related_file_fetches_over_search_misses() -> None:
    gh = MagicMock()
    repo = MagicMock()
    gh.get_repo.return_value = repo

    def get_contents(path: str, ref: str | None = None):
        if path == "src/failover_timeout.c":
            return MagicMock(decoded_content=b"int failover_timeout(void) { return 0; }\n")
        if path == "tests/failover_timeout.tcl":
            return MagicMock(decoded_content=b"test failover timeout {}\n")
        raise FileNotFoundError(path)

    repo.get_contents.side_effect = get_contents
    required_files = [
        ChangedFile(
            path="src/failover_timeout.c",
            status="modified",
            additions=3,
            deletions=1,
            patch='@@ -1 +1 @@\n-#include "old.h"\n+#include "new.h"',
            contents='int failover_timeout(void) { return 0; }',
            is_binary=False,
        ),
    ]
    handler = ReviewToolHandler(
        gh,
        "owner/repo",
        "head456",
        required_files=required_files,
        suggested_support_paths=["tests/failover_timeout.tcl"],
    )

    first = handler.execute("search_code", {"query": "failover_timeout"})
    assert "Inspect the required changed file" in first
    gh.search_code.assert_not_called()

    handler.execute("get_file", {"path": "src/failover_timeout.c"})
    second = handler.execute("search_code", {"query": "failover_timeout"})
    assert "inspect at least one related changed file/test" in second
    assert "tests/failover_timeout.tcl" in second
    gh.search_code.assert_not_called()

    handler.execute("get_file", {"path": "tests/failover_timeout.tcl"})
    gh.search_code.return_value = []
    miss = handler.execute("search_code", {"query": "missing_symbol"})
    repeat = handler.execute("search_code", {"query": "missing_symbol"})
    assert "No results found" in miss
    assert "already returned no results" in repeat
    gh.search_code.assert_called_once()


def test_review_tool_handler_blocks_symbol_rephrasing_after_variant_searches() -> None:
    gh = MagicMock()
    handler = ReviewToolHandler(
        gh,
        "owner/repo",
        "head456",
        head_file_texts={
            "src/cluster.h": "void resetClusterStats(void);\n",
        },
    )

    first = handler.execute("search_code", {"query": "resetClusterStats"})
    second = handler.execute("search_code", {"query": "void resetClusterStats"})
    third = handler.execute("search_code", {"query": "resetClusterStats(void)"})

    assert "Found 1 local result(s)" in first
    assert "Found 1 local result(s)" in second
    assert "already searched this symbol several times" in third
    gh.search_code.assert_not_called()


def test_review_coverage_note_lists_gaps() -> None:
    coverage = ReviewCoverage(
        requested_lgtm=False,
        checked_files=["src/failover.c"],
        skipped_files=[("src/failover.h", "covered via implementation search")],
        claimed_without_tool=["src/socket.c"],
        unaccounted_files=["tests/failover_timeout.tcl"],
        fetch_limit_hit=True,
    )

    note = coverage.render_review_note()

    assert "withheld LGTM" in note
    assert "`src/failover.c`" in note
    assert "`src/failover.h`: covered via implementation search" in note
    assert "`src/socket.c`" in note
    assert "`tests/failover_timeout.tcl`" in note
    assert "fetch budget" in note
    assert coverage.complete is False
    assert coverage.approvable is False


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


def test_code_reviewer_verifier_can_drop_candidate_findings() -> None:
    bedrock = MagicMock()
    bedrock.invoke.side_effect = [
        """
    {
      "findings": [
        {
          "path": "src/failover.c",
          "line": 14,
          "severity": "high",
          "confidence": "high",
          "title": "Potential stale state",
          "trigger": "a timeout fires before cleanup",
          "impact": "leave stale failover state behind",
          "body": "The timeout path updates the timer but not the state field."
        }
      ]
    }
    """,
        '{"results": [{"index": 0, "verdict": "drop", "reason": "not well supported"}]}',
    ]
    reviewer = CodeReviewer(bedrock)
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=_context().files,
        incremental=False,
    )

    findings = reviewer.review(_context(), scope, ReviewerConfig(max_review_comments=5))

    assert findings == []


def test_code_reviewer_logs_verifier_drop_reason(caplog: pytest.LogCaptureFixture) -> None:
    bedrock = MagicMock()
    bedrock.invoke.side_effect = [
        """
    {
      "findings": [
        {
          "path": "src/failover.c",
          "line": 14,
          "severity": "high",
          "confidence": "high",
          "title": "Potential stale state",
          "trigger": "a timeout fires before cleanup",
          "impact": "leave stale failover state behind",
          "body": "The timeout path updates the timer but not the state field."
        }
      ]
    }
    """,
        '{"results": [{"index": 0, "verdict": "drop", "reason": "not well supported"}]}',
    ]
    reviewer = CodeReviewer(bedrock)
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=_context().files,
        incremental=False,
    )

    with caplog.at_level(logging.INFO):
        findings = reviewer.review(_context(), scope, ReviewerConfig(max_review_comments=5))

    assert findings == []
    assert "Verifier dropped candidate 0" in caplog.text
    assert "not well supported" in caplog.text


def test_code_reviewer_ranks_by_severity_and_confidence_before_capping() -> None:
    bedrock = MagicMock()
    bedrock.invoke.side_effect = [
        """
    {
      "findings": [
        {
          "path": "src/failover.c",
          "line": 14,
          "severity": "medium",
          "confidence": "low",
          "title": "Lower-priority issue",
          "trigger": "the timeout path runs",
          "impact": "log stale telemetry",
          "body": "This only affects logging."
        },
        {
          "path": "src/failover.c",
          "line": 13,
          "severity": "high",
          "confidence": "high",
          "title": "Top issue",
          "trigger": "the timeout path runs",
          "impact": "leave stale failover state behind",
          "body": "State is never reset."
        }
      ]
    }
    """,
        """
    {
      "results": [
        {"index": 0, "verdict": "keep", "reason": "valid"},
        {"index": 1, "verdict": "keep", "reason": "valid"}
      ]
    }
    """,
    ]
    reviewer = CodeReviewer(bedrock)
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=_context().files,
        incremental=False,
    )

    findings = reviewer.review(_context(), scope, ReviewerConfig(max_review_comments=1))

    assert len(findings) == 1
    assert findings[0].title == "Top issue"


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

    # No findings means no verification call; the only invoke is the review pass
    user_prompt = bedrock.invoke.call_args[0][1]
    assert "Retrieved Valkey Context" in user_prompt
    assert "server notes" in user_prompt


def test_code_reviewer_includes_existing_review_discussion_in_prompts() -> None:
    runtime_client = MagicMock()
    bedrock = BedrockClient(BotConfig(), client=runtime_client)
    bedrock.converse_with_tools = MagicMock(
        return_value='{"reviews":[],"lgtm":true,"checked_files":["src/failover_timeout.c"],"skipped_files":[]}'
    )
    reviewer = CodeReviewer(bedrock, github_client=MagicMock())
    files = [
        ChangedFile(
            path="src/failover_timeout.c",
            status="modified",
            additions=5,
            deletions=1,
            patch="@@ -1 +1 @@\n-old\n+new",
            contents='int failover_timeout(void) { return 1; }',
            is_binary=False,
        ),
    ]
    context = PullRequestContext(
        repo="owner/repo",
        number=17,
        title="Improve failover logic",
        body="This updates failover behavior.",
        base_sha="base123",
        head_sha="head456",
        author="alice",
        files=files,
        review_comments=[
            ExistingReviewComment(
                path="src/failover_timeout.c",
                line=14,
                author="bob",
                body="This already looks risky when the timeout path retries twice.",
            ),
        ],
    )
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=files,
        incremental=False,
    )

    reviewer.review(context, scope, ReviewerConfig(max_review_comments=5))

    user_prompt = bedrock.converse_with_tools.call_args.args[1]
    assert "Existing review discussion already on this scope" in user_prompt
    assert "bob" in user_prompt
    assert "already looks risky" in user_prompt


def test_code_reviewer_groups_related_files_into_single_agentic_pass() -> None:
    runtime_client = MagicMock()
    bedrock = BedrockClient(BotConfig(), client=runtime_client)
    bedrock.converse_with_tools = MagicMock(
        return_value='{"reviews":[],"lgtm":true,"checked_files":["src/failover_timeout.c","tests/failover_timeout.tcl"],"skipped_files":[]}'
    )
    reviewer = CodeReviewer(bedrock, github_client=MagicMock())
    files = [
        ChangedFile(
            path="src/failover_timeout.c",
            status="modified",
            additions=5,
            deletions=1,
            patch="@@ -1 +1 @@\n-old\n+new",
            contents='''#include "failover_timeout.h"\nint failover_timeout(void) { return 1; }''',
            is_binary=False,
        ),
        ChangedFile(
            path="tests/failover_timeout.tcl",
            status="modified",
            additions=2,
            deletions=0,
            patch="@@ -1 +1 @@\n-old\n+new",
            contents="test failover timeout {}",
            is_binary=False,
        ),
    ]
    context = PullRequestContext(
        repo="owner/repo",
        number=17,
        title="Improve failover logic",
        body="This updates failover behavior.",
        base_sha="base123",
        head_sha="head456",
        author="alice",
        files=files,
    )
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=files,
        incremental=False,
    )

    reviewer.review(context, scope, ReviewerConfig(max_review_comments=5))

    assert bedrock.converse_with_tools.call_count == 1
    prompt = bedrock.converse_with_tools.call_args.args[1]
    assert "src/failover_timeout.c" in prompt
    assert "tests/failover_timeout.tcl" in prompt
    assert "Suggested related changed files/tests to inspect early" not in prompt


def test_agentic_review_budgets_allow_100_fetches_and_turns() -> None:
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=_context().files,
        incremental=False,
    )

    assert _agentic_review_budgets(scope) == (100, 100)


def test_code_reviewer_reuses_shared_tool_state_across_focused_agentic_passes() -> None:
    runtime_client = MagicMock()
    bedrock = BedrockClient(BotConfig(), client=runtime_client)
    bedrock.converse_with_tools = MagicMock(side_effect=[
        '{"reviews":[],"lgtm":true,"checked_files":["src/failover_timeout.c","tests/failover_timeout.tcl"],"skipped_files":[]}',
        '{"reviews":[],"lgtm":true,"checked_files":["docs/overview.md"],"skipped_files":[]}',
    ])
    reviewer = CodeReviewer(bedrock, github_client=MagicMock())
    files = [
        ChangedFile(
            path="src/failover_timeout.c",
            status="modified",
            additions=5,
            deletions=1,
            patch="@@ -1 +1 @@\n-old\n+new",
            contents='''#include "failover_timeout.h"\nint failover_timeout(void) { return 1; }''',
            is_binary=False,
        ),
        ChangedFile(
            path="tests/failover_timeout.tcl",
            status="modified",
            additions=2,
            deletions=0,
            patch="@@ -1 +1 @@\n-old\n+new",
            contents="test failover timeout {}",
            is_binary=False,
        ),
        ChangedFile(
            path="docs/overview.md",
            status="modified",
            additions=4,
            deletions=1,
            patch="@@ -1 +1 @@\n-old\n+new",
            contents="Updated operational overview.",
            is_binary=False,
        ),
    ]
    context = PullRequestContext(
        repo="owner/repo",
        number=17,
        title="Improve failover logic",
        body="This updates failover behavior.",
        base_sha="base123",
        head_sha="head456",
        author="alice",
        files=files,
    )
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=files,
        incremental=False,
    )
    shared_cache_ids: list[int] = []
    shared_search_state_ids: list[int] = []

    class _FakeHandler:
        def __init__(self, *args, required_files=None, shared_cache=None, shared_search_state=None, **kwargs):
            shared_cache_ids.append(id(shared_cache))
            shared_search_state_ids.append(id(shared_search_state))
            assert shared_cache is not None
            assert shared_search_state is not None
            assert required_files is not None
            self._path = required_files[0].path
            self.fetch_limit_hit = False

        def validate_terminal_tool(self, tool_name: str, tool_input: dict) -> tuple[bool, str]:
            return True, "Review submitted."

        def execute(self, tool_name: str, tool_input: dict) -> str:
            return ""

        def inspected_file_paths(self) -> list[str]:
            return [self._path]

        def checked_paths(self) -> list[str]:
            return [self._path]

        def render_context(self, *, max_chars: int = 24_000) -> str:
            return ""

    with patch("scripts.code_reviewer.ReviewToolHandler", _FakeHandler):
        reviewer.review(context, scope, ReviewerConfig(max_review_comments=5))

    assert bedrock.converse_with_tools.call_count == 2
    assert len(set(shared_cache_ids)) == 1
    assert len(set(shared_search_state_ids)) == 1


def test_code_reviewer_handles_unparseable_agentic_submission() -> None:
    runtime_client = MagicMock()
    bedrock = BedrockClient(BotConfig(), client=runtime_client)
    bedrock.converse_with_tools = MagicMock(return_value="not json")
    reviewer = CodeReviewer(bedrock, github_client=MagicMock())
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=_context().files,
        incremental=False,
    )

    findings = reviewer.review(_context(), scope, ReviewerConfig(max_review_comments=5))

    assert findings == []
    coverage = reviewer.get_last_review_coverage()
    assert coverage is not None
    assert coverage.requested_lgtm is False


def test_code_reviewer_withholds_findings_when_agentic_review_fails() -> None:
    runtime_client = MagicMock()
    bedrock = BedrockClient(BotConfig(), client=runtime_client)
    bedrock.converse_with_tools = MagicMock(side_effect=RuntimeError("tool loop exhausted"))
    reviewer = CodeReviewer(bedrock, github_client=MagicMock())
    scope = DiffScope(
        base_sha="base123",
        head_sha="head456",
        files=_context().files,
        incremental=False,
    )

    findings = reviewer.review(_context(), scope, ReviewerConfig(max_review_comments=5))

    assert findings == []
    coverage = reviewer.get_last_review_coverage()
    assert coverage is not None
    assert coverage.requested_lgtm is False
    assert coverage.unaccounted_files == ["src/failover.c"]


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

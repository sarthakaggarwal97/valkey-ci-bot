"""Unit tests for scripts.conflict_resolver.ConflictResolver."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts.backport_models import (
    BackportConfig,
    BackportPRContext,
    ConflictedFile,
)
from scripts.conflict_resolver import ConflictResolver

# ── Fixtures ──────────────────────────────────────────────────────────


def _make_pr_context(**overrides: object) -> BackportPRContext:
    defaults = dict(
        source_pr_number=123,
        source_pr_title="Fix memory leak in dict.c",
        source_pr_body="This fixes a memory leak when resizing.",
        source_pr_url="https://github.com/valkey-io/valkey/pull/123",
        source_pr_diff="diff --git a/src/dict.c ...",
        target_branch="8.1",
        commits=["abc123"],
        repo_full_name="valkey-io/valkey",
    )
    defaults.update(overrides)
    return BackportPRContext(**defaults)  # type: ignore[arg-type]


def _make_conflict(
    path: str = "src/dict.c",
    content_with_markers: str | None = None,
    target_branch_content: str = "int main() { return 0; }",
    source_branch_content: str = "int main() { return 1; }",
) -> ConflictedFile:
    if content_with_markers is None:
        content_with_markers = (
            "<<<<<<< HEAD\n"
            "int main() { return 0; }\n"
            "=======\n"
            "int main() { return 1; }\n"
            ">>>>>>> abc123\n"
        )
    return ConflictedFile(
        path=path,
        content_with_markers=content_with_markers,
        target_branch_content=target_branch_content,
        source_branch_content=source_branch_content,
    )


# ── Tests: resolve_conflicts ──────────────────────────────────────────


class TestResolveConflicts:
    """Tests for ConflictResolver.resolve_conflicts."""

    def test_whitespace_only_conflict_resolved_without_llm(self) -> None:
        """Whitespace-only conflicts use target branch content, no LLM."""
        bedrock = MagicMock()
        config = BackportConfig()
        resolver = ConflictResolver(bedrock, config)

        conflict = _make_conflict(
            target_branch_content="int main() {\n  return 0;\n}\n",
            source_branch_content="int main() {\n    return 0;\n}\n",
        )
        results = resolver.resolve_conflicts(
            [conflict], _make_pr_context(), token_budget=100_000,
        )

        assert len(results) == 1
        assert results[0].resolved_content == conflict.target_branch_content
        assert results[0].resolution_summary == "whitespace-only"
        assert results[0].tokens_used == 0
        bedrock.invoke.assert_not_called()

    def test_skip_all_when_exceeds_max_conflicting_files(self) -> None:
        """All files skipped when count exceeds max_conflicting_files."""
        bedrock = MagicMock()
        config = BackportConfig(max_conflicting_files=2)
        resolver = ConflictResolver(bedrock, config)

        conflicts = [_make_conflict(path=f"file{i}.c") for i in range(3)]
        results = resolver.resolve_conflicts(
            conflicts, _make_pr_context(), token_budget=100_000,
        )

        assert len(results) == 3
        for r in results:
            assert r.resolved_content is None
            assert "exceeds limit" in r.resolution_summary
        bedrock.invoke.assert_not_called()

    def test_stop_when_token_budget_exhausted(self) -> None:
        """Processing stops when cumulative tokens exceed budget."""
        bedrock = MagicMock()
        # Return clean content (no markers, balanced braces).
        bedrock.invoke.return_value = "int main() { return 42; }"
        config = BackportConfig()
        resolver = ConflictResolver(bedrock, config)

        conflicts = [_make_conflict(path=f"file{i}.c") for i in range(3)]
        # Very small budget — first file will exhaust it.
        results = resolver.resolve_conflicts(
            conflicts, _make_pr_context(), token_budget=1,
        )

        assert len(results) == 3
        # First file resolved.
        assert results[0].resolved_content is not None
        # Remaining files skipped due to budget.
        assert results[1].resolved_content is None
        assert "token budget" in results[1].resolution_summary
        assert results[2].resolved_content is None

    def test_successful_llm_resolution(self) -> None:
        """LLM returns clean content on first attempt."""
        bedrock = MagicMock()
        bedrock.invoke.return_value = "int main() { return 42; }"
        config = BackportConfig()
        resolver = ConflictResolver(bedrock, config)

        results = resolver.resolve_conflicts(
            [_make_conflict()], _make_pr_context(), token_budget=100_000,
        )

        assert len(results) == 1
        assert results[0].resolved_content == "int main() { return 42; }"
        assert results[0].resolution_summary == "Resolved by LLM"
        assert results[0].attempts == 1
        assert bedrock.invoke.call_count == 1

    def test_one_result_per_file(self) -> None:
        """Returns exactly one ResolutionResult per input file."""
        bedrock = MagicMock()
        bedrock.invoke.return_value = "int x() { return 0; }"
        config = BackportConfig()
        resolver = ConflictResolver(bedrock, config)

        conflicts = [_make_conflict(path=f"f{i}.c") for i in range(5)]
        results = resolver.resolve_conflicts(
            conflicts, _make_pr_context(), token_budget=1_000_000,
        )

        assert len(results) == len(conflicts)
        assert [r.path for r in results] == [c.path for c in conflicts]


# ── Tests: _resolve_single_file ───────────────────────────────────────


class TestResolveSingleFile:
    """Tests for ConflictResolver._resolve_single_file."""

    def test_retry_on_remaining_markers(self) -> None:
        """Retries when first response still has conflict markers."""
        bedrock = MagicMock()
        # First call returns markers, second returns clean content.
        bedrock.invoke.side_effect = [
            "<<<<<<< HEAD\nstuff\n=======\nother\n>>>>>>> abc",
            "int main() { return 0; }",
        ]
        config = BackportConfig(max_conflict_retries=2)
        resolver = ConflictResolver(bedrock, config)

        result = resolver._resolve_single_file(
            _make_conflict(), _make_pr_context(), max_retries=2,
        )

        assert result.resolved_content == "int main() { return 0; }"
        assert result.attempts == 2
        assert bedrock.invoke.call_count == 2

    def test_exhausted_retries_leaves_unresolved(self) -> None:
        """File left unresolved after all retries fail."""
        bedrock = MagicMock()
        bedrock.invoke.return_value = (
            "<<<<<<< HEAD\nstuff\n=======\nother\n>>>>>>> abc"
        )
        config = BackportConfig(max_conflict_retries=1)
        resolver = ConflictResolver(bedrock, config)

        result = resolver._resolve_single_file(
            _make_conflict(), _make_pr_context(), max_retries=1,
        )

        assert result.resolved_content is None
        assert "failed to remove conflict markers" in result.resolution_summary
        assert result.attempts == 2  # 1 initial + 1 retry
        assert bedrock.invoke.call_count == 2

    def test_syntax_validation_failure_leaves_unresolved(self) -> None:
        """File left unresolved when C syntax check fails."""
        bedrock = MagicMock()
        # Unbalanced braces — no conflict markers but bad syntax.
        bedrock.invoke.return_value = "int main() { return 0; "
        config = BackportConfig()
        resolver = ConflictResolver(bedrock, config)

        result = resolver._resolve_single_file(
            _make_conflict(), _make_pr_context(), max_retries=0,
        )

        assert result.resolved_content is None
        assert "C syntax validation" in result.resolution_summary

    def test_non_c_files_do_not_use_c_brace_validation(self) -> None:
        """Markdown files should not be rejected for unmatched curly braces."""
        bedrock = MagicMock()
        bedrock.invoke.return_value = "Use {placeholder in the docs.\n"
        config = BackportConfig()
        resolver = ConflictResolver(bedrock, config)

        result = resolver._resolve_single_file(
            _make_conflict(path="docs/guide.md"),
            _make_pr_context(),
            max_retries=0,
        )

        assert result.resolved_content == "Use {placeholder in the docs."
        assert result.resolution_summary == "Resolved by LLM"

    def test_yaml_validation_failure_leaves_unresolved(self) -> None:
        """YAML files should be validated with a YAML parser."""
        bedrock = MagicMock()
        bedrock.invoke.return_value = "steps: [unterminated"
        config = BackportConfig()
        resolver = ConflictResolver(bedrock, config)

        result = resolver._resolve_single_file(
            _make_conflict(path=".github/workflows/test.yml"),
            _make_pr_context(),
            max_retries=0,
        )

        assert result.resolved_content is None
        assert "YAML syntax validation" in result.resolution_summary

    def test_bedrock_exception_handled(self) -> None:
        """Bedrock errors are caught and file is left unresolved."""
        bedrock = MagicMock()
        bedrock.invoke.side_effect = RuntimeError("Bedrock unavailable")
        config = BackportConfig()
        resolver = ConflictResolver(bedrock, config)

        result = resolver._resolve_single_file(
            _make_conflict(), _make_pr_context(), max_retries=0,
        )

        assert result.resolved_content is None
        assert result.tokens_used > 0


# ── Tests: _build_prompt ──────────────────────────────────────────────


class TestBuildPrompt:
    """Tests for ConflictResolver._build_prompt."""

    def test_prompt_contains_all_required_context(self) -> None:
        """Prompt includes PR title, body, diff, file content, and versions."""
        bedrock = MagicMock()
        config = BackportConfig()
        resolver = ConflictResolver(bedrock, config)

        ctx = _make_pr_context()
        conflict = _make_conflict()
        system, user = resolver._build_prompt(conflict, ctx)

        assert "Valkey" in system
        assert ctx.source_pr_title in user
        assert ctx.source_pr_body in user
        assert ctx.source_pr_diff in user
        assert conflict.content_with_markers in user
        assert conflict.target_branch_content in user
        assert conflict.source_branch_content in user
        assert conflict.path in user

    def test_retry_prompt_includes_feedback(self) -> None:
        """On retry, feedback about remaining markers is appended."""
        bedrock = MagicMock()
        config = BackportConfig()
        resolver = ConflictResolver(bedrock, config)

        _, user = resolver._build_prompt(
            _make_conflict(),
            _make_pr_context(),
            feedback="Still has markers!",
        )

        assert "Still has markers!" in user

    def test_no_feedback_on_first_attempt(self) -> None:
        """First attempt has no feedback section."""
        bedrock = MagicMock()
        config = BackportConfig()
        resolver = ConflictResolver(bedrock, config)

        _, user = resolver._build_prompt(
            _make_conflict(), _make_pr_context(),
        )

        assert "Feedback" not in user


# ---------------------------------------------------------------------------
# Property-Based Tests
# ---------------------------------------------------------------------------

from hypothesis import given, settings
from hypothesis import strategies as st

# -- Strategies for generating test data --

_safe_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=200,
)

_pr_context_strategy = st.builds(
    BackportPRContext,
    source_pr_number=st.integers(min_value=1, max_value=999_999),
    source_pr_title=_safe_text,
    source_pr_body=_safe_text,
    source_pr_url=_safe_text,
    source_pr_diff=_safe_text,
    target_branch=_safe_text,
    commits=st.lists(_safe_text, min_size=1, max_size=5),
    repo_full_name=_safe_text,
)

_conflict_strategy = st.builds(
    ConflictedFile,
    path=_safe_text,
    content_with_markers=_safe_text,
    target_branch_content=_safe_text,
    source_branch_content=_safe_text,
)


# Feature: backport-agent, Property 7: Conflict resolution prompt contains all required context
class TestPromptCompletenessProperty:
    """
    **Validates: Requirements 3.1, 3.6**

    For any ConflictedFile and BackportPRContext, the constructed prompt
    should contain: the file content with conflict markers, the target
    branch version, the source branch version, the source PR title,
    body, and diff.
    """

    @given(conflict=_conflict_strategy, pr_context=_pr_context_strategy)
    @settings(max_examples=100, deadline=None)
    def test_prompt_contains_all_required_context(
        self,
        conflict: ConflictedFile,
        pr_context: BackportPRContext,
    ) -> None:
        bedrock = MagicMock()
        resolver = ConflictResolver(bedrock, BackportConfig())

        system_prompt, user_prompt = resolver._build_prompt(conflict, pr_context)

        # System prompt mentions the project
        assert "Valkey" in system_prompt

        # User prompt must contain all required context pieces
        assert conflict.content_with_markers in user_prompt
        assert conflict.target_branch_content in user_prompt
        assert conflict.source_branch_content in user_prompt
        assert pr_context.source_pr_title in user_prompt
        assert pr_context.source_pr_body in user_prompt
        assert pr_context.source_pr_diff in user_prompt


# Feature: backport-agent, Property 8: Independent file processing
class TestIndependentFileProcessingProperty:
    """
    **Validates: Requirements 3.5**

    For any list of ConflictedFiles, resolve_conflicts should return
    exactly one ResolutionResult per input file, regardless of whether
    individual resolutions succeed or fail.
    """

    @given(
        file_count=st.integers(min_value=1, max_value=10),
        succeed_flags=st.lists(st.booleans(), min_size=1, max_size=10),
    )
    @settings(max_examples=100, deadline=None)
    def test_one_result_per_file(
        self,
        file_count: int,
        succeed_flags: list[bool],
    ) -> None:
        # Ensure succeed_flags matches file_count
        flags = (succeed_flags * file_count)[:file_count]

        # Build conflicts with non-whitespace-only content so LLM path is taken
        conflicts = [
            ConflictedFile(
                path=f"src/file_{i}.c",
                content_with_markers=(
                    "<<<<<<< HEAD\n"
                    f"int f{i}() {{ return 0; }}\n"
                    "=======\n"
                    f"int f{i}() {{ return 1; }}\n"
                    ">>>>>>> abc\n"
                ),
                target_branch_content=f"int f{i}() {{ return 0; }}",
                source_branch_content=f"int f{i}() {{ return 1; }}",
            )
            for i in range(file_count)
        ]

        bedrock = MagicMock()
        call_idx = 0

        def mock_invoke(system: str, user: str, model_id: str = "") -> str:
            nonlocal call_idx
            # Determine which file this call is for based on call order
            file_idx = min(call_idx, file_count - 1)
            call_idx += 1
            if flags[file_idx]:
                # Clean content: no markers, balanced braces
                return f"int f{file_idx}() {{ return 42; }}"
            else:
                # Still has markers — will fail after retries
                return "<<<<<<< HEAD\nstuff\n=======\nother\n>>>>>>> abc"

        bedrock.invoke.side_effect = mock_invoke

        config = BackportConfig(max_conflict_retries=0)
        resolver = ConflictResolver(bedrock, config)

        results = resolver.resolve_conflicts(
            conflicts,
            _make_pr_context(),
            token_budget=10_000_000,
        )

        # Core property: exactly one result per input file
        assert len(results) == len(conflicts)
        # Results are in the same order as input files
        assert [r.path for r in results] == [c.path for c in conflicts]


# Feature: backport-agent, Property 10: Conflict resolver respects resource limits
class TestResourceLimitsProperty:
    """
    **Validates: Requirements 8.2, 8.3**

    For any list of conflicting files and configured limits:
    (a) not attempt resolution on any file if count exceeds
        max_conflicting_files
    (b) stop resolving further files once cumulative token usage
        exceeds per_backport_token_budget
    """

    @given(
        file_count=st.integers(min_value=1, max_value=30),
        max_files=st.integers(min_value=1, max_value=15),
    )
    @settings(max_examples=100, deadline=None)
    def test_skip_all_when_exceeds_max_files(
        self,
        file_count: int,
        max_files: int,
    ) -> None:
        conflicts = [
            ConflictedFile(
                path=f"src/f{i}.c",
                content_with_markers=(
                    "<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> abc\n"
                ),
                target_branch_content=f"int f{i}() {{ return 0; }}",
                source_branch_content=f"int f{i}() {{ return 1; }}",
            )
            for i in range(file_count)
        ]

        bedrock = MagicMock()
        bedrock.invoke.return_value = "int main() { return 0; }"
        config = BackportConfig(max_conflicting_files=max_files)
        resolver = ConflictResolver(bedrock, config)

        results = resolver.resolve_conflicts(
            conflicts, _make_pr_context(), token_budget=10_000_000,
        )

        assert len(results) == file_count

        if file_count > max_files:
            # (a) No file should be attempted — all skipped
            bedrock.invoke.assert_not_called()
            for r in results:
                assert r.resolved_content is None
                assert r.tokens_used == 0
                assert r.attempts == 0

    @given(
        file_count=st.integers(min_value=2, max_value=8),
        token_budget=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=100, deadline=None)
    def test_stop_resolving_when_budget_exhausted(
        self,
        file_count: int,
        token_budget: int,
    ) -> None:
        conflicts = [
            ConflictedFile(
                path=f"src/f{i}.c",
                content_with_markers=(
                    "<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> abc\n"
                ),
                target_branch_content=f"int f{i}() {{ return 0; }}",
                source_branch_content=f"int f{i}() {{ return 1; }}",
            )
            for i in range(file_count)
        ]

        bedrock = MagicMock()
        # Return clean content so each resolution succeeds and uses tokens
        bedrock.invoke.return_value = "int main() { return 0; }"
        config = BackportConfig(
            max_conflicting_files=100,  # high limit so we don't hit it
            max_conflict_retries=0,
        )
        resolver = ConflictResolver(bedrock, config)

        results = resolver.resolve_conflicts(
            conflicts, _make_pr_context(), token_budget=token_budget,
        )

        assert len(results) == file_count

        # (b) Once cumulative tokens exceed budget, remaining files are skipped
        cumulative_tokens = 0
        budget_exceeded = False
        for r in results:
            if budget_exceeded:
                # Files after budget exhaustion must be skipped
                assert r.resolved_content is None, (
                    f"File {r.path} should be skipped after budget exhaustion"
                )
                assert "token budget" in r.resolution_summary
                assert r.tokens_used == 0
            else:
                cumulative_tokens += r.tokens_used
                if cumulative_tokens >= token_budget:
                    budget_exceeded = True

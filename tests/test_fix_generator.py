"""Tests for the fix generator module."""

from __future__ import annotations

import difflib
import json
from unittest.mock import MagicMock, patch

import pytest

from scripts.bedrock_client import BedrockError
from scripts.config import BotConfig, RetrievalConfig
from scripts.fix_generator import (
    FixGenerator,
    _build_user_prompt,
    _count_patch_files,
    _strip_markdown_fences,
    _validate_patch_applies,
)
from scripts.models import RootCauseReport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_root_cause(**overrides) -> RootCauseReport:
    defaults = {
        "description": "Null pointer dereference in src/server.c",
        "files_to_change": ["src/server.c"],
        "confidence": "high",
        "rationale": "The pointer is not checked before use.",
        "is_flaky": False,
        "flakiness_indicators": None,
    }
    defaults.update(overrides)
    return RootCauseReport(**defaults)


_SAMPLE_DIFF = """\
--- a/src/server.c
+++ b/src/server.c
@@ -10,6 +10,7 @@
 void handle_request(Request *req) {
+    if (req == NULL) return;
     process(req->data);
 }
"""

_SAMPLE_DIFF_MULTI = """\
--- a/src/server.c
+++ b/src/server.c
@@ -10,6 +10,7 @@
 void handle_request(Request *req) {
+    if (req == NULL) return;
     process(req->data);
 }
--- a/src/client.c
+++ b/src/client.c
@@ -5,6 +5,7 @@
 void send_request() {
+    // fixed
 }
"""


def _make_generator(
    bedrock_return: str | Exception = _SAMPLE_DIFF,
    config: BotConfig | None = None,
) -> tuple[FixGenerator, MagicMock]:
    """Create a FixGenerator with a mocked BedrockClient."""
    mock_bedrock = MagicMock()
    if isinstance(bedrock_return, Exception):
        mock_bedrock.invoke.side_effect = bedrock_return
    else:
        mock_bedrock.invoke.return_value = bedrock_return
    cfg = config or BotConfig()
    return FixGenerator(mock_bedrock, cfg), mock_bedrock


# ---------------------------------------------------------------------------
# _strip_markdown_fences
# ---------------------------------------------------------------------------

class TestStripMarkdownFences:
    def test_strips_plain_fences(self):
        text = "```\nsome diff\n```"
        assert _strip_markdown_fences(text) == "some diff"

    def test_strips_language_fences(self):
        text = "```diff\nsome diff\n```"
        assert _strip_markdown_fences(text) == "some diff"

    def test_no_fences_unchanged(self):
        text = "--- a/file\n+++ b/file"
        assert _strip_markdown_fences(text) == text

    def test_empty_string(self):
        assert _strip_markdown_fences("") == ""


# ---------------------------------------------------------------------------
# _count_patch_files
# ---------------------------------------------------------------------------

class TestCountPatchFiles:
    def test_single_file(self):
        files = _count_patch_files(_SAMPLE_DIFF)
        assert files == {"src/server.c"}

    def test_multiple_files(self):
        files = _count_patch_files(_SAMPLE_DIFF_MULTI)
        assert files == {"src/server.c", "src/client.c"}

    def test_empty_diff(self):
        assert _count_patch_files("") == set()

    def test_new_file_excludes_dev_null(self):
        diff = "--- /dev/null\n+++ b/src/new.c\n@@ -0,0 +1 @@\n+new\n"
        files = _count_patch_files(diff)
        assert files == {"src/new.c"}


# ---------------------------------------------------------------------------
# _build_user_prompt
# ---------------------------------------------------------------------------

class TestBuildUserPrompt:
    def test_includes_root_cause_info(self):
        rc = _make_root_cause()
        prompt = _build_user_prompt(rc, {})
        assert "Null pointer dereference" in prompt
        assert "high" in prompt
        assert "src/server.c" in prompt

    def test_includes_source_files(self):
        rc = _make_root_cause()
        sources = {"src/server.c": "void handle_request() {}"}
        prompt = _build_user_prompt(rc, sources)
        assert "void handle_request" in prompt
        assert "### src/server.c" in prompt

    def test_includes_apply_error_feedback(self):
        rc = _make_root_cause()
        prompt = _build_user_prompt(rc, {}, apply_error="patch does not apply")
        assert "Previous Attempt Failed" in prompt
        assert "patch does not apply" in prompt

    def test_no_apply_error_omits_section(self):
        rc = _make_root_cause()
        prompt = _build_user_prompt(rc, {})
        assert "Previous Attempt Failed" not in prompt

    def test_includes_retrieved_context(self):
        rc = _make_root_cause()
        prompt = _build_user_prompt(rc, {}, "## Retrieved Valkey Context\nreplication notes")
        assert "Retrieved Valkey Context" in prompt
        assert "replication notes" in prompt


# ---------------------------------------------------------------------------
# FixGenerator.generate — confidence gating
# ---------------------------------------------------------------------------

class TestConfidenceGating:
    def test_skips_low_confidence(self):
        gen, mock_bedrock = _make_generator()
        rc = _make_root_cause(confidence="low")
        result = gen.generate(rc, {})
        assert result is None
        mock_bedrock.invoke.assert_not_called()

    def test_proceeds_with_high_confidence(self):
        gen, _ = _make_generator()
        rc = _make_root_cause(confidence="high")
        with patch("scripts.fix_generator._validate_patch_applies", return_value=(True, "")):
            result = gen.generate(rc, {"src/server.c": "code"})
        assert result is not None

    def test_proceeds_with_medium_confidence(self):
        gen, _ = _make_generator()
        rc = _make_root_cause(confidence="medium")
        with patch("scripts.fix_generator._validate_patch_applies", return_value=(True, "")):
            result = gen.generate(rc, {"src/server.c": "code"})
        assert result is not None

    def test_respects_configured_high_threshold(self):
        gen, mock_bedrock = _make_generator(
            config=BotConfig(confidence_threshold="high"),
        )
        rc = _make_root_cause(confidence="medium")
        result = gen.generate(rc, {})
        assert result is None
        mock_bedrock.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# FixGenerator.generate — patch validation and retries
# ---------------------------------------------------------------------------

class TestPatchValidation:
    def test_validate_patch_applies_against_source_file_workspace(self):
        original = "void handle_request(Request *req) {\n    process(req->data);\n}\n"
        patched = (
            "void handle_request(Request *req) {\n"
            "    if (req == NULL) return;\n"
            "    process(req->data);\n"
            "}\n"
        )
        diff = "\n".join(
            difflib.unified_diff(
                original.splitlines(),
                patched.splitlines(),
                fromfile="a/src/server.c",
                tofile="b/src/server.c",
                lineterm="",
            )
        ) + "\n"

        success, error_output = _validate_patch_applies(
            diff,
            {"src/server.c": original},
        )

        assert success is True
        assert error_output == ""

    def test_returns_diff_on_clean_apply(self):
        gen, _ = _make_generator()
        rc = _make_root_cause()
        with patch("scripts.fix_generator._validate_patch_applies", return_value=(True, "")):
            result = gen.generate(rc, {})
        assert result is not None
        assert "--- a/src/server.c" in result

    def test_retries_on_apply_failure(self):
        gen, mock_bedrock = _make_generator(config=BotConfig(max_retries_fix=2))
        rc = _make_root_cause()

        # First two attempts fail, third succeeds
        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return (False, "error: patch does not apply")
            return (True, "")

        with patch("scripts.fix_generator._validate_patch_applies", side_effect=side_effect):
            result = gen.generate(rc, {})

        assert result is not None
        assert "--- a/src/server.c" in result
        assert mock_bedrock.invoke.call_count == 3  # initial + 2 retries

    def test_returns_none_after_exhausting_retries(self):
        gen, mock_bedrock = _make_generator(config=BotConfig(max_retries_fix=1))
        rc = _make_root_cause()

        with patch(
            "scripts.fix_generator._validate_patch_applies",
            return_value=(False, "error: patch does not apply"),
        ):
            result = gen.generate(rc, {})

        assert result is None
        assert mock_bedrock.invoke.call_count == 2  # initial + 1 retry

    def test_retry_includes_error_feedback(self):
        gen, mock_bedrock = _make_generator(config=BotConfig(max_retries_fix=1))
        rc = _make_root_cause()

        with patch(
            "scripts.fix_generator._validate_patch_applies",
            return_value=(False, "error: corrupt patch"),
        ):
            gen.generate(rc, {})

        # Second call should include the error feedback
        assert mock_bedrock.invoke.call_count == 2
        second_call_args = mock_bedrock.invoke.call_args_list[1]
        user_prompt = second_call_args[0][1]
        assert "corrupt patch" in user_prompt

    def test_includes_retrieved_context_when_retriever_is_configured(self):
        gen, mock_bedrock = _make_generator()
        mock_retriever = MagicMock()
        mock_retriever.render_for_prompt.return_value = (
            "## Retrieved Valkey Context\nreplication subsystem notes"
        )
        gen.with_retriever(
            mock_retriever,
            RetrievalConfig(enabled=True, code_knowledge_base_id="CODEKB"),
        )
        rc = _make_root_cause()

        with patch("scripts.fix_generator._validate_patch_applies", return_value=(True, "")):
            gen.generate(rc, {"src/server.c": "void handle_request(void) {}"})

        user_prompt = mock_bedrock.invoke.call_args[0][1]
        assert "Retrieved Valkey Context" in user_prompt
        assert "replication subsystem notes" in user_prompt

    def test_includes_failed_hypotheses_when_provided(self):
        gen, mock_bedrock = _make_generator()
        rc = _make_root_cause()

        with patch("scripts.fix_generator._validate_patch_applies", return_value=(True, "")):
            gen.generate(
                rc,
                {"src/server.c": "void handle_request(void) {}"},
                failed_hypotheses=["null-guard only did not hold in repeated validation"],
            )

        user_prompt = mock_bedrock.invoke.call_args[0][1]
        assert "Previous Failed Approaches" in user_prompt
        assert "null-guard only did not hold" in user_prompt


# ---------------------------------------------------------------------------
# FixGenerator.generate — agentic path safety
# ---------------------------------------------------------------------------

class TestAgenticGeneration:
    def test_agentic_generation_uses_explicit_repo_ref(self):
        mock_bedrock = MagicMock()
        mock_bedrock.converse_with_tools.return_value = json.dumps({
            "diff": _SAMPLE_DIFF,
        })
        gen = FixGenerator(
            mock_bedrock,
            BotConfig(),
            github_client=MagicMock(),
            repo_full_name="owner/repo",
        )
        rc = _make_root_cause()

        with patch("scripts.code_reviewer.ReviewToolHandler") as handler_cls:
            with patch("scripts.fix_generator._validate_patch_applies", return_value=(True, "")):
                result = gen.generate(
                    rc,
                    {"src/server.c": "void handle_request(Request *req) {}"},
                    repo_ref="abc123",
                )

        assert result is not None
        assert _count_patch_files(result) == {"src/server.c"}
        assert handler_cls.call_args.kwargs["head_sha"] == "abc123"

    def test_agentic_generation_rejects_patch_outside_allowed_files(self):
        mock_bedrock = MagicMock()
        mock_bedrock.converse_with_tools.return_value = json.dumps({
            "diff": _SAMPLE_DIFF_MULTI,
        })
        gen = FixGenerator(
            mock_bedrock,
            BotConfig(),
            github_client=MagicMock(),
            repo_full_name="owner/repo",
        )
        rc = _make_root_cause(files_to_change=["src/server.c"])

        with patch("scripts.fix_generator._validate_patch_applies", return_value=(True, "")):
            result = gen._generate_agentic(
                rc,
                {"src/server.c": "void handle_request(Request *req) {}"},
                repo_ref="abc123",
            )

        assert result is None


# ---------------------------------------------------------------------------
# FixGenerator.generate — patch scope limits
# ---------------------------------------------------------------------------

class TestPatchScopeLimits:
    def test_rejects_patch_exceeding_max_files(self):
        # Generate a diff with 11 files (default limit is 10)
        lines = []
        for i in range(11):
            lines.append(f"--- a/src/file{i}.c")
            lines.append(f"+++ b/src/file{i}.c")
            lines.append("@@ -1,1 +1,2 @@")
            lines.append(" existing")
            lines.append("+added")
        big_diff = "\n".join(lines) + "\n"

        gen, _ = _make_generator(bedrock_return=big_diff)
        rc = _make_root_cause()
        result = gen.generate(rc, {})
        assert result is None

    def test_accepts_patch_within_file_limit(self):
        gen, _ = _make_generator(bedrock_return=_SAMPLE_DIFF_MULTI)
        rc = _make_root_cause(files_to_change=["src/server.c", "src/client.c"])
        with patch("scripts.fix_generator._validate_patch_applies", return_value=(True, "")):
            result = gen.generate(rc, {})
        assert result is not None

    def test_custom_max_patch_files(self):
        gen, _ = _make_generator(
            bedrock_return=_SAMPLE_DIFF_MULTI,
            config=BotConfig(max_patch_files=1),
        )
        rc = _make_root_cause()
        # 2 files > limit of 1
        result = gen.generate(rc, {})
        assert result is None

    def test_rejects_patch_outside_allowed_files(self):
        gen, mock_bedrock = _make_generator(
            bedrock_return=_SAMPLE_DIFF_MULTI,
            config=BotConfig(max_retries_fix=0),
        )
        rc = _make_root_cause(files_to_change=["src/server.c"])
        result = gen.generate(rc, {})
        assert result is None
        assert mock_bedrock.invoke.call_count == 1


# ---------------------------------------------------------------------------
# FixGenerator.generate — Bedrock errors
# ---------------------------------------------------------------------------

class TestBedrockErrors:
    def test_returns_none_on_bedrock_error(self):
        gen, _ = _make_generator(
            bedrock_return=BedrockError("API error", error_code="500")
        )
        rc = _make_root_cause()
        result = gen.generate(rc, {})
        assert result is None

    def test_returns_none_on_empty_response(self):
        gen, _ = _make_generator(bedrock_return="")
        rc = _make_root_cause(confidence="high")
        # Empty diff after stripping — should exhaust retries
        result = gen.generate(rc, {})
        assert result is None


# ---------------------------------------------------------------------------
# FixGenerator.generate — markdown fence stripping
# ---------------------------------------------------------------------------

class TestMarkdownFenceStripping:
    def test_strips_fences_from_response(self):
        fenced = f"```diff\n{_SAMPLE_DIFF}```"
        gen, _ = _make_generator(bedrock_return=fenced)
        rc = _make_root_cause()
        with patch("scripts.fix_generator._validate_patch_applies", return_value=(True, "")):
            result = gen.generate(rc, {})
        assert result is not None
        assert "```" not in result


# ---------------------------------------------------------------------------
# Feature: valkey-ci-agent, Property 8: Confidence gating for fix generation
# ---------------------------------------------------------------------------

from hypothesis import given, settings
from hypothesis import strategies as st

# Strategy: generate RootCauseReport instances with varying confidence levels
_confidence_strategy = st.sampled_from(["high", "medium", "low"])

_root_cause_strategy = st.builds(
    RootCauseReport,
    description=st.text(min_size=1, max_size=100),
    files_to_change=st.just([]),
    confidence=_confidence_strategy,
    rationale=st.text(min_size=1, max_size=100),
    is_flaky=st.booleans(),
    flakiness_indicators=st.none(),
)


class TestConfidenceGatingProperty:
    """Property 8: Confidence gating for fix generation.

    **Validates: Requirements 4.1, 4.6**

    For any RootCauseReport, the Fix_Generator should proceed with generation
    if and only if the confidence level is "high" or "medium". For any report
    with confidence "low", no fix should be generated.
    """

    @given(root_cause=_root_cause_strategy)
    @settings(max_examples=100)
    def test_confidence_gating_property(self, root_cause: RootCauseReport):
        """Low confidence → None + no Bedrock call; high/medium → Bedrock called."""
        gen, mock_bedrock = _make_generator(bedrock_return=_SAMPLE_DIFF)

        if root_cause.confidence == "low":
            result = gen.generate(root_cause, {"src/server.c": "code"})
            assert result is None, "Low confidence must produce None"
            mock_bedrock.invoke.assert_not_called()
        else:
            # confidence is "high" or "medium" — Bedrock should be invoked
            with patch(
                "scripts.fix_generator._validate_patch_applies",
                return_value=(True, ""),
            ):
                result = gen.generate(root_cause, {"src/server.c": "code"})
            assert result is not None, (
                f"Confidence '{root_cause.confidence}' must proceed with generation"
            )
            mock_bedrock.invoke.assert_called()


# ---------------------------------------------------------------------------
# Feature: valkey-ci-agent, Property 9: Patch scope validation
# ---------------------------------------------------------------------------


def _make_diff_for_files(file_paths: list[str]) -> str:
    """Build a minimal unified diff that modifies the given file paths."""
    lines: list[str] = []
    for path in file_paths:
        lines.append(f"--- a/{path}")
        lines.append(f"+++ b/{path}")
        lines.append("@@ -1,1 +1,2 @@")
        lines.append(" existing")
        lines.append("+added")
    return "\n".join(lines) + "\n"


# Strategy: file paths that look like real C source paths
_file_path_strategy = st.from_regex(r"src/[a-z][a-z0-9_]{0,15}\.(c|h)", fullmatch=True)


class TestPatchScopeValidationProperty:
    """Property 9: Patch scope validation.

    **Validates: Requirements 4.5**

    For any generated patch, the total number of modified files should not
    exceed the configured max_patch_files limit. Patches exceeding the limit
    are rejected (return None); patches within the limit are accepted.
    """

    @given(
        file_paths=st.lists(
            _file_path_strategy,
            min_size=1,
            max_size=20,
            unique=True,
        ),
        max_patch_files=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=100)
    def test_patch_scope_validation_property(
        self,
        file_paths: list[str],
        max_patch_files: int,
    ):
        """Patches exceeding max_patch_files are rejected; within-limit patches are accepted."""
        diff = _make_diff_for_files(file_paths)
        num_files = len(file_paths)

        config = BotConfig(max_patch_files=max_patch_files)
        gen, mock_bedrock = _make_generator(bedrock_return=diff, config=config)
        rc = _make_root_cause(
            confidence="high",
            files_to_change=file_paths,
        )

        with patch(
            "scripts.fix_generator._validate_patch_applies",
            return_value=(True, ""),
        ):
            result = gen.generate(rc, {"src/server.c": "code"})

        if num_files > max_patch_files:
            assert result is None, (
                f"Patch with {num_files} files should be rejected "
                f"(limit={max_patch_files})"
            )
        else:
            assert result is not None, (
                f"Patch with {num_files} files should be accepted "
                f"(limit={max_patch_files})"
            )
            # Verify the accepted patch only modifies the expected files
            modified = _count_patch_files(result)
            assert len(modified) <= max_patch_files, (
                f"Accepted patch modifies {len(modified)} files "
                f"but limit is {max_patch_files}"
            )


# ---------------------------------------------------------------------------
# Feature: valkey-ci-agent, Property 10: Fix generation retry limit
# ---------------------------------------------------------------------------


class TestFixGenerationRetryLimitProperty:
    """Property 10: Fix generation retry limit.

    **Validates: Requirements 4.4**

    For any sequence of patch apply failures, the Fix_Generator should retry
    at most max_retries_fix times. After exhausting retries, the fix should
    be marked as "generation-failed" (returns None). Total Bedrock calls
    should equal max_retries_fix + 1 (initial attempt + retries).
    """

    @given(max_retries=st.integers(min_value=0, max_value=5))
    @settings(max_examples=100)
    def test_fix_generation_retry_limit_property(self, max_retries: int):
        """When all patch applies fail, exactly max_retries_fix + 1 Bedrock
        calls are made and the result is None."""
        config = BotConfig(max_retries_fix=max_retries)
        gen, mock_bedrock = _make_generator(
            bedrock_return=_SAMPLE_DIFF, config=config
        )
        rc = _make_root_cause(confidence="high")

        with patch(
            "scripts.fix_generator._validate_patch_applies",
            return_value=(False, "error: patch does not apply"),
        ):
            result = gen.generate(rc, {"src/server.c": "code"})

        expected_calls = max_retries + 1
        assert result is None, (
            f"Expected None after exhausting {max_retries} retries, "
            f"got a non-None result"
        )
        assert mock_bedrock.invoke.call_count == expected_calls, (
            f"Expected {expected_calls} Bedrock calls "
            f"(1 initial + {max_retries} retries), "
            f"got {mock_bedrock.invoke.call_count}"
        )

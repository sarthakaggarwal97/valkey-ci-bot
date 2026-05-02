
from __future__ import annotations

import difflib
from types import SimpleNamespace
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.config import BotConfig
from scripts.fix_generator import (
    FixGenerator,
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


def _make_generator(config: BotConfig | None = None) -> FixGenerator:
    cfg = config or BotConfig()
    return FixGenerator(cfg, repo_full_name="valkey-io/valkey")


def _fake_git_run(cmd, **_kwargs):
    """Stub subprocess.run to succeed for git clone/fetch/checkout calls."""
    if cmd[:2] in (["git", "clone"], ["git", "fetch"], ["git", "checkout"]):
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    raise AssertionError(f"unexpected command: {cmd}")


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
# _validate_patch_applies
# ---------------------------------------------------------------------------


class TestValidatePatchApplies:
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


# ---------------------------------------------------------------------------
# FixGenerator.generate — confidence gating
# ---------------------------------------------------------------------------


class TestConfidenceGating:
    def test_skips_low_confidence(self):
        gen = _make_generator()
        rc = _make_root_cause(confidence="low")
        with patch.object(gen, "_generate_with_claude_code") as mock_claude:
            result = gen.generate(rc, {})
        assert result is None
        mock_claude.assert_not_called()

    def test_proceeds_with_high_confidence(self):
        gen = _make_generator()
        rc = _make_root_cause(confidence="high")
        with patch.object(
            gen, "_generate_with_claude_code", return_value=_SAMPLE_DIFF,
        ) as mock_claude:
            result = gen.generate(rc, {"src/server.c": "code"})
        assert result == _SAMPLE_DIFF
        mock_claude.assert_called_once()

    def test_proceeds_with_medium_confidence(self):
        gen = _make_generator()
        rc = _make_root_cause(confidence="medium")
        with patch.object(
            gen, "_generate_with_claude_code", return_value=_SAMPLE_DIFF,
        ) as mock_claude:
            result = gen.generate(rc, {"src/server.c": "code"})
        assert result == _SAMPLE_DIFF
        mock_claude.assert_called_once()

    def test_respects_configured_high_threshold(self):
        gen = _make_generator(config=BotConfig(confidence_threshold="high"))
        rc = _make_root_cause(confidence="medium")
        with patch.object(gen, "_generate_with_claude_code") as mock_claude:
            result = gen.generate(rc, {})
        assert result is None
        mock_claude.assert_not_called()


# ---------------------------------------------------------------------------
# FixGenerator.generate — returns None when Claude Code is unavailable
# ---------------------------------------------------------------------------


class TestClaudeCodeUnavailable:
    def test_returns_none_when_claude_cli_missing(self):
        gen = _make_generator()
        rc = _make_root_cause()
        with patch("scripts.fix_generator.shutil.which", return_value=None):
            result = gen.generate(rc, {"src/server.c": "code"})
        assert result is None

    def test_returns_none_when_repo_full_name_missing(self):
        gen = FixGenerator(BotConfig(), repo_full_name="")
        rc = _make_root_cause()
        with patch("scripts.fix_generator.shutil.which", return_value="/bin/claude"):
            result = gen.generate(rc, {"src/server.c": "code"})
        assert result is None

    def test_returns_none_when_disabled_by_env(self, monkeypatch):
        gen = _make_generator()
        rc = _make_root_cause()
        monkeypatch.setenv("CI_AGENT_DISABLE_CLAUDE_PATCH_GENERATOR", "1")
        with patch("scripts.fix_generator.shutil.which", return_value="/bin/claude"):
            result = gen.generate(rc, {"src/server.c": "code"})
        assert result is None


# ---------------------------------------------------------------------------
# FixGenerator.generate — Claude Code happy path + validation
# ---------------------------------------------------------------------------


class TestClaudeCodePatchGeneration:
    def test_generate_returns_captured_diff_from_claude_code(self):
        gen = _make_generator()
        rc = _make_root_cause(files_to_change=["src/server.c"])

        with patch("scripts.fix_generator.shutil.which", return_value="/bin/claude"), \
            patch("scripts.fix_generator.subprocess.run", side_effect=_fake_git_run), \
            patch(
                "scripts.fix_generator.run_agent",
                return_value=SimpleNamespace(stdout="edited", stderr="", returncode=0),
            ) as mock_agent, \
            patch("scripts.fix_generator._capture_worktree_diff", return_value=_SAMPLE_DIFF):
            result = gen.generate(rc, {"src/server.c": "old"})

        assert result == _SAMPLE_DIFF
        mock_agent.assert_called_once()

    def test_generate_returns_none_when_claude_agent_fails(self):
        gen = _make_generator()
        rc = _make_root_cause(files_to_change=["src/server.c"])

        with patch("scripts.fix_generator.shutil.which", return_value="/bin/claude"), \
            patch("scripts.fix_generator.subprocess.run", side_effect=_fake_git_run), \
            patch(
                "scripts.fix_generator.run_agent",
                return_value=SimpleNamespace(stdout="failed", stderr="", returncode=1),
            ):
            result = gen.generate(rc, {"src/server.c": "old"})

        assert result is None

    def test_generate_rejects_patch_outside_allowed_files(self):
        gen = _make_generator()
        rc = _make_root_cause(files_to_change=["src/server.c"])

        with patch("scripts.fix_generator.shutil.which", return_value="/bin/claude"), \
            patch("scripts.fix_generator.subprocess.run", side_effect=_fake_git_run), \
            patch(
                "scripts.fix_generator.run_agent",
                return_value=SimpleNamespace(stdout="edited", stderr="", returncode=0),
            ), \
            patch("scripts.fix_generator._capture_worktree_diff", return_value=_SAMPLE_DIFF_MULTI):
            result = gen.generate(rc, {"src/server.c": "old"})

        # Patch touches src/server.c AND src/client.c but scope limits to server.c
        assert result is None

    def test_generate_uses_explicit_repo_ref_for_checkout(self):
        gen = _make_generator()
        rc = _make_root_cause(files_to_change=["src/server.c"])

        observed = {}

        def fake_run(cmd, **kwargs):
            # Record the checkout ref
            if cmd[:2] == ["git", "checkout"]:
                observed["checkout_cmd"] = cmd
            if cmd[:2] == ["git", "fetch"]:
                observed["fetch_cmd"] = cmd
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        with patch("scripts.fix_generator.shutil.which", return_value="/bin/claude"), \
            patch("scripts.fix_generator.subprocess.run", side_effect=fake_run), \
            patch(
                "scripts.fix_generator.run_agent",
                return_value=SimpleNamespace(stdout="edited", stderr="", returncode=0),
            ), \
            patch("scripts.fix_generator._capture_worktree_diff", return_value=_SAMPLE_DIFF):
            result = gen.generate(rc, {"src/server.c": "old"}, repo_ref="abc123")

        assert result == _SAMPLE_DIFF
        # After fetching repo_ref, the checkout uses FETCH_HEAD
        assert observed["checkout_cmd"][-1] == "FETCH_HEAD"
        assert "abc123" in observed["fetch_cmd"]

    def test_with_domain_context_feeds_into_prompt(self):
        gen = _make_generator()
        rc = _make_root_cause()

        captured_prompt = {}

        def capture_run_agent(name, prompt, cwd):
            captured_prompt["prompt"] = prompt
            return SimpleNamespace(stdout="edited", stderr="", returncode=0)

        gen.with_domain_context("Valkey runtime: replication is critical.")

        with patch("scripts.fix_generator.shutil.which", return_value="/bin/claude"), \
            patch("scripts.fix_generator.subprocess.run", side_effect=_fake_git_run), \
            patch("scripts.fix_generator.run_agent", side_effect=capture_run_agent), \
            patch("scripts.fix_generator._capture_worktree_diff", return_value=_SAMPLE_DIFF):
            gen.generate(rc, {"src/server.c": "old"})

        assert "Valkey runtime: replication is critical." in captured_prompt["prompt"]


# ---------------------------------------------------------------------------
# Property: confidence gating
# ---------------------------------------------------------------------------

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
    """Confidence gating remains enforced regardless of backend."""

    @given(root_cause=_root_cause_strategy)
    @settings(max_examples=50)
    def test_confidence_gating_property(self, root_cause: RootCauseReport):
        gen = _make_generator()
        with patch.object(
            gen, "_generate_with_claude_code", return_value=_SAMPLE_DIFF,
        ) as mock_claude:
            result = gen.generate(root_cause, {"src/server.c": "code"})

        if root_cause.confidence == "low":
            assert result is None
            mock_claude.assert_not_called()
        else:
            assert result == _SAMPLE_DIFF
            mock_claude.assert_called_once()


# ---------------------------------------------------------------------------
# Property: patch scope validation via _validate_checkout_diff
# ---------------------------------------------------------------------------


def _make_diff_for_files(file_paths: list[str]) -> str:
    lines: list[str] = []
    for path in file_paths:
        lines.append(f"--- a/{path}")
        lines.append(f"+++ b/{path}")
        lines.append("@@ -1,1 +1,2 @@")
        lines.append(" existing")
        lines.append("+added")
    return "\n".join(lines) + "\n"


_file_path_strategy = st.from_regex(r"src/[a-z][a-z0-9_]{0,15}\.(c|h)", fullmatch=True)


class TestPatchScopeValidationProperty:
    """Patches exceeding max_patch_files are rejected; within-limit patches accepted."""

    @given(
        file_paths=st.lists(
            _file_path_strategy,
            min_size=1,
            max_size=20,
            unique=True,
        ),
        max_patch_files=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=50)
    def test_patch_scope_validation_property(
        self,
        file_paths: list[str],
        max_patch_files: int,
    ):
        diff = _make_diff_for_files(file_paths)
        num_files = len(file_paths)

        config = BotConfig(max_patch_files=max_patch_files)
        gen = FixGenerator(config, repo_full_name="valkey-io/valkey")
        rc = _make_root_cause(
            confidence="high",
            files_to_change=file_paths,
        )

        with patch("scripts.fix_generator.shutil.which", return_value="/bin/claude"), \
            patch("scripts.fix_generator.subprocess.run", side_effect=_fake_git_run), \
            patch(
                "scripts.fix_generator.run_agent",
                return_value=SimpleNamespace(stdout="edited", stderr="", returncode=0),
            ), \
            patch("scripts.fix_generator._capture_worktree_diff", return_value=diff):
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
            modified = _count_patch_files(result)
            assert len(modified) <= max_patch_files, (
                f"Accepted patch modifies {len(modified)} files "
                f"but limit is {max_patch_files}"
            )

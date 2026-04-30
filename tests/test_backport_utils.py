"""Unit tests for backport utility functions."""

from __future__ import annotations

from scripts.backport_utils import (
    build_branch_name,
    build_pr_title,
    has_conflict_markers,
    is_whitespace_only_conflict,
    parse_backport_labels,
    validate_c_syntax,
)

# --- parse_backport_labels ---


class TestParseBackportLabels:
    def test_single_label(self) -> None:
        assert parse_backport_labels(["backport 8.1"]) == ["8.1"]

    def test_multiple_labels(self) -> None:
        labels = ["backport 8.1", "bug", "backport 7.2"]
        assert parse_backport_labels(labels) == ["8.1", "7.2"]

    def test_no_matching_labels(self) -> None:
        assert parse_backport_labels(["bug", "enhancement", "urgent"]) == []

    def test_empty_list(self) -> None:
        assert parse_backport_labels([]) == []

    def test_case_sensitive(self) -> None:
        assert parse_backport_labels(["Backport 8.1", "BACKPORT 8.1"]) == []

    def test_prefix_only_no_branch(self) -> None:
        assert parse_backport_labels(["backport "]) == []

    def test_exact_string_backport(self) -> None:
        assert parse_backport_labels(["backport"]) == []

    def test_branch_with_slashes(self) -> None:
        assert parse_backport_labels(["backport release/8.1"]) == ["release/8.1"]


# --- build_branch_name ---


class TestBuildBranchName:
    def test_basic(self) -> None:
        assert build_branch_name(123, "8.1") == "backport/123-to-8.1"

    def test_large_pr_number(self) -> None:
        assert build_branch_name(99999, "7.2") == "backport/99999-to-7.2"


# --- build_pr_title ---


class TestBuildPrTitle:
    def test_basic(self) -> None:
        assert build_pr_title("Fix memory leak", "8.1") == "[Backport 8.1] Fix memory leak"

    def test_preserves_original_title(self) -> None:
        title = "[BUG] Segfault on startup"
        assert build_pr_title(title, "7.2") == "[Backport 7.2] [BUG] Segfault on startup"


# --- has_conflict_markers ---


class TestHasConflictMarkers:
    def test_no_markers(self) -> None:
        assert has_conflict_markers("clean code\nno conflicts\n") is False

    def test_opening_marker(self) -> None:
        assert has_conflict_markers("<<<<<<< HEAD\ncode\n") is True

    def test_separator_marker(self) -> None:
        assert has_conflict_markers("code\n=======\nother\n") is True

    def test_closing_marker(self) -> None:
        assert has_conflict_markers("code\n>>>>>>> branch\n") is True

    def test_full_conflict_block(self) -> None:
        content = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
        assert has_conflict_markers(content) is True

    def test_fewer_than_seven_chars(self) -> None:
        assert has_conflict_markers("<<<<<< not enough") is False
        assert has_conflict_markers("====== not enough") is False
        assert has_conflict_markers(">>>>>> not enough") is False

    def test_empty_string(self) -> None:
        assert has_conflict_markers("") is False


# --- validate_c_syntax ---


class TestValidateCSyntax:
    def test_balanced(self) -> None:
        assert validate_c_syntax("int main() { return 0; }") is True

    def test_nested_balanced(self) -> None:
        assert validate_c_syntax("void f() { if (x) { y(); } }") is True

    def test_unbalanced_extra_open(self) -> None:
        assert validate_c_syntax("void f() { if (x) {") is False

    def test_unbalanced_extra_close(self) -> None:
        assert validate_c_syntax("void f() }") is False

    def test_empty_string(self) -> None:
        assert validate_c_syntax("") is True

    def test_no_braces(self) -> None:
        assert validate_c_syntax("// just a comment") is True

    def test_closing_before_opening(self) -> None:
        assert validate_c_syntax("} {") is False


# --- is_whitespace_only_conflict ---


class TestIsWhitespaceOnlyConflict:
    def test_identical(self) -> None:
        assert is_whitespace_only_conflict("int x = 1;", "int x = 1;") is True

    def test_different_indentation(self) -> None:
        assert is_whitespace_only_conflict("  int x = 1;", "    int x = 1;") is True

    def test_trailing_whitespace(self) -> None:
        assert is_whitespace_only_conflict("int x = 1;  ", "int x = 1;") is True

    def test_different_line_endings(self) -> None:
        assert is_whitespace_only_conflict("a\nb\n", "a\r\nb\r\n") is True

    def test_tabs_vs_spaces(self) -> None:
        assert is_whitespace_only_conflict("\tint x;", "    int x;") is True

    def test_actual_content_difference(self) -> None:
        assert is_whitespace_only_conflict("int x = 1;", "int x = 2;") is False

    def test_both_empty(self) -> None:
        assert is_whitespace_only_conflict("", "") is True

    def test_whitespace_vs_empty(self) -> None:
        assert is_whitespace_only_conflict("   ", "") is True

    def test_different_code(self) -> None:
        assert is_whitespace_only_conflict("foo()", "bar()") is False


# --- Property-Based Tests ---

from hypothesis import given, settings
from hypothesis import strategies as st


# Feature: backport-agent, Property 1: Label parsing extracts correct target branches
class TestParseBackportLabelsProperty:
    """**Validates: Requirements 1.1, 1.4**"""

    @given(
        backport_branches=st.lists(
            st.text(min_size=1, max_size=50).filter(lambda s: "\x00" not in s),
            max_size=10,
        ),
        other_labels=st.lists(
            st.text(max_size=50).filter(
                lambda s: not s.startswith("backport ") or s == "backport "
            ),
            max_size=10,
        ),
    )
    @settings(max_examples=100)
    def test_extracts_exactly_matching_branches(
        self, backport_branches: list[str], other_labels: list[str]
    ) -> None:
        """For any mix of backport and non-backport labels, parse_backport_labels
        returns exactly the branch names from labels matching 'backport <branch>'."""
        # Build labels: valid backport labels + noise labels
        backport_labels = [f"backport {branch}" for branch in backport_branches]
        all_labels = backport_labels + other_labels

        result = parse_backport_labels(all_labels)

        # The result should contain exactly the branches we constructed,
        # in the same order (backport labels come first in our input list)
        assert result == backport_branches

    @given(
        labels=st.lists(
            st.text(max_size=50).filter(
                lambda s: not s.startswith("backport ") or s == "backport "
            ),
            max_size=20,
        ),
    )
    @settings(max_examples=100)
    def test_no_extra_branches_from_non_matching_labels(
        self, labels: list[str]
    ) -> None:
        """When no labels match 'backport <branch>', the result is empty."""
        result = parse_backport_labels(labels)
        assert result == []


# Feature: backport-agent, Property 2: Branch name construction follows convention
class TestBuildBranchNameProperty:
    """**Validates: Requirements 4.1, 6.2**"""

    @given(
        pr_number=st.integers(min_value=1, max_value=10**9),
        target_branch=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N", "P")),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_branch_name_matches_convention(
        self, pr_number: int, target_branch: str
    ) -> None:
        """For any positive PR number and non-empty branch name,
        build_branch_name returns 'backport/<pr_number>-to-<target_branch>'."""
        result = build_branch_name(pr_number, target_branch)
        assert result == f"backport/{pr_number}-to-{target_branch}"
        assert result.startswith("backport/")
        assert f"-to-{target_branch}" in result


# Feature: backport-agent, Property 3: PR title follows convention
class TestBuildPrTitleProperty:
    """**Validates: Requirements 4.3**"""

    @given(
        source_title=st.text(min_size=1, max_size=200),
        target_branch=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N", "P")),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_pr_title_matches_convention(
        self, source_title: str, target_branch: str
    ) -> None:
        """For any non-empty PR title and branch name,
        build_pr_title returns '[Backport <target_branch>] <source_title>'."""
        result = build_pr_title(source_title, target_branch)
        assert result == f"[Backport {target_branch}] {source_title}"
        assert result.startswith(f"[Backport {target_branch}] ")
        assert result.endswith(source_title)


# Feature: backport-agent, Property 5: Conflict marker detection
class TestHasConflictMarkersProperty:
    """**Validates: Requirements 3.3**"""

    MARKERS = ["<<<<<<<", "=======", ">>>>>>>"]

    @given(
        base=st.text(max_size=200),
        marker=st.sampled_from(MARKERS),
        suffix=st.text(max_size=50),
    )
    @settings(max_examples=100, deadline=None)
    def test_detects_injected_markers(
        self, base: str, marker: str, suffix: str
    ) -> None:
        """Strings with an injected conflict marker are always detected."""
        # Filter out base strings that already contain a marker
        from hypothesis import assume

        assume(not any(m in base for m in self.MARKERS))
        content = base + marker + suffix
        assert has_conflict_markers(content) is True

    @given(
        content=st.text(
            alphabet=st.characters(
                blacklist_characters="<=>",
            ),
            max_size=300,
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_no_false_positives_without_marker_chars(self, content: str) -> None:
        """Strings without '<', '=', '>' characters never trigger detection."""
        assert has_conflict_markers(content) is False


# Feature: backport-agent, Property 6: C syntax validation rejects unbalanced braces
class TestValidateCSyntaxProperty:
    """**Validates: Requirements 5.5**"""

    @given(data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_balanced_braces_accepted(self, data: st.DataObject) -> None:
        """Strings with balanced curly braces (depth never negative) pass validation."""
        # Build a string with guaranteed balanced braces
        depth = data.draw(st.integers(min_value=1, max_value=10))
        filler = st.text(
            alphabet=st.characters(blacklist_characters="{}"), max_size=20
        )
        parts: list[str] = []
        for _ in range(depth):
            parts.append(data.draw(filler))
            parts.append("{")
        for _ in range(depth):
            parts.append(data.draw(filler))
            parts.append("}")
        parts.append(data.draw(filler))
        content = "".join(parts)
        assert validate_c_syntax(content) is True

    @given(
        content=st.text(
            alphabet=st.characters(blacklist_characters="{}"), max_size=100
        ),
        extra_opens=st.integers(min_value=1, max_value=5),
    )
    @settings(max_examples=100, deadline=None)
    def test_extra_open_braces_rejected(
        self, content: str, extra_opens: int
    ) -> None:
        """Strings with more '{' than '}' are rejected."""
        unbalanced = content + "{" * extra_opens
        assert validate_c_syntax(unbalanced) is False

    @given(
        content=st.text(
            alphabet=st.characters(blacklist_characters="{}"), max_size=100
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_closing_before_opening_rejected(self, content: str) -> None:
        """A '}' appearing before any '{' is rejected."""
        unbalanced = "}" + content + "{"
        assert validate_c_syntax(unbalanced) is False


# Feature: backport-agent, Property 14: Whitespace-only conflicts are resolved without LLM
class TestIsWhitespaceOnlyConflictProperty:
    """**Validates: Requirements 3.2**"""

    @given(
        base=st.text(min_size=0, max_size=200),
    )
    @settings(max_examples=100, deadline=None)
    def test_whitespace_variations_detected(self, base: str) -> None:
        """Adding whitespace-only changes to a string is detected as whitespace-only."""
        import re

        # Create a version with whitespace modifications:
        # replace each whitespace char with a different whitespace sequence
        ws_map = {" ": "\t", "\t": "  ", "\n": "\r\n", "\r": "\n"}
        modified = []
        for ch in base:
            if ch in ws_map:
                modified.append(ws_map[ch])
            else:
                modified.append(ch)
        modified_str = "".join(modified)
        # Both should have the same non-whitespace content
        assert is_whitespace_only_conflict(base, modified_str) is True

    @given(
        base=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N", "P")),
            min_size=1,
            max_size=100,
        ),
        insert_char=st.characters(whitelist_categories=("L", "N", "P")),
        insert_pos=st.integers(min_value=0),
    )
    @settings(max_examples=100, deadline=None)
    def test_non_whitespace_differences_detected(
        self, base: str, insert_char: str, insert_pos: int
    ) -> None:
        """Strings with non-whitespace differences return False."""
        from hypothesis import assume

        pos = insert_pos % (len(base) + 1)
        modified = base[:pos] + insert_char + base[pos:]
        # Only test when the non-whitespace content actually differs
        import re

        base_stripped = re.sub(r"\s+", "", base)
        modified_stripped = re.sub(r"\s+", "", modified)
        assume(base_stripped != modified_stripped)
        assert is_whitespace_only_conflict(base, modified) is False

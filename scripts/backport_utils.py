"""Utility functions for the Backport Agent pipeline.

Provides label parsing, naming conventions, conflict detection, and basic
syntax validation helpers used across the backport workflow.
"""

from __future__ import annotations

import re

_BACKPORT_PREFIX = "backport "
_CONFLICT_MARKERS = re.compile(r"<{7}|={7}|>{7}")


def parse_backport_labels(labels: list[str]) -> list[str]:
    """Extract target branch names from labels matching ``backport <branch>``.

    Only labels with the exact case-sensitive prefix ``"backport "`` followed
    by a non-empty branch name are considered.  Duplicate branch names are
    preserved in the order they appear.

    **Validates: Requirements 1.1, 1.4**
    """
    branches: list[str] = []
    for label in labels:
        if label.startswith(_BACKPORT_PREFIX):
            branch = label[len(_BACKPORT_PREFIX):]
            if branch:
                branches.append(branch)
    return branches


def build_branch_name(source_pr_number: int, target_branch: str) -> str:
    """Return ``backport/<source_pr_number>-to-<target_branch>``.

    **Validates: Requirements 4.1, 6.2**
    """
    return f"backport/{source_pr_number}-to-{target_branch}"


def build_pr_title(source_pr_title: str, target_branch: str) -> str:
    """Return ``[Backport <target_branch>] <source_pr_title>``.

    **Validates: Requirements 4.3**
    """
    return f"[Backport {target_branch}] {source_pr_title}"


def has_conflict_markers(content: str) -> bool:
    """Check whether *content* contains git conflict markers.

    Returns ``True`` if any of ``<<<<<<<``, ``=======``, or ``>>>>>>>``
    (seven characters each) appear anywhere in the string.

    **Validates: Requirements 3.3**
    """
    return bool(_CONFLICT_MARKERS.search(content))


def validate_c_syntax(content: str) -> bool:
    """Basic C syntax validation — checks for balanced curly braces.

    Returns ``True`` when the number of ``{`` equals the number of ``}``
    and the brace depth never goes negative (i.e. no ``}`` before its
    matching ``{``).

    **Validates: Requirements 5.5**
    """
    depth = 0
    for ch in content:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def is_whitespace_only_conflict(target_content: str, source_content: str) -> bool:
    """Return ``True`` when *target_content* and *source_content* differ only in whitespace.

    Whitespace differences include spaces, tabs, indentation, trailing
    whitespace, and line endings.  The comparison strips all whitespace
    from both strings before checking equality.

    **Validates: Requirements 3.2**
    """
    return _strip_all_whitespace(target_content) == _strip_all_whitespace(source_content)


def _strip_all_whitespace(s: str) -> str:
    """Remove all whitespace characters from *s*."""
    return re.sub(r"\s+", "", s)

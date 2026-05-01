"""Diff-aware validation helpers for PR review comments."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from scripts.models import ReviewFinding

_METHODOLOGY_PATTERNS = (
    re.compile(r"\bi ran\b", re.IGNORECASE),
    re.compile(r"\bi used\b", re.IGNORECASE),
    re.compile(r"\bgit (?:cat-file|grep|show|diff|log)\b", re.IGNORECASE),
    re.compile(r"\bthe diff shows\b", re.IGNORECASE),
)
_SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)


@dataclass(frozen=True)
class FileDiffMap:
    """Commentable lines for a changed file."""

    path: str
    added_lines: set[int] = field(default_factory=set)
    context_lines: set[int] = field(default_factory=set)
    deleted: bool = False
    renamed_from: str = ""

    @property
    def commentable_lines(self) -> set[int]:
        return self.added_lines | self.context_lines

    def is_commentable(self, line: int | None) -> bool:
        if line is None:
            return True
        if line <= 0:
            return False
        lines = self.commentable_lines
        return not lines or line in lines


def parse_diff_map(path: str, patch: str | None, *, status: str = "") -> FileDiffMap:
    """Parse a GitHub patch into new-file lines that can receive comments."""
    deleted = status == "removed"
    if not patch:
        return FileDiffMap(path=path, deleted=deleted)

    added: set[int] = set()
    context: set[int] = set()
    new_line: int | None = None
    for raw_line in patch.splitlines():
        if raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw_line)
            new_line = int(match.group(1)) if match else None
            continue
        if new_line is None:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            added.add(new_line)
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            continue
        else:
            context.add(new_line)
            new_line += 1

    return FileDiffMap(path=path, added_lines=added, context_lines=context, deleted=deleted)


def build_diff_maps(diff_scope: object) -> dict[str, FileDiffMap]:
    """Build commentable-line maps keyed by changed path."""
    maps: dict[str, FileDiffMap] = {}
    for changed_file in getattr(diff_scope, "files", []) or []:
        path = str(getattr(changed_file, "path", "") or "")
        if not path:
            continue
        maps[path] = parse_diff_map(
            path,
            getattr(changed_file, "patch", None),
            status=str(getattr(changed_file, "status", "") or ""),
        )
    return maps


def is_line_commentable(
    path: object,
    line: object,
    diff_maps: dict[str, FileDiffMap],
) -> bool:
    """Return whether a raw finding path/line can be published inline."""
    if line is None:
        return True
    try:
        parsed_line = int(line)
    except (TypeError, ValueError):
        return True
    if parsed_line <= 0:
        return False
    file_map = diff_maps.get(str(path or ""))
    if file_map is None:
        return False
    return file_map.is_commentable(parsed_line)


def validate_review_finding_for_publish(
    finding: ReviewFinding,
    diff_maps: dict[str, FileDiffMap],
) -> tuple[bool, str]:
    """Cheap deterministic self-check before publishing a review finding."""
    if finding.path not in diff_maps:
        return False, "path-not-in-diff"
    if finding.line is not None and not diff_maps[finding.path].is_commentable(finding.line):
        return False, "line-not-commentable"
    body = finding.body or ""
    if not body.strip():
        return False, "empty-body"
    if any(pattern.search(body) for pattern in _METHODOLOGY_PATTERNS):
        return False, "methodology-leak"
    if any(pattern.search(body) for pattern in _SECRET_PATTERNS):
        return False, "secret-like-text"
    if finding.severity in {"high", "critical"} and len(body.strip()) < 20:
        return False, "severity-without-rationale"
    return True, ""

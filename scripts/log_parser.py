"""Log parser router and protocol for CI failure log parsing."""

from __future__ import annotations

import logging
import re
from typing import Protocol

from scripts.models import ParsedFailure

logger = logging.getLogger(__name__)

RAW_EXCERPT_LINES = 2000

_ERROR_MARKERS = re.compile(
    r"(?:error:|Error:|FAILED|fatal:|FATAL|assertion failed|Traceback"
    r"|==\d+==ERROR|runtime error:|Invalid (?:read|write)|definitely lost"
    r"|\[err\]:|\[exception\]:|\[timeout\]:|Tcl error"
    r"|panic:|Aborted|Segmentation fault|SIGSEGV"
    r"|make(?:\[\d+\])?:\s+\*\*\*|undefined reference"
    r"|Process completed with exit code [1-9])",
    re.IGNORECASE,
)


def _extract_marker_excerpt(lines: list[str], limit: int) -> str | None:
    """Scan *lines* for error markers and return a context window."""
    marker_indices: list[int] = []
    for idx, line in enumerate(lines):
        if _ERROR_MARKERS.search(line):
            marker_indices.append(idx)

    if not marker_indices:
        return None

    cluster_start = marker_indices[0]
    cluster_end = marker_indices[0]
    for mi in marker_indices[1:]:
        if mi - cluster_end <= 20:
            cluster_end = mi
        else:
            break

    context_padding = 30
    region_start = max(0, cluster_start - context_padding)
    region_end = min(len(lines), cluster_end + context_padding + 1)
    marker_region = lines[region_start:region_end]

    tail_budget = limit - len(marker_region)
    if tail_budget > 0:
        tail_lines = lines[-tail_budget:]
        combined = list(marker_region)
        marker_region_set = set(range(region_start, region_end))
        tail_start = len(lines) - tail_budget
        for i, line in enumerate(tail_lines):
            abs_idx = tail_start + i
            if abs_idx not in marker_region_set:
                combined.append(line)
        return "\n".join(combined[:limit])
    return "\n".join(marker_region[:limit])


class LogParser(Protocol):
    """Protocol for individual log parsers."""

    def can_parse(self, log_content: str) -> bool: ...
    def parse(self, log_content: str) -> list[ParsedFailure]: ...


_CONDITION_EVAL_RE = re.compile(
    r"^Evaluating\s*(?::|[\w.-]+\.if)"
    r"|^Expanded\s*:"
    r"|^\(success\(\)"
    r"|^Result\s*:\s*(?:true|false)"
    r"|^Requested labels\s*:"
    r"|^Job defined at\s*:"
    r"|^Waiting for a runner to pick up"
    r"|^Job is (?:about to start running|waiting for a hosted runner)"
    r"|^\s*(?:true|false)\s*&&",
)
_TS_LINE_PREFIX_RE = re.compile(
    r"(?m)^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s*",
)


def is_workflow_condition_only(log_content: str) -> bool:
    """Return True when the log is only GitHub Actions workflow/lifecycle metadata.

    When a job's steps are all skipped via an ``if:`` condition, or the job
    failed before it could actually run, the log contains only GitHub's job
    startup / condition-evaluation output — lines like ``Evaluating: ...``,
    ``Expanded: ...``, ``Result: true``, ``Requested labels: ...``, ``Job
    defined at: ...``, ``Waiting for a runner to pick up this job...``.

    These are not actionable code failures and should be filtered upstream
    of parsing.
    """
    if not log_content:
        return True
    stripped = _TS_LINE_PREFIX_RE.sub("", log_content)
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if not lines:
        return True
    matched = sum(1 for ln in lines if _CONDITION_EVAL_RE.search(ln))
    # Treat as noise if ≥80% of non-empty lines are GitHub lifecycle text.
    return matched >= max(1, int(len(lines) * 0.8))


class LogParserRouter:
    """Tries registered parsers by priority; merges results from all matching parsers.

    An optional ``fallback_parser`` is invoked only when no deterministic
    parser matched. Use this for expensive (LLM-backed) extraction that
    should not run on every log.
    """

    def __init__(self, parsers: list[LogParser] | None = None) -> None:
        self._parsers: list[tuple[int, LogParser]] = []
        self._fallback_parser: LogParser | None = None
        if parsers:
            for p in parsers:
                self._parsers.append((100, p))

    def register(self, parser: LogParser, *, priority: int = 100) -> None:
        """Register a parser. Lower priority number = tried first.

        All matching parsers contribute results (not just the first match).
        """
        self._parsers.append((priority, parser))
        self._parsers.sort(key=lambda t: t[0])

    def set_fallback(self, parser: LogParser | None) -> None:
        """Install a fallback parser invoked only when all deterministic
        parsers miss. Pass ``None`` to remove the fallback.
        """
        self._fallback_parser = parser

    def parse(
        self,
        log_content: str,
        *,
        raw_excerpt_lines: int | None = None,
    ) -> tuple[list[ParsedFailure], str | None, bool]:
        """Parse log content using all matching parsers.

        Returns:
            (parsed_failures, raw_excerpt_or_none, is_unparseable)
        """
        all_failures: list[ParsedFailure] = []
        seen_ids: set[str] = set()
        matched_parsers: list[str] = []

        for _priority, parser in self._parsers:
            try:
                if parser.can_parse(log_content):
                    failures = parser.parse(log_content)
                    for f in failures:
                        if f.failure_identifier not in seen_ids:
                            seen_ids.add(f.failure_identifier)
                            all_failures.append(f)
                    if failures:
                        matched_parsers.append(type(parser).__name__)
            except Exception as exc:
                logger.warning("Parser %s raised: %s", type(parser).__name__, exc)
                continue

        if all_failures:
            logger.info(
                "Parsing complete: %s matched, %d failure(s) extracted.",
                "+".join(matched_parsers),
                len(all_failures),
            )
            return all_failures, None, False

        # No deterministic parser matched — try the AI fallback if configured.
        if self._fallback_parser is not None:
            try:
                if self._fallback_parser.can_parse(log_content):
                    failures = self._fallback_parser.parse(log_content)
                    for f in failures:
                        if f.failure_identifier not in seen_ids:
                            seen_ids.add(f.failure_identifier)
                            all_failures.append(f)
                    if all_failures:
                        logger.info(
                            "Parsing complete: AI fallback matched, %d failure(s) extracted.",
                            len(all_failures),
                        )
                        return all_failures, None, False
            except Exception as exc:
                logger.warning(
                    "Fallback parser %s raised: %s",
                    type(self._fallback_parser).__name__,
                    exc,
                )

        # No parser matched (including fallback) — return raw excerpt
        limit = raw_excerpt_lines if raw_excerpt_lines is not None else RAW_EXCERPT_LINES
        lines = log_content.splitlines()

        marker_excerpt = _extract_marker_excerpt(lines, limit)
        excerpt = marker_excerpt if marker_excerpt is not None else "\n".join(lines[-limit:])

        logger.warning(
            "Parsing complete: no parser matched, flagging as unparseable "
            "(returning up to %d lines).",
            limit,
        )
        return [], excerpt, True

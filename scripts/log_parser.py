"""Log parser router and protocol for CI failure log parsing."""

from __future__ import annotations

import logging
from typing import Protocol

from scripts.models import ParsedFailure

logger = logging.getLogger(__name__)

RAW_EXCERPT_LINES = 200


class LogParser(Protocol):
    """Protocol for individual log parsers."""

    def can_parse(self, log_content: str) -> bool: ...
    def parse(self, log_content: str) -> list[ParsedFailure]: ...


class LogParserRouter:
    """Tries registered parsers in order; falls back to raw excerpt."""

    def __init__(self, parsers: list[LogParser] | None = None) -> None:
        self._parsers: list[LogParser] = parsers or []

    def register(self, parser: LogParser) -> None:
        self._parsers.append(parser)

    def parse(self, log_content: str) -> tuple[list[ParsedFailure], str | None, bool]:
        """Parse log content.

        Returns:
            (parsed_failures, raw_excerpt_or_none, is_unparseable)
        """
        for parser in self._parsers:
            try:
                if parser.can_parse(log_content):
                    failures = parser.parse(log_content)
                    if failures:
                        logger.info(
                            "Parsing complete: %s matched, %d failure(s) extracted.",
                            type(parser).__name__, len(failures),
                        )
                        return failures, None, False
            except Exception as exc:
                logger.warning("Parser %s raised: %s", type(parser).__name__, exc)
                continue

        # No parser matched — return raw excerpt
        lines = log_content.splitlines()
        excerpt = "\n".join(lines[-RAW_EXCERPT_LINES:])
        logger.warning(
            "Parsing complete: no parser matched, flagging as unparseable "
            "(returning last %d lines).",
            min(len(lines), RAW_EXCERPT_LINES),
        )
        return [], excerpt, True

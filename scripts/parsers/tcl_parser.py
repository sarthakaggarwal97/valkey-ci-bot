"""Tcl runtest output parser."""

from __future__ import annotations

import re

from scripts.models import ParsedFailure

# Matches: [err]: Test description in tests/unit/foo.tcl
_ERR_RE = re.compile(
    r"^\[err\]:\s+(.+?)(?:\s+in\s+(\S+\.tcl))?$", re.MULTILINE | re.IGNORECASE
)
# Matches: Expected 'x' to equal 'y' (or similar assertion lines after [err])
_ASSERT_RE = re.compile(r"Expected\s+.+", re.IGNORECASE)


class TclTestParser:
    """Parses Tcl runtest [err]: patterns."""

    def can_parse(self, log_content: str) -> bool:
        return "[err]:" in log_content.lower() or "[err]" in log_content

    def parse(self, log_content: str) -> list[ParsedFailure]:
        failures: list[ParsedFailure] = []
        seen: set[str] = set()

        for m in _ERR_RE.finditer(log_content):
            description = m.group(1).strip()
            file_path = m.group(2) or ""
            identifier = description if not file_path else f"{file_path}::{description}"

            if identifier in seen:
                continue
            seen.add(identifier)

            # Look for assertion details in the lines following the error
            after = log_content[m.end(): m.end() + 500]
            assertion_details: str | None = None
            assert_match = _ASSERT_RE.search(after)
            if assert_match:
                assertion_details = assert_match.group(0).strip()

            failures.append(ParsedFailure(
                failure_identifier=identifier,
                test_name=description,
                file_path=file_path,
                error_message=description,
                assertion_details=assertion_details,
                line_number=None,
                stack_trace=None,
                parser_type="tcl",
            ))

        return failures

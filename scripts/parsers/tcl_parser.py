"""Tcl runtest output parser."""

from __future__ import annotations

import re

from scripts.models import ParsedFailure

# Matches: [err]: Test description in tests/unit/foo.tcl
_ERR_RE = re.compile(
    r"^\[err\]:\s+(.+?)(?:\s+in\s+(\S+\.tcl))?$", re.MULTILINE | re.IGNORECASE
)
# Matches the final runtest summary block, for example:
# *** [TIMEOUT]: Fix cluster in tests/unit/cluster/many-slot-migration.tcl
_SUMMARY_FAILURE_RE = re.compile(
    r"^\*{3}\s+\[([A-Z_ -]+)\]:\s+(.+?)\s+in\s+(\S+\.tcl)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
# Matches: Expected 'x' to equal 'y' (or similar assertion lines after [err])
_ASSERT_RE = re.compile(r"Expected\s+.+", re.IGNORECASE)


class TclTestParser:
    """Parses Tcl runtest [err]: patterns."""

    def can_parse(self, log_content: str) -> bool:
        return bool(
            _ERR_RE.search(log_content) or _SUMMARY_FAILURE_RE.search(log_content)
        )

    def parse(self, log_content: str) -> list[ParsedFailure]:
        failures: list[ParsedFailure] = []
        seen: set[str] = set()

        def append_failure(
            *,
            description: str,
            file_path: str,
            error_message: str,
            assertion_details: str | None = None,
        ) -> None:
            identifier = description if not file_path else f"{file_path}::{description}"
            if identifier in seen:
                return
            seen.add(identifier)

            failures.append(ParsedFailure(
                failure_identifier=identifier,
                test_name=description,
                file_path=file_path,
                error_message=error_message,
                assertion_details=assertion_details,
                line_number=None,
                stack_trace=None,
                parser_type="tcl",
            ))

        for m in _ERR_RE.finditer(log_content):
            description = m.group(1).strip()
            file_path = m.group(2) or ""

            # Look for assertion details in the lines following the error
            after = log_content[m.end(): m.end() + 500]
            assertion_details: str | None = None
            assert_match = _ASSERT_RE.search(after)
            if assert_match:
                assertion_details = assert_match.group(0).strip()

            append_failure(
                description=description,
                file_path=file_path,
                error_message=description,
                assertion_details=assertion_details,
            )

        for m in _SUMMARY_FAILURE_RE.finditer(log_content):
            status = m.group(1).strip().upper()
            description = m.group(2).strip()
            file_path = m.group(3)

            append_failure(
                description=description,
                file_path=file_path,
                error_message=f"[{status}]: {description}",
                assertion_details=f"Runtest summary status: {status}",
            )

        return failures

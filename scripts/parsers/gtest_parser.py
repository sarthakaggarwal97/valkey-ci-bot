"""Google Test output parser."""

from __future__ import annotations

import re

from scripts.models import ParsedFailure

# Matches: [  FAILED  ] TestSuite.TestName
_FAILED_RE = re.compile(r"^\[\s+FAILED\s+\]\s+(\S+)", re.MULTILINE)
# Matches: path/to/file.cc:123: Failure
_LOCATION_RE = re.compile(r"^(\S+?):(\d+):\s+Failure", re.MULTILINE)
# Matches: Expected: ... / Actual: ...
_ASSERTION_RE = re.compile(
    r"(Expected:.*?(?:\n\s+Actual:.*?)?)", re.MULTILINE | re.DOTALL
)


class GTestParser:
    """Parses Google Test [FAILED] patterns."""

    def can_parse(self, log_content: str) -> bool:
        return "[  FAILED  ]" in log_content

    def parse(self, log_content: str) -> list[ParsedFailure]:
        failures: list[ParsedFailure] = []
        seen: set[str] = set()

        for m in _FAILED_RE.finditer(log_content):
            test_name = m.group(1)
            if test_name in seen:
                continue
            seen.add(test_name)

            # Search backwards from the FAILED line for location info
            preceding = log_content[: m.start()]
            file_path = ""
            line_number: int | None = None
            assertion_details: str | None = None

            loc_matches = list(_LOCATION_RE.finditer(preceding))
            if loc_matches:
                last_loc = loc_matches[-1]
                file_path = last_loc.group(1)
                line_number = int(last_loc.group(2))

                # Look for assertion details after the location
                after_loc = preceding[last_loc.end():]
                assertion_match = _ASSERTION_RE.search(after_loc)
                if assertion_match:
                    assertion_details = assertion_match.group(1).strip()

            failures.append(ParsedFailure(
                failure_identifier=test_name,
                test_name=test_name,
                file_path=file_path,
                error_message=f"Test {test_name} failed",
                assertion_details=assertion_details,
                line_number=line_number,
                stack_trace=None,
                parser_type="gtest",
            ))

        return failures

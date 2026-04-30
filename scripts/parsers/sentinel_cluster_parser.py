"""Sentinel and cluster test failure parser."""

from __future__ import annotations

import re

from scripts.models import ParsedFailure

# Optional ISO-8601 timestamp that GitHub Actions prepends to every log line.
_TS_PREFIX = r"(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s+)?"

# Sentinel/cluster tests use the same Tcl [err] pattern but may also have:
# Caught [err]: ... in tests/sentinel/foo.tcl
# or cluster-specific patterns
_SENTINEL_ERR_RE = re.compile(
    rf"^{_TS_PREFIX}\[err\]:\s+(.+?)"
    rf"(?:\s+in\s+(tests/(?:sentinel|cluster|integration)/\S+\.tcl))?"
    rf"\s*(?:\(\d+\s*ms\))?\s*$",
    re.MULTILINE | re.IGNORECASE,
)
# Cluster test specific: "FAIL: <test description>"
_CLUSTER_FAIL_RE = re.compile(
    rf"^{_TS_PREFIX}FAIL:\s+(.+?)(?:\s+in\s+(\S+\.tcl))?\s*$", re.MULTILINE
)


class SentinelClusterParser:
    """Parses sentinel/cluster test failure patterns."""

    def can_parse(self, log_content: str) -> bool:
        return bool(
            _SENTINEL_ERR_RE.search(log_content)
            or _CLUSTER_FAIL_RE.search(log_content)
        )

    def parse(self, log_content: str) -> list[ParsedFailure]:
        failures: list[ParsedFailure] = []
        seen: set[str] = set()

        for pattern, parser_type in [
            (_SENTINEL_ERR_RE, "sentinel"),
            (_CLUSTER_FAIL_RE, "cluster"),
        ]:
            for m in pattern.finditer(log_content):
                description = m.group(1).strip()
                file_path = m.group(2) or ""

                # Determine parser_type from file path if possible
                actual_type = parser_type
                if "sentinel" in file_path:
                    actual_type = "sentinel"
                elif "cluster" in file_path:
                    actual_type = "cluster"

                identifier = (
                    f"{file_path}::{description}" if file_path else description
                )
                if identifier in seen:
                    continue
                seen.add(identifier)

                failures.append(ParsedFailure(
                    failure_identifier=identifier,
                    test_name=description,
                    file_path=file_path,
                    error_message=description,
                    assertion_details=None,
                    line_number=None,
                    stack_trace=None,
                    parser_type=actual_type,
                ))

        return failures

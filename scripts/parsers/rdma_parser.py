"""RDMA test failure parser."""

from __future__ import annotations

import re

from scripts.models import ParsedFailure

# Optional ISO-8601 timestamp that GitHub Actions prepends to every log line.
_TS_PREFIX = r"(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s+)?"

# RDMA-specific [err]: lines. Require the path to be RDMA-flavored
# (tests/**/rdma* or tests/**/*rdma*.tcl) so this parser doesn't grab
# generic TCL failures just because the log mentions RDMA in passing.
_RDMA_ERR_RE = re.compile(
    rf"^{_TS_PREFIX}\[err\]:\s+(.+?)"
    rf"\s+in\s+(tests/\S*rdma[^/\s]*\.tcl|tests/\S*/rdma[-_]\S*\.tcl)"
    rf"\s*(?:\(\d+\s*ms\))?\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_RDMA_FAIL_RE = re.compile(
    r"(?:RDMA|rdma).*(?:failed|error|timeout|refused)", re.IGNORECASE,
)
# Match RDMA connection errors. Require the error phrase to mention
# RDMA/InfiniBand ('ibv_*' / 'rdma_*') explicitly, so generic TCP
# 'Connection refused' messages don't trigger this parser.
_RDMA_CONN_RE = re.compile(
    r"(rdma_connect\s+failed|ibv_\w+\s+(?:failed|error)"
    r"|(?:RDMA|InfiniBand)[^\n]*?connection\s+refused)",
    re.IGNORECASE,
)


class RdmaParser:
    """Parses RDMA test failure output from runtest-rdma."""

    def can_parse(self, log_content: str) -> bool:
        # Only claim the log if we have either (a) an [err]: line with an
        # RDMA-specific test path, or (b) an actual RDMA protocol/connection
        # error phrase. Merely mentioning "rdma" elsewhere in a large test
        # log is not enough — generic TCL failures should fall through to
        # TclTestParser.
        return bool(
            _RDMA_ERR_RE.search(log_content)
            or _RDMA_CONN_RE.search(log_content)
        )

    def parse(self, log_content: str) -> list[ParsedFailure]:
        failures: list[ParsedFailure] = []
        seen: set[str] = set()

        for m in _RDMA_ERR_RE.finditer(log_content):
            desc = m.group(1).strip()
            file_path = m.group(2) or ""
            ident = f"rdma:{file_path}::{desc}" if file_path else f"rdma:{desc}"
            if ident in seen:
                continue
            seen.add(ident)
            failures.append(ParsedFailure(
                failure_identifier=ident,
                test_name=desc,
                file_path=file_path,
                error_message=desc,
                assertion_details=None,
                line_number=None,
                stack_trace=None,
                parser_type="rdma",
            ))

        if not failures:
            # Deduplicate by the error phrase only (not timestamps/PIDs)
            conn_errors: set[str] = set()
            for m in _RDMA_CONN_RE.finditer(log_content):
                phrase = m.group(1).strip().lower()
                conn_errors.add(phrase)

            for phrase in sorted(conn_errors):
                ident = f"rdma:{phrase}"
                if ident in seen:
                    continue
                seen.add(ident)
                failures.append(ParsedFailure(
                    failure_identifier=ident,
                    test_name=None,
                    file_path="",
                    error_message=f"RDMA: {phrase}",
                    assertion_details=None,
                    line_number=None,
                    stack_trace=None,
                    parser_type="rdma",
                ))

        return failures

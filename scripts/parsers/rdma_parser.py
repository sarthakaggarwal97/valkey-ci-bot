"""RDMA test failure parser."""

from __future__ import annotations

import re

from scripts.models import ParsedFailure

# RDMA-specific patterns from runtest-rdma
_RDMA_ERR_RE = re.compile(
    r"^\[err\]:\s+(.+?)(?:\s+in\s+(\S+))?$", re.MULTILINE | re.IGNORECASE,
)
_RDMA_FAIL_RE = re.compile(
    r"(?:RDMA|rdma).*(?:failed|error|timeout|refused)", re.IGNORECASE,
)
_RDMA_CONN_RE = re.compile(
    r"(?:connection\s+refused|rdma_connect|ibv_\w+)\s*.*(?:fail|error)",
    re.IGNORECASE,
)


class RdmaParser:
    """Parses RDMA test failure output from runtest-rdma."""

    def can_parse(self, log_content: str) -> bool:
        return "rdma" in log_content.lower() and bool(
            _RDMA_ERR_RE.search(log_content)
            or _RDMA_FAIL_RE.search(log_content)
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
            for m in _RDMA_CONN_RE.finditer(log_content):
                msg = m.group(0).strip()
                ident = f"rdma-conn:{msg[:80]}"
                if ident in seen:
                    continue
                seen.add(ident)
                failures.append(ParsedFailure(
                    failure_identifier=ident,
                    test_name=None,
                    file_path="",
                    error_message=f"RDMA: {msg}",
                    assertion_details=None,
                    line_number=None,
                    stack_trace=None,
                    parser_type="rdma",
                ))

        return failures

"""Module API test failure parser."""

from __future__ import annotations

import re

from scripts.models import ParsedFailure

# Optional ISO-8601 timestamp that GitHub Actions prepends to every log line.
_TS_PREFIX = r"(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s+)?"

# Module load/unload failures
_MODULE_LOAD_RE = re.compile(
    r"(?:Error|Failed)\s+(?:loading|unloading)\s+(?:module|shared object)\s*[:\s]+(\S+)",
    re.IGNORECASE,
)
# Module API assertion: serverAssert / redisAssert / ValkeyModule_Assert
_MODULE_ASSERT_RE = re.compile(
    r"((?:server|redis|ValkeyModule_?)Assert(?:WithInfo)?)\s*\(\s*(.+?)\s*\)"
    r"(?:\s+in\s+(\S+?):(\d+))?",
    re.IGNORECASE,
)
# Module test [err] patterns (Tcl-based but module-specific paths)
_MODULE_ERR_RE = re.compile(
    rf"^{_TS_PREFIX}\[err\]:\s+(.+?)"
    rf"(?:\s+in\s+(tests/(?:modules|unit/moduleapi)/\S+\.tcl))?"
    rf"\s*(?:\(\d+\s*ms\))?\s*$",
    re.MULTILINE | re.IGNORECASE,
)
# Module crash: "Module ... caused a crash"
_MODULE_CRASH_RE = re.compile(
    r"[Mm]odule\s+(\S+)\s+(?:caused|triggered)\s+(?:a\s+)?(?:crash|segfault|signal)",
)


class ModuleApiParser:
    """Parses module API test failures from runtest-moduleapi."""

    def can_parse(self, log_content: str) -> bool:
        return bool(
            _MODULE_ERR_RE.search(log_content)
            or _MODULE_LOAD_RE.search(log_content)
            or _MODULE_ASSERT_RE.search(log_content)
            or _MODULE_CRASH_RE.search(log_content)
        )

    def parse(self, log_content: str) -> list[ParsedFailure]:
        failures: list[ParsedFailure] = []
        seen: set[str] = set()

        for m in _MODULE_ERR_RE.finditer(log_content):
            desc = m.group(1).strip()
            file_path = m.group(2) or ""
            ident = f"{file_path}::{desc}" if file_path else f"module:{desc}"
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
                parser_type="module",
            ))

        for m in _MODULE_LOAD_RE.finditer(log_content):
            module_path = m.group(1).strip()
            ident = f"module-load:{module_path}"
            if ident in seen:
                continue
            seen.add(ident)
            failures.append(ParsedFailure(
                failure_identifier=ident,
                test_name=None,
                file_path=module_path,
                error_message=f"Module load failure: {module_path}",
                assertion_details=None,
                line_number=None,
                stack_trace=None,
                parser_type="module",
            ))

        for m in _MODULE_ASSERT_RE.finditer(log_content):
            assert_fn = m.group(1)
            condition = m.group(2).strip()
            file_path = m.group(3) or ""
            line_number = int(m.group(4)) if m.group(4) else None
            ident = f"module-assert:{file_path}:{line_number}:{condition[:50]}"
            if ident in seen:
                continue
            seen.add(ident)
            failures.append(ParsedFailure(
                failure_identifier=ident,
                test_name=assert_fn,
                file_path=file_path,
                error_message=f"{assert_fn}({condition})",
                assertion_details=condition,
                line_number=line_number,
                stack_trace=None,
                parser_type="module",
            ))

        for m in _MODULE_CRASH_RE.finditer(log_content):
            module_name = m.group(1)
            ident = f"module-crash:{module_name}"
            if ident in seen:
                continue
            seen.add(ident)
            failures.append(ParsedFailure(
                failure_identifier=ident,
                test_name=None,
                file_path="",
                error_message=f"Module {module_name} crashed",
                assertion_details=None,
                line_number=None,
                stack_trace=None,
                parser_type="module",
            ))

        return failures

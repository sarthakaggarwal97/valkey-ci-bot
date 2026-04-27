"""Compiler and linker error parser for gcc/clang output."""

from __future__ import annotations

import re

from scripts.models import ParsedFailure

# Matches: src/foo.c:42:10: error: some message
# Also matches warnings promoted to errors via -Werror
_ERROR_RE = re.compile(
    r"^(\S+?):(\d+):(\d+):\s+(error|fatal error):\s+(.+)$", re.MULTILINE
)
_WERROR_RE = re.compile(
    r"^(\S+?):(\d+):(\d+):\s+warning:\s+(.+?)\s+\[-Werror", re.MULTILINE
)
# Linker errors: clang: error: linker command failed ...
# or: /usr/bin/ld: ... error ...
_LINKER_ERROR_RE = re.compile(
    r"^(clang|gcc|cc|c\+\+|g\+\+|ld|/usr/bin/ld):\s+(error:\s+.+|.+?:\s+.+(?:not an object|undefined reference|multiple definition|cannot find).*)$",
    re.MULTILINE,
)
# make error: make[N]: *** [target] Error N
_MAKE_ERROR_RE = re.compile(
    r"^make(?:\[\d+\])?:\s+\*\*\*\s+\[([^\]]+)\]\s+Error\s+(\d+)$", re.MULTILINE
)


class BuildErrorParser:
    """Parses gcc/clang file:line:col: error: patterns and linker errors."""

    def can_parse(self, log_content: str) -> bool:
        return bool(
            _ERROR_RE.search(log_content)
            or _WERROR_RE.search(log_content)
            or _LINKER_ERROR_RE.search(log_content)
        )

    def parse(self, log_content: str) -> list[ParsedFailure]:
        failures: list[ParsedFailure] = []
        seen: set[str] = set()

        for m in _ERROR_RE.finditer(log_content):
            file_path = m.group(1)
            line_num = int(m.group(2))
            message = m.group(5).strip()
            identifier = f"build:{file_path}:{line_num}"

            if identifier in seen:
                continue
            seen.add(identifier)

            failures.append(ParsedFailure(
                failure_identifier=identifier,
                test_name=None,
                file_path=file_path,
                error_message=message,
                assertion_details=None,
                line_number=line_num,
                stack_trace=None,
                parser_type="build",
            ))

        for m in _WERROR_RE.finditer(log_content):
            file_path = m.group(1)
            line_num = int(m.group(2))
            message = m.group(4).strip()
            identifier = f"build:{file_path}:{line_num}"

            if identifier in seen:
                continue
            seen.add(identifier)

            failures.append(ParsedFailure(
                failure_identifier=identifier,
                test_name=None,
                file_path=file_path,
                error_message=message,
                assertion_details=None,
                line_number=line_num,
                stack_trace=None,
                parser_type="build",
            ))

        for m in _LINKER_ERROR_RE.finditer(log_content):
            tool = m.group(1)
            message = m.group(2).strip()
            identifier = f"linker:{tool}:{message[:80]}"

            if identifier in seen:
                continue
            seen.add(identifier)

            # Try to extract a file path from the message
            file_path = ""
            file_match = re.search(r'(\S+\.(?:a|o|so|c|cpp|h))', message)
            if file_match:
                file_path = file_match.group(1)

            failures.append(ParsedFailure(
                failure_identifier=identifier,
                test_name=None,
                file_path=file_path,
                error_message=f"{tool}: {message}",
                assertion_details=None,
                line_number=None,
                stack_trace=None,
                parser_type="build",
            ))

        return failures

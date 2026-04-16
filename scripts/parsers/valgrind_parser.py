"""Valgrind output parser."""

from __future__ import annotations

import re

from scripts.models import ParsedFailure

# ==PID== Invalid read of size N
_VALGRIND_ERROR_RE = re.compile(
    r"^==\d+==\s+(Invalid (?:read|write|free)|Conditional jump|"
    r"Use of uninitialised|Syscall param|Mismatched free)(.+?)$",
    re.MULTILINE,
)
# ==PID==    at 0xADDR: func (file.c:42)
_VALGRIND_FRAME_RE = re.compile(
    r"==\d+==\s+(?:at|by)\s+\S+:\s+(\S+)\s+\((\S+?):(\d+)\)", re.MULTILINE,
)
# ==PID== ERROR SUMMARY: N errors from M contexts
_VALGRIND_SUMMARY_RE = re.compile(
    r"==\d+==\s+ERROR SUMMARY:\s+(\d+)\s+errors", re.MULTILINE,
)
# ==PID== definitely lost: N bytes in M blocks
_VALGRIND_LEAK_RE = re.compile(
    r"==\d+==\s+(definitely|indirectly|possibly) lost:\s+([\d,]+)\s+bytes",
    re.MULTILINE,
)


class ValgrindParser:
    """Parses Valgrind memory error and leak output."""

    def can_parse(self, log_content: str) -> bool:
        return bool(
            _VALGRIND_ERROR_RE.search(log_content)
            or _VALGRIND_LEAK_RE.search(log_content)
        )

    def parse(self, log_content: str) -> list[ParsedFailure]:
        failures: list[ParsedFailure] = []
        seen: set[str] = set()

        for m in _VALGRIND_ERROR_RE.finditer(log_content):
            error_type = m.group(1).strip()
            detail = m.group(2).strip()
            after = log_content[m.end():m.end() + 1500]
            file_path, line_number, func = "", None, None

            frame = _VALGRIND_FRAME_RE.search(after)
            if frame:
                func = frame.group(1)
                file_path = frame.group(2)
                line_number = int(frame.group(3))

            stack = self._extract_stack(after)
            ident = f"valgrind:{error_type}:{file_path}:{line_number}"
            if ident in seen:
                continue
            seen.add(ident)

            failures.append(ParsedFailure(
                failure_identifier=ident,
                test_name=func,
                file_path=file_path,
                error_message=f"Valgrind: {error_type} {detail}".strip(),
                assertion_details=f"Function: {func}" if func else None,
                line_number=line_number,
                stack_trace=stack,
                parser_type="valgrind",
            ))

        for m in _VALGRIND_LEAK_RE.finditer(log_content):
            kind = m.group(1)
            byte_count = m.group(2).replace(",", "")
            if int(byte_count) == 0:
                continue
            after = log_content[m.end():m.end() + 1500]
            file_path, line_number, func = "", None, None
            frame = _VALGRIND_FRAME_RE.search(after)
            if frame:
                func = frame.group(1)
                file_path = frame.group(2)
                line_number = int(frame.group(3))

            ident = f"valgrind-leak:{kind}:{file_path}:{line_number}"
            if ident in seen:
                continue
            seen.add(ident)

            failures.append(ParsedFailure(
                failure_identifier=ident,
                test_name=func,
                file_path=file_path,
                error_message=f"Valgrind: {kind} lost {byte_count} bytes",
                assertion_details=None,
                line_number=line_number,
                stack_trace=self._extract_stack(after),
                parser_type="valgrind",
            ))

        return failures

    @staticmethod
    def _extract_stack(text: str) -> str | None:
        frames = _VALGRIND_FRAME_RE.findall(text)
        if not frames:
            return None
        return "\n".join(f"  {f[0]} at {f[1]}:{f[2]}" for f in frames[:10])

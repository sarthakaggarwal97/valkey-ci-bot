"""AddressSanitizer and UndefinedBehaviorSanitizer output parser."""

from __future__ import annotations

import re

from scripts.models import ParsedFailure

# ==PID==ERROR: AddressSanitizer: heap-buffer-overflow on address ...
_ASAN_ERROR_RE = re.compile(
    r"==\d+==ERROR:\s+(AddressSanitizer|LeakSanitizer):\s+(.+?)(?:\s+on\s+address\s+\S+)?$",
    re.MULTILINE,
)
# #0 0xaddr in func_name file.c:42
_ASAN_FRAME_RE = re.compile(
    r"#\d+\s+\S+\s+in\s+(\S+)\s+(\S+?):(\d+)", re.MULTILINE,
)
# runtime error: signed integer overflow: ...
_UBSAN_RE = re.compile(
    r"^(\S+?):(\d+):\d+:\s+runtime error:\s+(.+)$", re.MULTILINE,
)
# SUMMARY: *Sanitizer: error_type file.c:line ...
_SUMMARY_RE = re.compile(
    r"SUMMARY:\s+\w+Sanitizer:\s+(\S+)\s+(\S+?):(\d+)", re.MULTILINE,
)


class SanitizerParser:
    """Parses ASAN/UBSan/LeakSanitizer output."""

    def can_parse(self, log_content: str) -> bool:
        return bool(
            _ASAN_ERROR_RE.search(log_content)
            or _UBSAN_RE.search(log_content)
            or _SUMMARY_RE.search(log_content)
        )

    def parse(self, log_content: str) -> list[ParsedFailure]:
        failures: list[ParsedFailure] = []
        seen: set[str] = set()

        for m in _ASAN_ERROR_RE.finditer(log_content):
            sanitizer = m.group(1)
            error_type = m.group(2).strip()
            file_path, line_number, func = "", None, None

            after = log_content[m.end():m.end() + 2000]
            frame = _ASAN_FRAME_RE.search(after)
            if frame:
                func = frame.group(1)
                file_path = frame.group(2)
                line_number = int(frame.group(3))

            stack = self._extract_stack(after)
            ident = f"{sanitizer}:{error_type}:{file_path}:{line_number}"
            if ident in seen:
                continue
            seen.add(ident)

            failures.append(ParsedFailure(
                failure_identifier=ident,
                test_name=func,
                file_path=file_path,
                error_message=f"{sanitizer}: {error_type}",
                assertion_details=f"Function: {func}" if func else None,
                line_number=line_number,
                stack_trace=stack,
                parser_type="sanitizer",
            ))

        for m in _UBSAN_RE.finditer(log_content):
            file_path = m.group(1)
            line_number = int(m.group(2))
            message = m.group(3).strip()
            ident = f"ubsan:{file_path}:{line_number}"
            if ident in seen:
                continue
            seen.add(ident)

            failures.append(ParsedFailure(
                failure_identifier=ident,
                test_name=None,
                file_path=file_path,
                error_message=f"UBSan: {message}",
                assertion_details=None,
                line_number=line_number,
                stack_trace=None,
                parser_type="sanitizer",
            ))

        if not failures:
            for m in _SUMMARY_RE.finditer(log_content):
                error_type = m.group(1)
                file_path = m.group(2)
                line_number = int(m.group(3))
                ident = f"sanitizer-summary:{file_path}:{line_number}"
                if ident in seen:
                    continue
                seen.add(ident)
                failures.append(ParsedFailure(
                    failure_identifier=ident,
                    test_name=None,
                    file_path=file_path,
                    error_message=f"Sanitizer: {error_type}",
                    assertion_details=None,
                    line_number=line_number,
                    stack_trace=None,
                    parser_type="sanitizer",
                ))

        return failures

    @staticmethod
    def _extract_stack(text: str) -> str | None:
        frames = _ASAN_FRAME_RE.findall(text)
        if not frames:
            return None
        return "\n".join(f"  #{i} {f[0]} at {f[1]}:{f[2]}" for i, f in enumerate(frames[:10]))

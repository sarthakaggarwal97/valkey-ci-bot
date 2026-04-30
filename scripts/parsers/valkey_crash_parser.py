"""Parser for Valkey server crashes (signals, stack traces, assertions).

Valkey's ``sigsegvHandler`` emits a well-structured crash report when the
server crashes from a signal. The report looks like::

    === REDIS BUG REPORT START: Cut & paste starting from here ===
    24782:M 24 Mar 2026 05:30:40.101 # Valkey 8.2.0 crashed by signal: 11, si_code: 1
    24782:M 24 Mar 2026 05:30:40.101 # Crashed running the instruction at: 0x5555556a1234
    ...
    ------ STACK TRACE ------
    Backtrace:
    /path/to/valkey-server(+0xa1234) [0x5555556a1234]
    ...
    === REDIS BUG REPORT END ===

serverAssert failures emit a similar report::

    === ASSERTION FAILED ===
    ==> src/t_hash.c:842 'h != NULL' is not true

Both are strong structured signals we want to extract as ``ParsedFailure``.
"""

from __future__ import annotations

import re

from scripts.models import ParsedFailure

# "Valkey <version> crashed by signal: N" or legacy "Redis <version> crashed by signal: N"
_CRASH_SIGNAL_RE = re.compile(
    r"(?:Valkey|Redis)\s+(\S+)\s+crashed\s+by\s+signal:\s+(\d+)"
    r"(?:,\s+si_code:\s+(\d+))?",
    re.IGNORECASE,
)

# "=== ASSERTION FAILED ===" followed by "==> file:line 'expr' is not true"
_ASSERT_HEADER_RE = re.compile(
    r"={3,}\s*ASSERTION\s+FAILED\s*={3,}", re.IGNORECASE,
)
_ASSERT_DETAIL_RE = re.compile(
    r"==>\s+(\S+?):(\d+)\s+'([^']+)'\s+is\s+not\s+true",
    re.IGNORECASE,
)

# Bare serverAssert/redisAssert macro output without the === banner:
# "serverAssert in foo.c:42 -> assertion 'x == 0' failed"
_BARE_ASSERT_RE = re.compile(
    r"(?:server|redis|valkey)Assert(?:WithInfo)?\s+"
    r"(?:in\s+)?(\S+?):(\d+)\s*->\s*assertion\s+'([^']+)'",
    re.IGNORECASE,
)

# Stack trace frame: "valkey-server(funcname+0xoff) [0xaddr]" or
# "/path/to/file(funcname+0xoff) [0xaddr]".
_STACK_FRAME_RE = re.compile(
    r"^(?:\S+\s+)?\S*(?:valkey-server|redis-server|valkey-sentinel|redis-sentinel)"
    r"(?:\([\w.+_-]*\+0x[0-9a-f]+\))?\s*\[0x[0-9a-f]+\]",
    re.MULTILINE | re.IGNORECASE,
)


class ValkeyCrashParser:
    """Parses Valkey server-process crash reports (signal + assertion)."""

    def can_parse(self, log_content: str) -> bool:
        return bool(
            _CRASH_SIGNAL_RE.search(log_content)
            or _ASSERT_HEADER_RE.search(log_content)
            or _BARE_ASSERT_RE.search(log_content)
        )

    def parse(self, log_content: str) -> list[ParsedFailure]:
        failures: list[ParsedFailure] = []
        seen: set[str] = set()

        # Signal-based crashes
        for m in _CRASH_SIGNAL_RE.finditer(log_content):
            version = m.group(1).strip()
            signal_num = m.group(2)
            si_code = m.group(3) or ""

            # Grab a short stack trace window after the crash marker.
            after = log_content[m.end(): m.end() + 4000]
            stack = self._extract_stack(after)

            signal_name = _SIGNAL_NAMES.get(signal_num, f"signal {signal_num}")
            ident = f"crash:signal-{signal_num}:{version}"
            if ident in seen:
                continue
            seen.add(ident)

            failures.append(ParsedFailure(
                failure_identifier=ident,
                test_name=None,
                file_path="",
                error_message=(
                    f"Valkey {version} crashed: {signal_name}"
                    + (f" (si_code={si_code})" if si_code else "")
                ),
                assertion_details=None,
                line_number=None,
                stack_trace=stack,
                parser_type="crash",
            ))

        # Assertion-banner crashes
        for m in _ASSERT_HEADER_RE.finditer(log_content):
            after = log_content[m.end(): m.end() + 2000]
            detail = _ASSERT_DETAIL_RE.search(after)
            if not detail:
                continue
            file_path = detail.group(1)
            line_number = int(detail.group(2))
            expr = detail.group(3)

            ident = f"assert:{file_path}:{line_number}"
            if ident in seen:
                continue
            seen.add(ident)

            stack = self._extract_stack(after[detail.end():])
            failures.append(ParsedFailure(
                failure_identifier=ident,
                test_name=None,
                file_path=file_path,
                error_message=f"Assertion failed: {expr!r} in {file_path}:{line_number}",
                assertion_details=expr,
                line_number=line_number,
                stack_trace=stack,
                parser_type="crash",
            ))

        # Bare serverAssert output (no banner)
        for m in _BARE_ASSERT_RE.finditer(log_content):
            file_path = m.group(1)
            line_number = int(m.group(2))
            expr = m.group(3)

            ident = f"assert:{file_path}:{line_number}"
            if ident in seen:
                continue
            seen.add(ident)

            failures.append(ParsedFailure(
                failure_identifier=ident,
                test_name=None,
                file_path=file_path,
                error_message=f"Assertion failed: {expr!r} in {file_path}:{line_number}",
                assertion_details=expr,
                line_number=line_number,
                stack_trace=None,
                parser_type="crash",
            ))

        return failures

    @staticmethod
    def _extract_stack(text: str) -> str | None:
        frames = _STACK_FRAME_RE.findall(text)
        if not frames:
            return None
        # First 10 frames, deduplicated in order
        seen: set[str] = set()
        picked: list[str] = []
        for f in frames:
            if f in seen:
                continue
            seen.add(f)
            picked.append(f.strip())
            if len(picked) >= 10:
                break
        return "\n".join(picked) if picked else None


# Common POSIX signal names — keep small, add on demand.
_SIGNAL_NAMES: dict[str, str] = {
    "4": "SIGILL (illegal instruction)",
    "6": "SIGABRT (abort / assertion)",
    "7": "SIGBUS (bus error)",
    "8": "SIGFPE (arithmetic error)",
    "9": "SIGKILL",
    "11": "SIGSEGV (segmentation fault)",
    "13": "SIGPIPE",
    "14": "SIGALRM",
    "15": "SIGTERM",
}

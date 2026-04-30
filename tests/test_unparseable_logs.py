# Feature: valkey-ci-agent, Property 4: Unparseable logs produce raw excerpt
"""Property-based tests for unparseable log handling.

Validates: Requirements 2.5

For any log content that does not match any supported parser format, the parser
router should return a result flagged as "unparseable" containing exactly the
last RAW_EXCERPT_LINES lines of the log.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from scripts.log_parser import RAW_EXCERPT_LINES, LogParserRouter
from scripts.parsers.build_error_parser import BuildErrorParser
from scripts.parsers.gtest_parser import GTestParser
from scripts.parsers.sentinel_cluster_parser import SentinelClusterParser
from scripts.parsers.tcl_parser import TclTestParser


def _make_router() -> LogParserRouter:
    """Create a router with all 4 real parsers registered."""
    router = LogParserRouter()
    router.register(GTestParser())
    router.register(TclTestParser())
    router.register(BuildErrorParser())
    router.register(SentinelClusterParser())
    return router


# ---------------------------------------------------------------------------
# Strategy: generate safe log lines that do NOT trigger any parser
# ---------------------------------------------------------------------------
# Avoids all parser trigger patterns:
#   - No "["           (GTest "[  FAILED  ]", Tcl/Sentinel "[err]")
#   - No "FAIL:"       (Cluster)
#   - No "error:"      (Build errors)
#   - No "[-Werror"    (Build -Werror warnings)
_SAFE_ALPHABET = "ABCDEGHIJKLMNOPQRSTUVWXYZabcdghjkmnopqstuvwxyz0123456789 =+.,;#@!?$%&*/"
_safe_line = st.text(alphabet=_SAFE_ALPHABET, min_size=1, max_size=80)


@st.composite
def unparseable_log(draw: st.DrawFn, min_lines: int = 0, max_lines: int | None = None) -> str:
    """Generate a multi-line log that no parser can match.

    Each line has at least 1 character so that splitlines() returns the
    expected number of items regardless of trailing-newline edge cases.
    """
    if max_lines is None:
        max_lines = RAW_EXCERPT_LINES * 2
    num_lines = draw(st.integers(min_value=min_lines, max_value=max_lines))
    lines = draw(st.lists(_safe_line, min_size=num_lines, max_size=num_lines))
    return "\n".join(lines)


class TestUnparseableLogsProperty:
    """Property 4: Unparseable logs produce raw excerpt.

    **Validates: Requirements 2.5**
    """

    @given(log=unparseable_log(min_lines=0, max_lines=RAW_EXCERPT_LINES - 1))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_short_log_returns_all_lines_as_excerpt(self, log: str) -> None:
        """Logs shorter than RAW_EXCERPT_LINES return the entire log as excerpt."""
        router = _make_router()
        failures, excerpt, is_unparseable = router.parse(log)

        assert failures == [], "No parsed failures expected for unparseable log"
        assert is_unparseable is True, "Should be flagged as unparseable"

        # The router joins all lines with "\n"
        original_lines = log.splitlines()
        expected_excerpt = "\n".join(original_lines)
        assert excerpt == expected_excerpt

    @given(log=unparseable_log(min_lines=RAW_EXCERPT_LINES, max_lines=RAW_EXCERPT_LINES + 500))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_long_log_returns_last_N_lines(self, log: str) -> None:
        """Logs with RAW_EXCERPT_LINES+ lines return exactly the last N lines."""
        router = _make_router()
        failures, excerpt, is_unparseable = router.parse(log)

        assert failures == [], "No parsed failures expected for unparseable log"
        assert is_unparseable is True, "Should be flagged as unparseable"
        assert excerpt is not None, "Excerpt should not be None for unparseable log"

        # Reproduce the exact logic the router uses: splitlines then join last N
        original_lines = log.splitlines()
        expected_excerpt = "\n".join(original_lines[-RAW_EXCERPT_LINES:])
        assert excerpt == expected_excerpt

        # The excerpt should contain at most RAW_EXCERPT_LINES lines
        assert len(original_lines) >= RAW_EXCERPT_LINES
        tail = original_lines[-RAW_EXCERPT_LINES:]
        assert len(tail) == RAW_EXCERPT_LINES

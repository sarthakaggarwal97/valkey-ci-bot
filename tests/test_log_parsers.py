# Feature: valkey-ci-agent, Property 3: Log parser extracts structured fields from all supported formats
"""Property-based tests for log parsers.

Validates: Requirements 2.2, 2.3, 2.4

For any valid failure log in Google Test, Tcl runtest, build error, sentinel,
cluster, or module API format, the appropriate parser should extract at minimum
the failure identifier, file path, and error message. The parser type field
should correctly identify the source format.
"""

from __future__ import annotations

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from scripts.models import ParsedFailure
from scripts.parsers.build_error_parser import BuildErrorParser
from scripts.parsers.gtest_parser import GTestParser
from scripts.parsers.sentinel_cluster_parser import SentinelClusterParser
from scripts.parsers.tcl_parser import TclTestParser

# ---------------------------------------------------------------------------
# Strategies: generate log strings matching each parser's format grammar
# ---------------------------------------------------------------------------

# Strategy for valid C/C++ identifiers (used in test suite/test names)
_identifier = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,30}", fullmatch=True)

# Strategy for file paths
_file_stem = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)
_cc_file_path = st.builds(
    lambda dirs, name: f"{dirs}/{name}",
    dirs=st.sampled_from(["src", "tests/unit", "path/to"]),
    name=st.builds(lambda s: f"{s}.cc", s=_file_stem),
)
_c_file_path = st.builds(
    lambda dirs, name: f"{dirs}/{name}",
    dirs=st.sampled_from(["src", "lib", "deps"]),
    name=st.builds(lambda s: f"{s}.c", s=_file_stem),
)
_tcl_unit_path = st.builds(
    lambda name: f"tests/unit/{name}.tcl",
    name=_file_stem,
)
_sentinel_path = st.builds(
    lambda name: f"tests/sentinel/{name}.tcl",
    name=_file_stem,
)
_cluster_path = st.builds(
    lambda name: f"tests/cluster/{name}.tcl",
    name=_file_stem,
)

_line_number = st.integers(min_value=1, max_value=99999)
_col_number = st.integers(min_value=1, max_value=200)
_error_msg = st.from_regex(r"[a-zA-Z][a-zA-Z0-9 _\-]{1,60}", fullmatch=True)
_test_description = st.from_regex(r"[A-Za-z][A-Za-z0-9 _\-]{1,40}", fullmatch=True)


# ---------------------------------------------------------------------------
# Google Test log generator
# ---------------------------------------------------------------------------

@st.composite
def gtest_log(draw: st.DrawFn) -> tuple[str, str, str, int]:
    """Generate a valid Google Test failure log.

    Returns (log_content, test_name, file_path, line_number).
    """
    suite = draw(_identifier)
    name = draw(_identifier)
    test_name = f"{suite}.{name}"
    file_path = draw(_cc_file_path)
    line_num = draw(_line_number)

    log = (
        f"Running main() from gtest_main.cc\n"
        f"[==========] Running 5 tests from 1 test suite.\n"
        f"[----------] 5 tests from {suite}\n"
        f"[ RUN      ] {test_name}\n"
        f"{file_path}:{line_num}: Failure\n"
        f"Expected: something\n"
        f"  Actual: something_else\n"
        f"[  FAILED  ] {test_name}\n"
        f"[----------] 5 tests from {suite}\n"
    )
    return log, test_name, file_path, line_num


# ---------------------------------------------------------------------------
# Tcl test log generator
# ---------------------------------------------------------------------------

@st.composite
def tcl_log(draw: st.DrawFn) -> tuple[str, str, str]:
    """Generate a valid Tcl runtest failure log.

    Returns (log_content, description, file_path).
    """
    description = draw(_test_description)
    file_path = draw(_tcl_unit_path)

    log = (
        f"Starting test server...\n"
        f"[err]: {description} in {file_path}\n"
        f"Expected 'x' to equal 'y'\n"
    )
    return log, description, file_path


# ---------------------------------------------------------------------------
# Build error log generators
# ---------------------------------------------------------------------------

@st.composite
def build_error_log(draw: st.DrawFn) -> tuple[str, str, int, str]:
    """Generate a valid compiler error log.

    Returns (log_content, file_path, line_number, error_message).
    """
    file_path = draw(_c_file_path)
    line_num = draw(_line_number)
    col_num = draw(_col_number)
    message = draw(_error_msg)

    log = (
        f"make[1]: Entering directory '/build'\n"
        f"{file_path}:{line_num}:{col_num}: error: {message}\n"
        f"make[1]: *** [Makefile:42: target] Error 1\n"
    )
    return log, file_path, line_num, message


@st.composite
def build_werror_log(draw: st.DrawFn) -> tuple[str, str, int, str]:
    """Generate a valid -Werror promoted warning log.

    Returns (log_content, file_path, line_number, warning_message).
    """
    file_path = draw(_c_file_path)
    line_num = draw(_line_number)
    col_num = draw(_col_number)
    message = draw(_error_msg)

    log = (
        f"make[1]: Entering directory '/build'\n"
        f"{file_path}:{line_num}:{col_num}: warning: {message} [-Werror,-Wunused]\n"
        f"1 warning generated.\n"
    )
    return log, file_path, line_num, message


# ---------------------------------------------------------------------------
# Sentinel/cluster log generators
# ---------------------------------------------------------------------------

@st.composite
def sentinel_log(draw: st.DrawFn) -> tuple[str, str, str]:
    """Generate a valid sentinel test failure log.

    Returns (log_content, description, file_path).
    """
    description = draw(_test_description)
    file_path = draw(_sentinel_path)

    log = (
        f"Starting sentinel test...\n"
        f"[err]: {description} in {file_path}\n"
        f"Sentinel test completed with errors\n"
    )
    return log, description, file_path


@st.composite
def cluster_fail_log(draw: st.DrawFn) -> tuple[str, str, str]:
    """Generate a valid cluster FAIL: pattern log.

    Returns (log_content, description, file_path).
    """
    description = draw(_test_description)
    file_path = draw(_cluster_path)

    log = (
        f"Starting cluster test...\n"
        f"FAIL: {description} in {file_path}\n"
        f"Cluster test completed with errors\n"
    )
    return log, description, file_path


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

class TestGTestParserProperty:
    """Property tests for GTestParser."""

    @given(data=gtest_log())
    @settings(max_examples=100)
    def test_extracts_structured_fields(self, data: tuple[str, str, str, int]) -> None:
        """**Validates: Requirements 2.2, 2.3**"""
        log_content, test_name, file_path, line_num = data
        parser = GTestParser()

        assert parser.can_parse(log_content)
        results = parser.parse(log_content)

        assert len(results) >= 1
        failure = results[0]
        assert failure.failure_identifier == test_name
        assert failure.file_path == file_path
        assert failure.error_message  # non-empty
        assert failure.parser_type == "gtest"
        assert failure.line_number == line_num


class TestTclParserProperty:
    """Property tests for TclTestParser."""

    @given(data=tcl_log())
    @settings(max_examples=100)
    def test_extracts_structured_fields(self, data: tuple[str, str, str]) -> None:
        """**Validates: Requirements 2.2, 2.3**"""
        log_content, description, file_path = data
        parser = TclTestParser()

        assert parser.can_parse(log_content)
        results = parser.parse(log_content)

        assert len(results) >= 1
        failure = results[0]
        assert failure.failure_identifier  # non-empty
        assert failure.file_path == file_path
        assert failure.error_message  # non-empty
        assert failure.parser_type == "tcl"

    def test_extracts_runtest_summary_timeout(self) -> None:
        """**Validates: Requirements 2.2, 2.3**"""
        log_content = (
            "Test Summary: 3885 passed, 1 failed\n"
            "!!! WARNING The following tests failed:\n"
            "*** [TIMEOUT]: Fix cluster in "
            "tests/unit/cluster/many-slot-migration.tcl\n"
            "##[error]Process completed with exit code 1.\n"
        )
        parser = TclTestParser()

        assert parser.can_parse(log_content)
        results = parser.parse(log_content)

        assert len(results) == 1
        failure = results[0]
        assert failure.failure_identifier == (
            "tests/unit/cluster/many-slot-migration.tcl::Fix cluster"
        )
        assert failure.test_name == "Fix cluster"
        assert failure.file_path == "tests/unit/cluster/many-slot-migration.tcl"
        assert failure.error_message == "[TIMEOUT]: Fix cluster"
        assert failure.assertion_details == "Runtest summary status: TIMEOUT"
        assert failure.parser_type == "tcl"


class TestBuildErrorParserProperty:
    """Property tests for BuildErrorParser."""

    @given(data=build_error_log())
    @settings(max_examples=100)
    def test_extracts_structured_fields_from_errors(
        self, data: tuple[str, str, int, str]
    ) -> None:
        """**Validates: Requirements 2.2, 2.3**"""
        log_content, file_path, line_num, message = data
        parser = BuildErrorParser()

        assert parser.can_parse(log_content)
        results = parser.parse(log_content)

        assert len(results) >= 1
        failure = results[0]
        assert failure.failure_identifier  # non-empty
        assert failure.file_path == file_path
        assert failure.error_message  # non-empty
        assert failure.parser_type == "build"
        assert failure.line_number == line_num

    @given(data=build_werror_log())
    @settings(max_examples=100)
    def test_extracts_structured_fields_from_werror(
        self, data: tuple[str, str, int, str]
    ) -> None:
        """**Validates: Requirements 2.2, 2.3**"""
        log_content, file_path, line_num, message = data
        parser = BuildErrorParser()

        assert parser.can_parse(log_content)
        results = parser.parse(log_content)

        assert len(results) >= 1
        failure = results[0]
        assert failure.failure_identifier  # non-empty
        assert failure.file_path == file_path
        assert failure.error_message  # non-empty
        assert failure.parser_type == "build"
        assert failure.line_number == line_num


class TestSentinelClusterParserProperty:
    """Property tests for SentinelClusterParser."""

    @given(data=sentinel_log())
    @settings(max_examples=100)
    def test_extracts_structured_fields_from_sentinel(
        self, data: tuple[str, str, str]
    ) -> None:
        """**Validates: Requirements 2.2, 2.4**"""
        log_content, description, file_path = data
        parser = SentinelClusterParser()

        assert parser.can_parse(log_content)
        results = parser.parse(log_content)

        assert len(results) >= 1
        failure = results[0]
        assert failure.failure_identifier  # non-empty
        assert failure.file_path == file_path
        assert failure.error_message  # non-empty
        assert failure.parser_type == "sentinel"

    @given(data=cluster_fail_log())
    @settings(max_examples=100)
    def test_extracts_structured_fields_from_cluster(
        self, data: tuple[str, str, str]
    ) -> None:
        """**Validates: Requirements 2.2, 2.4**"""
        log_content, description, file_path = data
        parser = SentinelClusterParser()

        assert parser.can_parse(log_content)
        results = parser.parse(log_content)

        assert len(results) >= 1
        failure = results[0]
        assert failure.failure_identifier  # non-empty
        assert failure.file_path == file_path
        assert failure.error_message  # non-empty
        assert failure.parser_type == "cluster"

"""Tests for the root cause analyzer module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, PropertyMock

import pytest

from scripts.bedrock_client import BedrockClient, BedrockError
from scripts.config import BotConfig, ProjectContext, RetrievalConfig
from scripts.models import FailureReport, ParsedFailure, RootCauseReport
from scripts.root_cause_analyzer import (
    RootCauseAnalyzer,
    _apply_test_to_source_patterns,
    _build_user_prompt,
    _detect_flaky_indicators,
    _extract_file_paths,
    _parse_bedrock_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_parsed_failure(**overrides) -> ParsedFailure:
    defaults = dict(
        failure_identifier="TestSuite.TestName",
        test_name="TestSuite.TestName",
        file_path="tests/unit/test_foo.cc",
        error_message="Expected 1 but got 2",
        assertion_details=None,
        line_number=42,
        stack_trace=None,
        parser_type="gtest",
    )
    defaults.update(overrides)
    return ParsedFailure(**defaults)


def _make_failure_report(**overrides) -> FailureReport:
    defaults = dict(
        workflow_name="CI",
        job_name="test-ubuntu",
        matrix_params={"os": "ubuntu-latest"},
        commit_sha="abc123def456",
        failure_source="trusted",
        parsed_failures=[_make_parsed_failure()],
        raw_log_excerpt=None,
        is_unparseable=False,
    )
    defaults.update(overrides)
    return FailureReport(**defaults)


def _make_bedrock_response(
    description: str = "Bug in foo.c",
    files_to_change: list[str] | None = None,
    confidence: str = "high",
    rationale: str = "The assertion is wrong",
    is_flaky: bool = False,
    flakiness_indicators: list[str] | None = None,
) -> str:
    return json.dumps({
        "description": description,
        "files_to_change": files_to_change or ["src/foo.c"],
        "confidence": confidence,
        "rationale": rationale,
        "is_flaky": is_flaky,
        "flakiness_indicators": flakiness_indicators,
    })


def _make_analyzer(
    bedrock_response: str | Exception = "",
    file_contents: dict[str, str] | None = None,
) -> tuple[RootCauseAnalyzer, MagicMock, MagicMock]:
    """Create an analyzer with mocked Bedrock and GitHub clients."""
    mock_bedrock = MagicMock(spec=BedrockClient)
    if isinstance(bedrock_response, Exception):
        mock_bedrock.invoke.side_effect = bedrock_response
    else:
        mock_bedrock.invoke.return_value = bedrock_response

    mock_github = MagicMock()
    mock_repo = MagicMock()
    mock_github.get_repo.return_value = mock_repo

    if file_contents:
        def get_contents_side_effect(path, ref=None):
            if path in file_contents:
                mock_file = MagicMock()
                mock_file.decoded_content = file_contents[path].encode("utf-8")
                return mock_file
            raise Exception(f"File not found: {path}")
        mock_repo.get_contents.side_effect = get_contents_side_effect
    else:
        mock_repo.get_contents.side_effect = Exception("File not found")

    analyzer = RootCauseAnalyzer(mock_bedrock, mock_github)
    return analyzer, mock_bedrock, mock_github


# ---------------------------------------------------------------------------
# Unit tests: _detect_flaky_indicators
# ---------------------------------------------------------------------------

class TestDetectFlakyIndicators:
    def test_detects_timeout_keyword(self):
        pf = _make_parsed_failure(error_message="Test timed out after 30s")
        indicators = _detect_flaky_indicators(pf)
        assert "timed out" in indicators

    def test_detects_race_condition(self):
        pf = _make_parsed_failure(
            error_message="Possible race condition in handler"
        )
        indicators = _detect_flaky_indicators(pf)
        assert "race condition" in indicators

    def test_detects_intermittent_in_stack_trace(self):
        pf = _make_parsed_failure(
            error_message="assertion failed",
            stack_trace="intermittent failure in network layer",
        )
        indicators = _detect_flaky_indicators(pf)
        assert "intermittent" in indicators

    def test_no_indicators_for_normal_failure(self):
        pf = _make_parsed_failure(error_message="Expected 1 but got 2")
        indicators = _detect_flaky_indicators(pf)
        assert indicators == []

    def test_detects_multiple_indicators(self):
        pf = _make_parsed_failure(
            error_message="timeout after sleep, possible deadlock"
        )
        indicators = _detect_flaky_indicators(pf)
        assert "timeout" in indicators
        assert "sleep" in indicators
        assert "deadlock" in indicators


# ---------------------------------------------------------------------------
# Unit tests: _extract_file_paths
# ---------------------------------------------------------------------------

class TestExtractFilePaths:
    def test_extracts_c_source_path(self):
        text = "error in src/server.c:42: undefined reference"
        paths = _extract_file_paths(text)
        assert "src/server.c" in paths

    def test_extracts_test_path(self):
        text = "FAILED at tests/unit/test_foo.cc:10"
        paths = _extract_file_paths(text)
        assert "tests/unit/test_foo.cc" in paths

    def test_extracts_multiple_paths(self):
        text = "src/foo.c:10: error\nsrc/bar.h:20: note"
        paths = _extract_file_paths(text)
        assert "src/foo.c" in paths
        assert "src/bar.h" in paths

    def test_deduplicates_paths(self):
        text = "src/foo.c:10: error\nsrc/foo.c:20: note"
        paths = _extract_file_paths(text)
        assert paths.count("src/foo.c") == 1

    def test_returns_empty_for_no_paths(self):
        assert _extract_file_paths("no file paths here") == []

    def test_returns_empty_for_empty_string(self):
        assert _extract_file_paths("") == []

    def test_extracts_tcl_path(self):
        text = "[err]: Test failed in tests/unit/expire.tcl"
        paths = _extract_file_paths(text)
        assert "tests/unit/expire.tcl" in paths


# ---------------------------------------------------------------------------
# Unit tests: _apply_test_to_source_patterns
# ---------------------------------------------------------------------------

class TestApplyTestToSourcePatterns:
    def test_maps_tcl_test_to_c_source(self):
        patterns = [
            {"test_path": "tests/unit/{name}.tcl", "source_path": "src/{name}.c"}
        ]
        result = _apply_test_to_source_patterns("tests/unit/expire.tcl", patterns)
        assert result == ["src/expire.c"]

    def test_maps_cc_test_to_c_source(self):
        patterns = [
            {"test_path": "tests/unit/{name}.cc", "source_path": "src/{name}.c"}
        ]
        result = _apply_test_to_source_patterns("tests/unit/server.cc", patterns)
        assert result == ["src/server.c"]

    def test_no_match_returns_empty(self):
        patterns = [
            {"test_path": "tests/unit/{name}.tcl", "source_path": "src/{name}.c"}
        ]
        result = _apply_test_to_source_patterns("src/foo.c", patterns)
        assert result == []

    def test_empty_patterns_returns_empty(self):
        result = _apply_test_to_source_patterns("tests/unit/foo.tcl", [])
        assert result == []

    def test_multiple_patterns(self):
        patterns = [
            {"test_path": "tests/unit/{name}.tcl", "source_path": "src/{name}.c"},
            {"test_path": "tests/unit/{name}.tcl", "source_path": "src/{name}.h"},
        ]
        result = _apply_test_to_source_patterns("tests/unit/foo.tcl", patterns)
        assert "src/foo.c" in result
        assert "src/foo.h" in result


# ---------------------------------------------------------------------------
# Unit tests: _parse_bedrock_response
# ---------------------------------------------------------------------------

class TestParseBedrockResponse:
    def test_parses_valid_json(self):
        raw = _make_bedrock_response()
        report = _parse_bedrock_response(raw)
        assert report.description == "Bug in foo.c"
        assert report.files_to_change == ["src/foo.c"]
        assert report.confidence == "high"
        assert report.is_flaky is False

    def test_strips_markdown_fences(self):
        raw = "```json\n" + _make_bedrock_response() + "\n```"
        report = _parse_bedrock_response(raw)
        assert report.description == "Bug in foo.c"

    def test_invalid_confidence_defaults_to_low(self):
        raw = _make_bedrock_response(confidence="unknown")
        report = _parse_bedrock_response(raw)
        assert report.confidence == "low"

    def test_raises_on_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_bedrock_response("not json at all")

    def test_flaky_report(self):
        raw = _make_bedrock_response(
            is_flaky=True,
            flakiness_indicators=["timeout", "race condition"],
        )
        report = _parse_bedrock_response(raw)
        assert report.is_flaky is True
        assert "timeout" in report.flakiness_indicators


# ---------------------------------------------------------------------------
# Unit tests: _build_user_prompt
# ---------------------------------------------------------------------------

class TestBuildUserPrompt:
    def test_includes_failure_context(self):
        report = _make_failure_report()
        prompt = _build_user_prompt(report, {})
        assert "CI" in prompt
        assert "test-ubuntu" in prompt
        assert "abc123def456" in prompt

    def test_includes_parsed_failure_details(self):
        report = _make_failure_report()
        prompt = _build_user_prompt(report, {})
        assert "TestSuite.TestName" in prompt
        assert "Expected 1 but got 2" in prompt

    def test_includes_source_contents(self):
        report = _make_failure_report()
        sources = {"src/foo.c": "int main() { return 0; }"}
        prompt = _build_user_prompt(report, sources)
        assert "src/foo.c" in prompt
        assert "int main()" in prompt

    def test_includes_matrix_params(self):
        report = _make_failure_report(matrix_params={"os": "ubuntu", "tls": "yes"})
        prompt = _build_user_prompt(report, {})
        assert "os=ubuntu" in prompt
        assert "tls=yes" in prompt

    def test_includes_raw_log_excerpt(self):
        report = _make_failure_report(raw_log_excerpt="some raw log output")
        prompt = _build_user_prompt(report, {})
        assert "some raw log output" in prompt

    def test_includes_retrieved_context(self):
        report = _make_failure_report()
        prompt = _build_user_prompt(report, {}, "## Retrieved Valkey Context\nsnippet")
        assert "Retrieved Valkey Context" in prompt
        assert "snippet" in prompt


# ---------------------------------------------------------------------------
# Unit tests: RootCauseAnalyzer.identify_relevant_files
# ---------------------------------------------------------------------------

class TestIdentifyRelevantFiles:
    def test_includes_failure_file_path(self):
        analyzer, _, _ = _make_analyzer()
        pf = _make_parsed_failure(file_path="tests/unit/test_foo.cc")
        project = ProjectContext()
        files = analyzer.identify_relevant_files(pf, project)
        assert "tests/unit/test_foo.cc" in files

    def test_extracts_paths_from_error_message(self):
        analyzer, _, _ = _make_analyzer()
        pf = _make_parsed_failure(
            error_message="error in src/server.c:42: bad value"
        )
        project = ProjectContext()
        files = analyzer.identify_relevant_files(pf, project)
        assert "src/server.c" in files

    def test_extracts_paths_from_stack_trace(self):
        analyzer, _, _ = _make_analyzer()
        pf = _make_parsed_failure(
            stack_trace="  at src/networking.c:100\n  at src/server.c:50"
        )
        project = ProjectContext()
        files = analyzer.identify_relevant_files(pf, project)
        assert "src/networking.c" in files
        assert "src/server.c" in files

    def test_applies_test_to_source_patterns(self):
        analyzer, _, _ = _make_analyzer()
        pf = _make_parsed_failure(file_path="tests/unit/expire.tcl")
        project = ProjectContext(
            test_to_source_patterns=[
                {"test_path": "tests/unit/{name}.tcl", "source_path": "src/{name}.c"}
            ]
        )
        files = analyzer.identify_relevant_files(pf, project)
        assert "src/expire.c" in files
        assert "tests/unit/expire.tcl" in files

    def test_deduplicates_files(self):
        analyzer, _, _ = _make_analyzer()
        pf = _make_parsed_failure(
            file_path="src/foo.c",
            error_message="error in src/foo.c:10",
        )
        project = ProjectContext()
        files = analyzer.identify_relevant_files(pf, project)
        assert files.count("src/foo.c") == 1


# ---------------------------------------------------------------------------
# Unit tests: RootCauseAnalyzer.analyze
# ---------------------------------------------------------------------------

class TestAnalyze:
    def test_successful_analysis(self):
        response = _make_bedrock_response(
            description="Null pointer in server.c",
            confidence="high",
        )
        analyzer, mock_bedrock, _ = _make_analyzer(bedrock_response=response)
        report = _make_failure_report()
        report._repo_name = "valkey-io/valkey"

        result = analyzer.analyze(report, ProjectContext())

        assert result.description == "Null pointer in server.c"
        assert result.confidence == "high"
        mock_bedrock.invoke.assert_called_once()

    def test_bedrock_error_returns_analysis_failed(self):
        analyzer, _, _ = _make_analyzer(
            bedrock_response=BedrockError("API down", error_code="ServiceError")
        )
        report = _make_failure_report()
        report._repo_name = "valkey-io/valkey"

        result = analyzer.analyze(report, ProjectContext())

        assert "analysis-failed" in result.description
        assert result.confidence == "low"

    def test_unparseable_response_returns_analysis_failed(self):
        analyzer, _, _ = _make_analyzer(bedrock_response="not valid json {{{")
        report = _make_failure_report()
        report._repo_name = "valkey-io/valkey"

        result = analyzer.analyze(report, ProjectContext())

        assert "analysis-failed" in result.description

    def test_flaky_indicators_merged_into_report(self):
        response = _make_bedrock_response(is_flaky=False)
        analyzer, _, _ = _make_analyzer(bedrock_response=response)
        pf = _make_parsed_failure(
            error_message="Test timed out after waiting"
        )
        report = _make_failure_report(parsed_failures=[pf])
        report._repo_name = "valkey-io/valkey"

        result = analyzer.analyze(report, ProjectContext())

        assert result.is_flaky is True
        assert "timed out" in result.flakiness_indicators

    def test_retrieves_file_contents_at_commit_sha(self):
        response = _make_bedrock_response()
        file_contents = {"tests/unit/test_foo.cc": "// test code"}
        analyzer, mock_bedrock, mock_github = _make_analyzer(
            bedrock_response=response,
            file_contents=file_contents,
        )
        report = _make_failure_report()
        report._repo_name = "valkey-io/valkey"

        result = analyzer.analyze(report, ProjectContext())

        # Verify GitHub was called to get file contents
        mock_github.get_repo.assert_called()
        # Verify the prompt sent to Bedrock includes the source content
        call_args = mock_bedrock.invoke.call_args
        user_prompt = call_args[0][1]
        assert "test code" in user_prompt

    def test_includes_retrieved_context_when_retriever_is_configured(self):
        response = _make_bedrock_response()
        analyzer, mock_bedrock, _ = _make_analyzer(bedrock_response=response)
        mock_retriever = MagicMock()
        mock_retriever.render_for_prompt.return_value = (
            "## Retrieved Valkey Context\nsentinel failover notes"
        )
        analyzer.with_retriever(
            mock_retriever,
            RetrievalConfig(enabled=True, code_knowledge_base_id="CODEKB"),
        )
        report = _make_failure_report(raw_log_excerpt="failover timeout")
        report._repo_name = "valkey-io/valkey"

        analyzer.analyze(report, ProjectContext())

        user_prompt = mock_bedrock.invoke.call_args[0][1]
        assert "Retrieved Valkey Context" in user_prompt
        assert "sentinel failover notes" in user_prompt

    def test_analysis_failed_report_structure(self):
        """Verify the analysis-failed report has the expected shape."""
        report = RootCauseAnalyzer._analysis_failed_report("some error")
        assert report.description.startswith("analysis-failed:")
        assert report.files_to_change == []
        assert report.confidence == "low"
        assert report.is_flaky is False


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------
# Feature: valkey-ci-agent, Property 6: Relevant file identification from failure data

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Valid root directories that _FILE_PATH_RE recognises
_ROOT_DIRS = ["src", "tests", "test", "include", "lib", "modules"]
# Valid file extensions that _FILE_PATH_RE recognises
_EXTENSIONS = ["cpp", "cc", "hpp", "tcl", "py", "rs", "java", "c", "h"]


def _file_path_strategy() -> st.SearchStrategy[str]:
    """Generate file paths that match the _FILE_PATH_RE regex."""
    return st.builds(
        lambda root, segments, stem, ext: f"{root}/{'/'.join(segments)}/{stem}.{ext}"
        if segments
        else f"{root}/{stem}.{ext}",
        root=st.sampled_from(_ROOT_DIRS),
        segments=st.lists(
            st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True),
            min_size=0,
            max_size=2,
        ),
        stem=st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True),
        ext=st.sampled_from(_EXTENSIONS),
    )


def _embed_path_in_text(path: str) -> st.SearchStrategy[str]:
    """Wrap a file path in surrounding text so _extract_file_paths can find it."""
    return st.sampled_from([
        f"error in {path}:42: undefined reference",
        f"  at {path}:100",
        f'"{path}": no such file',
        f"FAILED at {path}:1",
        f"({path}:10) assertion failed",
    ])


@st.composite
def _parsed_failure_with_paths(draw: st.DrawFn):
    """Generate a ParsedFailure with file paths embedded in various fields."""
    # Generate 1-3 file paths to embed
    embedded_paths = draw(
        st.lists(_file_path_strategy(), min_size=1, max_size=3, unique=True)
    )

    # Decide which fields get paths
    error_path = embedded_paths[0]
    error_text = draw(_embed_path_in_text(error_path))

    stack_paths = embedded_paths[1:2]
    stack_text = None
    if stack_paths:
        stack_text = draw(_embed_path_in_text(stack_paths[0]))

    assertion_paths = embedded_paths[2:3]
    assertion_text = None
    if assertion_paths:
        assertion_text = draw(_embed_path_in_text(assertion_paths[0]))

    # The failure's own file_path (always included in results)
    own_file = draw(_file_path_strategy())

    failure = ParsedFailure(
        failure_identifier="TestSuite.TestCase",
        test_name="TestSuite.TestCase",
        file_path=own_file,
        error_message=error_text,
        assertion_details=assertion_text,
        line_number=42,
        stack_trace=stack_text,
        parser_type="gtest",
    )
    return failure, embedded_paths, own_file


class TestRelevantFileIdentificationProperty:
    """Property 6: Relevant file identification from failure data.

    Validates: Requirements 3.1
    """

    @given(data=_parsed_failure_with_paths())
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_returns_non_empty_list_containing_referenced_paths(
        self,
        data: tuple[ParsedFailure, list[str], str],
    ) -> None:
        """**Validates: Requirements 3.1**

        For any ParsedFailure containing file paths in error messages,
        stack traces, or test file references, identify_relevant_files
        should return a non-empty list that includes those paths.
        """
        failure, embedded_paths, own_file = data
        analyzer, _, _ = _make_analyzer()
        project = ProjectContext()

        result = analyzer.identify_relevant_files(failure, project)

        # Result must be non-empty
        assert len(result) > 0, "identify_relevant_files returned empty list"

        # The failure's own file_path must always be present
        assert own_file in result, (
            f"failure.file_path={own_file!r} missing from result {result}"
        )

        # Every embedded path must appear in the result
        for path in embedded_paths:
            assert path in result, (
                f"embedded path {path!r} missing from result {result}"
            )

        # Result must be deduplicated
        assert len(result) == len(set(result)), (
            f"result contains duplicates: {result}"
        )


# ---------------------------------------------------------------------------
# Feature: valkey-ci-agent, Property 7: Root cause analysis error propagation
# ---------------------------------------------------------------------------


def _unparseable_response_strategy() -> st.SearchStrategy[str]:
    """Generate strings that cause _parse_bedrock_response to raise.

    These are either invalid JSON or valid JSON whose top-level value is
    not a dict (so .get() / attribute access fails).
    """
    return st.one_of(
        # Completely non-JSON text
        st.text(min_size=1, max_size=200).filter(
            lambda s: not s.strip().startswith("{") and not s.strip().startswith("[")
        ),
        # Truncated / malformed JSON
        st.sampled_from([
            "{",
            '{"description": "ok"',
            "```json\n{broken\n```",
        ]),
        # Valid JSON but not a dict — causes AttributeError on .get()
        st.sampled_from([
            "null",
            "[]",
            "true",
            "false",
            "0",
            "42",
            '"just a string"',
            "[1, 2, 3]",
        ]),
    )


def _bedrock_error_strategy() -> st.SearchStrategy[BedrockError]:
    """Generate various BedrockError instances."""
    error_codes = st.sampled_from([
        "ServiceError",
        "ValidationException",
        "AccessDeniedException",
        "ResourceNotFoundException",
        "ThrottlingException",
        "InternalServerException",
        "ModelErrorException",
        None,
    ])
    messages = st.text(min_size=1, max_size=200)
    retryable = st.booleans()
    return st.builds(
        lambda msg, code, retry: BedrockError(msg, error_code=code, retryable=retry),
        msg=messages,
        code=error_codes,
        retry=retryable,
    )


class TestRootCauseAnalysisErrorPropagation:
    """Property 7: Root cause analysis error propagation.

    **Validates: Requirements 3.6**

    For any Bedrock error (API failure or unparseable response), the
    Root_Cause_Analyzer should return a result with status
    "analysis-failed" and confidence "low".
    """

    @given(error=_bedrock_error_strategy())
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_bedrock_error_returns_analysis_failed(
        self,
        error: BedrockError,
    ) -> None:
        """**Validates: Requirements 3.6**

        For any BedrockError raised by the client, analyze() must return
        a report with 'analysis-failed' in the description, confidence
        'low', empty files_to_change, and is_flaky False.
        """
        analyzer, _, _ = _make_analyzer(bedrock_response=error)
        report = _make_failure_report()
        report._repo_name = "valkey-io/valkey"

        result = analyzer.analyze(report, ProjectContext())

        assert "analysis-failed" in result.description, (
            f"Expected 'analysis-failed' in description, got: {result.description!r}"
        )
        assert result.confidence == "low", (
            f"Expected confidence 'low', got: {result.confidence!r}"
        )
        assert result.files_to_change == [], (
            f"Expected empty files_to_change, got: {result.files_to_change!r}"
        )
        assert result.is_flaky is False, (
            "analysis-failed report should not be marked flaky"
        )

    @given(bad_response=_unparseable_response_strategy())
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_unparseable_response_returns_analysis_failed(
        self,
        bad_response: str,
    ) -> None:
        """**Validates: Requirements 3.6**

        For any unparseable Bedrock response, analyze() must return
        a report with 'analysis-failed' in the description, confidence
        'low', empty files_to_change, and is_flaky False.
        """
        analyzer, _, _ = _make_analyzer(bedrock_response=bad_response)
        report = _make_failure_report()
        report._repo_name = "valkey-io/valkey"

        result = analyzer.analyze(report, ProjectContext())

        assert "analysis-failed" in result.description, (
            f"Expected 'analysis-failed' in description, got: {result.description!r}"
        )
        assert result.confidence == "low", (
            f"Expected confidence 'low', got: {result.confidence!r}"
        )
        assert result.files_to_change == [], (
            f"Expected empty files_to_change, got: {result.files_to_change!r}"
        )
        assert result.is_flaky is False, (
            "analysis-failed report should not be marked flaky"
        )

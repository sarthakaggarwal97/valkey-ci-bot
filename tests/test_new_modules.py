"""Tests for all new modules added to the CI agent.

Covers parsers, correlation engine, review feedback, fuzzer trends,
alerting, SLA metrics, JSON/HTML/text helpers, exceptions, log parser
router updates, and config __post_init__ validation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from scripts.alerting import Alert, AlertConfig, AlertDispatcher
from scripts.config import BotConfig, ReviewerConfig
from scripts.correlation_engine import CorrelatedFailureGroup, correlate_failures
from scripts.exceptions import (
    AnalysisError,
    CIAgentError,
    ConfigurationError,
    GitHubAPIError,
    ParseError,
    RateLimitExceeded,
    StoreConflictError,
    StoreError,
    ValidationError,
)
from scripts.fuzzer_trends import FuzzerTrendTracker, ScenarioTrend
from scripts.html_helpers import SafeHtml, html_attr, html_cell, html_escape, safe_html
from scripts.json_helpers import bool_text, mapping, safe_float, safe_int, safe_list, safe_str
from scripts.log_parser import LogParserRouter
from scripts.models import FailureReport, ParsedFailure
from scripts.parsers.module_api_parser import ModuleApiParser
from scripts.parsers.rdma_parser import RdmaParser
from scripts.parsers.sanitizer_parser import SanitizerParser
from scripts.parsers.valgrind_parser import ValgrindParser
from scripts.review_feedback import FeedbackTracker, ReviewFinding
from scripts.sla_metrics import MetricsTracker, OperationMetric, SLAMetrics
from scripts.text_utils import strip_ansi, strip_markdown_fences

# ── helpers ──────────────────────────────────────────────────────────

def _make_report(
    parsed: Optional[list] = None,
    commit: str = "abc123",
    job: str = "build",
) -> FailureReport:
    return FailureReport(
        workflow_name="ci",
        job_name=job,
        matrix_params={},
        commit_sha=commit,
        failure_source="trusted",
        parsed_failures=parsed or [],
    )


def _pf(
    ident: str = "t1",
    test_name: Optional[str] = None,
    file_path: str = "",
    error_message: str = "err",
) -> ParsedFailure:
    return ParsedFailure(
        failure_identifier=ident,
        test_name=test_name,
        file_path=file_path,
        error_message=error_message,
        assertion_details=None,
        line_number=None,
        stack_trace=None,
        parser_type="test",
    )


# =====================================================================
# 1. SanitizerParser
# =====================================================================

class TestSanitizerParser:

    def test_can_parse_asan_log(self) -> None:
        log = "==12345==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1234\n"
        assert SanitizerParser().can_parse(log)

    def test_cannot_parse_unrelated(self) -> None:
        assert not SanitizerParser().can_parse("all tests passed\n")

    def test_parse_asan_with_frame(self) -> None:
        log = (
            "==99==ERROR: AddressSanitizer: heap-buffer-overflow on address 0xdead\n"
            "    #0 0x55 in myFunc src/server.c:42\n"
            "    #1 0x66 in main src/main.c:10\n"
        )
        results = SanitizerParser().parse(log)
        assert len(results) == 1
        f = results[0]
        assert f.parser_type == "sanitizer"
        assert "heap-buffer-overflow" in f.error_message
        assert f.file_path == "src/server.c"
        assert f.line_number == 42
        assert f.test_name == "myFunc"
        assert f.stack_trace is not None

    def test_parse_ubsan(self) -> None:
        log = "src/util.c:10:5: runtime error: signed integer overflow: 2147483647 + 1\n"
        results = SanitizerParser().parse(log)
        assert len(results) == 1
        assert "UBSan" in results[0].error_message
        assert results[0].line_number == 10

    def test_parse_summary_fallback(self) -> None:
        log = "SUMMARY: AddressSanitizer: stack-overflow src/foo.c:7 in bar\n"
        results = SanitizerParser().parse(log)
        assert len(results) == 1
        assert results[0].file_path == "src/foo.c"

    def test_deduplicates(self) -> None:
        log = (
            "==1==ERROR: AddressSanitizer: heap-use-after-free on address 0x1\n"
            "    #0 0x1 in fn src/a.c:1\n"
            "==2==ERROR: AddressSanitizer: heap-use-after-free on address 0x2\n"
            "    #0 0x2 in fn src/a.c:1\n"
        )
        results = SanitizerParser().parse(log)
        assert len(results) == 1

    def test_empty_log(self) -> None:
        assert SanitizerParser().parse("") == []


# =====================================================================
# 2. ValgrindParser
# =====================================================================

class TestValgrindParser:

    def test_can_parse_valgrind_error(self) -> None:
        log = "==100== Invalid read of size 4\n"
        assert ValgrindParser().can_parse(log)

    def test_cannot_parse_clean(self) -> None:
        assert not ValgrindParser().can_parse("all good\n")

    def test_parse_invalid_read(self) -> None:
        log = (
            "==100== Invalid read of size 4\n"
            "==100==    at 0xABC: readData (io.c:55)\n"
            "==100==    by 0xDEF: main (main.c:10)\n"
        )
        results = ValgrindParser().parse(log)
        assert len(results) == 1
        f = results[0]
        assert f.parser_type == "valgrind"
        assert "Invalid read" in f.error_message
        assert f.file_path == "io.c"
        assert f.line_number == 55

    def test_parse_leak(self) -> None:
        log = (
            "==200== definitely lost: 1,024 bytes in 2 blocks\n"
            "==200==    at 0x1: alloc (alloc.c:3)\n"
        )
        results = ValgrindParser().parse(log)
        assert len(results) == 1
        assert "definitely" in results[0].error_message
        assert "1024" in results[0].error_message

    def test_zero_byte_leak_skipped(self) -> None:
        log = "==300== definitely lost: 0 bytes in 0 blocks\n"
        assert ValgrindParser().parse(log) == []

    def test_empty_log(self) -> None:
        assert ValgrindParser().parse("") == []


# =====================================================================
# 3. RdmaParser
# =====================================================================

class TestRdmaParser:

    def test_can_parse_rdma_err(self) -> None:
        # rdma parser requires the [err]: line to reference an RDMA-flavored
        # test path — otherwise it would greedily claim unrelated failures.
        log = (
            "rdma setup\n"
            "[err]: connection timed out in tests/integration/rdma_test.tcl\n"
        )
        assert RdmaParser().can_parse(log)

    def test_cannot_parse_no_rdma(self) -> None:
        assert not RdmaParser().can_parse("[err]: something\n")

    def test_parse_err_with_file(self) -> None:
        log = (
            "rdma init\n"
            "[err]: replication failed in tests/integration/rdma_repl.tcl\n"
        )
        results = RdmaParser().parse(log)
        assert len(results) == 1
        assert results[0].file_path == "tests/integration/rdma_repl.tcl"
        assert results[0].parser_type == "rdma"

    def test_parse_connection_fallback(self) -> None:
        # rdma connection-error fallback now requires explicit RDMA context
        # (ibv_* failure or 'rdma_connect failed'). Generic 'Connection
        # refused' messages no longer trigger the parser.
        log = "rdma test\nrdma_connect failed: ECONNREFUSED\n"
        results = RdmaParser().parse(log)
        assert len(results) == 1
        assert "RDMA" in results[0].error_message

    def test_empty_rdma_log(self) -> None:
        assert RdmaParser().parse("") == []


# =====================================================================
# 4. ModuleApiParser
# =====================================================================

class TestModuleApiParser:

    def test_can_parse_module_err(self) -> None:
        log = "[err]: test failed in tests/modules/foo.tcl\n"
        assert ModuleApiParser().can_parse(log)

    def test_cannot_parse_unrelated(self) -> None:
        assert not ModuleApiParser().can_parse("OK\n")

    def test_parse_module_err(self) -> None:
        log = "[err]: keyspace notify in tests/unit/moduleapi/hooks.tcl\n"
        results = ModuleApiParser().parse(log)
        assert len(results) == 1
        f = results[0]
        assert f.parser_type == "module"
        assert f.file_path == "tests/unit/moduleapi/hooks.tcl"

    def test_parse_module_load_failure(self) -> None:
        log = "Error loading module /path/to/mymod.so\n"
        results = ModuleApiParser().parse(log)
        assert len(results) == 1
        assert "load failure" in results[0].error_message.lower()

    def test_parse_module_crash(self) -> None:
        log = "Module testmod caused a crash\n"
        results = ModuleApiParser().parse(log)
        assert len(results) == 1
        assert "crashed" in results[0].error_message

    def test_parse_module_assert(self) -> None:
        log = "serverAssert(condition != NULL) in src/module.c:99\n"
        results = ModuleApiParser().parse(log)
        assert len(results) >= 1
        f = results[0]
        assert f.assertion_details is not None
        assert f.line_number == 99

    def test_empty_log(self) -> None:
        assert ModuleApiParser().parse("") == []


# =====================================================================
# 5. CorrelationEngine
# =====================================================================

class TestCorrelationEngine:

    def test_empty_input(self) -> None:
        assert correlate_failures([]) == []

    def test_single_report_ungrouped(self) -> None:
        r = _make_report([_pf("t1", file_path="a.c")])
        groups = correlate_failures([r])
        assert len(groups) == 1
        assert groups[0].correlation_reason == "ungrouped"

    def test_shared_file_groups(self) -> None:
        r1 = _make_report([_pf("t1", file_path="src/net.c")])
        r2 = _make_report([_pf("t2", file_path="src/net.c")])
        groups = correlate_failures([r1, r2])
        file_groups = [g for g in groups if g.correlation_reason == "shared_file_paths"]
        assert len(file_groups) == 1
        assert len(file_groups[0].failures) == 2
        assert "src/net.c" in file_groups[0].shared_files

    def test_fuzzy_error_groups(self) -> None:
        r1 = _make_report([_pf("t1", error_message="timeout waiting for replication")])
        r2 = _make_report([_pf("t2", error_message="timeout waiting for replication sync")])
        groups = correlate_failures([r1, r2])
        error_groups = [g for g in groups if g.correlation_reason == "shared_error_pattern"]
        assert len(error_groups) == 1

    def test_shared_prefix_groups(self) -> None:
        r1 = _make_report([_pf(ident="a", test_name="tests/unit/cluster/a", error_message="error alpha")])
        r2 = _make_report([_pf(ident="b", test_name="tests/unit/cluster/b", error_message="completely different msg")])
        groups = correlate_failures([r1, r2])
        prefix_groups = [g for g in groups if "shared_test_prefix" in g.correlation_reason]
        assert len(prefix_groups) == 1

    def test_group_id_deterministic(self) -> None:
        r1 = _make_report([_pf("t1", file_path="x.c")])
        r2 = _make_report([_pf("t2", file_path="x.c")])
        g1 = correlate_failures([r1, r2])
        g2 = correlate_failures([r1, r2])
        assert g1[0].group_id == g2[0].group_id


# =====================================================================
# 6. ReviewFeedback
# =====================================================================

class TestFeedbackTracker:

    def _finding(self, fid: str = "f1", confidence: str = "high") -> ReviewFinding:
        return ReviewFinding(
            finding_id=fid, file_path="a.c", line=1,
            severity="high", confidence=confidence,
        )

    def test_record_and_resolve(self) -> None:
        t = FeedbackTracker()
        t.record_finding(1, self._finding("f1"))
        t.record_resolution(1, "f1", "fixed")
        stats = t.get_accuracy_stats()
        assert stats["total"] == 1
        assert stats["resolved"] == 1
        assert stats["precision_rate"] == 1.0

    def test_dismissed_lowers_precision(self) -> None:
        t = FeedbackTracker()
        t.record_finding(1, self._finding("f1"))
        t.record_finding(1, self._finding("f2"))
        t.record_resolution(1, "f1", "fixed")
        t.record_resolution(1, "f2", "dismissed")
        stats = t.get_accuracy_stats()
        assert stats["precision_rate"] == 0.5

    def test_empty_stats(self) -> None:
        stats = FeedbackTracker().get_accuracy_stats()
        assert stats["total"] == 0
        assert stats["precision_rate"] == 0.0

    def test_confidence_calibration(self) -> None:
        t = FeedbackTracker()
        t.record_finding(1, self._finding("f1", confidence="high"))
        t.record_finding(1, self._finding("f2", confidence="low"))
        t.record_resolution(1, "f1", "fixed")
        t.record_resolution(1, "f2", "dismissed")
        cal = t.get_confidence_calibration()
        assert cal["high"]["precision_rate"] == 1.0
        assert cal["low"]["precision_rate"] == 0.0

    def test_resolve_nonexistent_finding(self) -> None:
        t = FeedbackTracker()
        t.record_finding(1, self._finding("f1"))
        t.record_resolution(1, "nonexistent", "fixed")
        stats = t.get_accuracy_stats()
        assert stats["resolved"] == 0


# =====================================================================
# 7. FuzzerTrends
# =====================================================================

class TestFuzzerTrendTracker:

    def test_no_runs_empty(self) -> None:
        assert FuzzerTrendTracker().get_trends() == []

    def test_stable_trend(self) -> None:
        t = FuzzerTrendTracker()
        now = datetime.now(timezone.utc)
        for i in range(6):
            ts = (now - timedelta(days=6 - i)).isoformat()
            t.record_run("scen1", ts, True)
        trends = t.get_trends()
        assert len(trends) == 1
        assert trends[0].trend == "stable"
        assert trends[0].failure_rate == 0.0

    def test_degrading_trend(self) -> None:
        t = FuzzerTrendTracker()
        now = datetime.now(timezone.utc)
        for i in range(4):
            ts = (now - timedelta(days=10 - i)).isoformat()
            t.record_run("scen1", ts, True)
        for i in range(4):
            ts = (now - timedelta(days=4 - i)).isoformat()
            t.record_run("scen1", ts, False)
        trends = t.get_trends()
        assert trends[0].trend == "degrading"

    def test_get_degrading_scenarios(self) -> None:
        t = FuzzerTrendTracker()
        now = datetime.now(timezone.utc)
        for i in range(4):
            ts = (now - timedelta(days=10 - i)).isoformat()
            t.record_run("good", ts, True)
        for i in range(4):
            ts = (now - timedelta(days=10 - i)).isoformat()
            t.record_run("bad", ts, True)
        for i in range(4):
            ts = (now - timedelta(days=4 - i)).isoformat()
            t.record_run("bad", ts, False)
        degrading = t.get_degrading_scenarios()
        names = [s.scenario_name for s in degrading]
        assert "bad" in names
        assert "good" not in names

    def test_old_runs_outside_window(self) -> None:
        t = FuzzerTrendTracker()
        t.record_run("scen1", "2020-01-01T00:00:00+00:00", False)
        assert t.get_trends(window_days=14) == []


# =====================================================================
# 8. Alerting
# =====================================================================

class TestAlertConfig:

    def test_meets_severity_high(self) -> None:
        cfg = AlertConfig(min_severity="high")
        assert cfg.meets_severity("critical")
        assert cfg.meets_severity("high")
        assert not cfg.meets_severity("medium")
        assert not cfg.meets_severity("low")

    def test_meets_severity_low(self) -> None:
        cfg = AlertConfig(min_severity="low")
        assert cfg.meets_severity("low")
        assert cfg.meets_severity("critical")

    def test_unknown_severity_treated_as_zero(self) -> None:
        cfg = AlertConfig(min_severity="high")
        assert not cfg.meets_severity("unknown")


class TestAlertDispatcher:

    def test_disabled_returns_false(self) -> None:
        cfg = AlertConfig(enabled=False, webhook_url="http://x")
        d = AlertDispatcher(cfg)
        assert d.send(Alert(title="t", message="m")) is False

    def test_below_severity_skipped(self) -> None:
        cfg = AlertConfig(enabled=True, min_severity="critical", webhook_url="http://x")
        d = AlertDispatcher(cfg)
        assert d.send(Alert(title="t", message="m", severity="low")) is False

    @patch("scripts.alerting.AlertDispatcher._post_json", return_value=True)
    def test_sends_to_webhook(self, mock_post: MagicMock) -> None:
        cfg = AlertConfig(enabled=True, webhook_url="http://hook", min_severity="low")
        d = AlertDispatcher(cfg)
        assert d.send(Alert(title="t", message="m", severity="high")) is True
        mock_post.assert_called_once()

    @patch("scripts.alerting.AlertDispatcher._post_json", return_value=True)
    def test_sends_to_slack(self, mock_post: MagicMock) -> None:
        cfg = AlertConfig(enabled=True, slack_webhook_url="http://slack", min_severity="low")
        d = AlertDispatcher(cfg)
        assert d.send(Alert(title="t", message="m")) is True


# =====================================================================
# 9. SLA Metrics
# =====================================================================

class TestMetricsTracker:

    def _metric(self, op: str = "rca", dur: float = 1.0, tokens: int = 100, ok: bool = True) -> OperationMetric:
        return OperationMetric(
            operation=op,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
            duration_seconds=dur,
            tokens_used=tokens,
            success=ok,
        )

    def test_empty_metrics(self) -> None:
        sla = MetricsTracker().get_sla_metrics()
        assert sla.total_operations == 0

    def test_record_and_aggregate(self) -> None:
        t = MetricsTracker()
        t.record(self._metric(dur=2.0, tokens=200))
        t.record(self._metric(dur=4.0, tokens=400))
        sla = t.get_sla_metrics()
        assert sla.total_operations == 2
        assert sla.avg_duration_seconds == 3.0
        assert sla.total_tokens == 600

    def test_filter_by_operation(self) -> None:
        t = MetricsTracker()
        t.record(self._metric(op="rca"))
        t.record(self._metric(op="fix_gen"))
        sla = t.get_sla_metrics("rca")
        assert sla.total_operations == 1

    def test_cost_summary(self) -> None:
        t = MetricsTracker()
        t.record(self._metric(op="rca", tokens=100))
        t.record(self._metric(op="rca", tokens=50))
        t.record(self._metric(op="review", tokens=200))
        costs = t.get_cost_summary()
        assert costs["rca"] == 150
        assert costs["review"] == 200

    def test_to_dict(self) -> None:
        t = MetricsTracker()
        t.record(self._metric())
        result = t.to_dict()
        assert len(result) == 1
        assert "operation" in result[0]

    def test_timer_context_manager(self) -> None:
        t = MetricsTracker()
        with t.start_timer("rca") as timer:
            timer.tokens_used = 50
        sla = t.get_sla_metrics()
        assert sla.total_operations == 1
        assert sla.total_tokens == 50

    def test_timer_records_failure_on_exception(self) -> None:
        t = MetricsTracker()
        with pytest.raises(ValueError):
            with t.start_timer("rca") as timer:
                raise ValueError("boom")
        sla = t.get_sla_metrics()
        assert sla.failure_count == 1


# =====================================================================
# 10. JSON Helpers
# =====================================================================

class TestJsonHelpers:

    def test_mapping_dict(self) -> None:
        assert mapping({"a": 1}) == {"a": 1}

    def test_mapping_non_dict(self) -> None:
        assert mapping("nope") == {}
        assert mapping(None) == {}
        assert mapping(42) == {}

    def test_safe_list_list(self) -> None:
        assert safe_list([1, 2]) == [1, 2]

    def test_safe_list_non_list(self) -> None:
        assert safe_list(None) == []
        assert safe_list("x") == []

    def test_safe_str(self) -> None:
        assert safe_str(None) == ""
        assert safe_str(None, "default") == "default"
        assert safe_str(42) == "42"
        assert safe_str("hello") == "hello"

    def test_safe_int(self) -> None:
        assert safe_int("10") == 10
        assert safe_int(None) == 0
        assert safe_int("bad", 5) == 5

    def test_safe_float(self) -> None:
        assert safe_float("3.14") == pytest.approx(3.14)
        assert safe_float(None) == 0.0
        assert safe_float("bad", 1.5) == 1.5

    def test_bool_text(self) -> None:
        assert bool_text(True) == "yes"
        assert bool_text(False) == "no"


# =====================================================================
# 11. HTML Helpers
# =====================================================================

class TestHtmlHelpers:

    def test_html_escape(self) -> None:
        assert html_escape("<b>hi</b>") == "&lt;b&gt;hi&lt;/b&gt;"

    def test_html_escape_none(self) -> None:
        assert html_escape(None) == ""

    def test_html_attr(self) -> None:
        assert html_attr('val"ue') == "val&quot;ue"

    def test_html_cell_safe_html_passthrough(self) -> None:
        s = safe_html("<b>bold</b>")
        assert html_cell(s) == "<b>bold</b>"

    def test_html_cell_escapes_plain(self) -> None:
        assert "&lt;" in html_cell("<script>")

    def test_safe_html_is_str(self) -> None:
        s = safe_html("test")
        assert isinstance(s, str)
        assert isinstance(s, SafeHtml)


# =====================================================================
# 12. Text Utils
# =====================================================================

class TestTextUtils:

    def test_strip_markdown_fences(self) -> None:
        text = "```python\nprint('hi')\n```"
        assert strip_markdown_fences(text) == "print('hi')"

    def test_strip_markdown_no_fences(self) -> None:
        assert strip_markdown_fences("plain text") == "plain text"

    def test_strip_markdown_empty(self) -> None:
        assert strip_markdown_fences("") == ""

    def test_strip_ansi(self) -> None:
        assert strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_strip_ansi_no_codes(self) -> None:
        assert strip_ansi("clean") == "clean"


# =====================================================================
# 13. Exceptions
# =====================================================================

class TestExceptions:

    def test_hierarchy(self) -> None:
        assert issubclass(GitHubAPIError, CIAgentError)
        assert issubclass(ConfigurationError, CIAgentError)
        assert issubclass(StoreConflictError, StoreError)
        assert issubclass(StoreError, CIAgentError)
        assert issubclass(ParseError, CIAgentError)
        assert issubclass(ValidationError, CIAgentError)
        assert issubclass(RateLimitExceeded, CIAgentError)
        assert issubclass(AnalysisError, CIAgentError)

    def test_raise_and_catch(self) -> None:
        with pytest.raises(CIAgentError):
            raise GitHubAPIError("api down")

    def test_store_conflict_caught_as_store(self) -> None:
        with pytest.raises(StoreError):
            raise StoreConflictError("conflict")

    def test_message_preserved(self) -> None:
        try:
            raise ParseError("bad log")
        except ParseError as e:
            assert str(e) == "bad log"


# =====================================================================
# 14. LogParserRouter (updated priority system)
# =====================================================================

class TestLogParserRouter:

    def test_register_priority_order(self) -> None:
        router = LogParserRouter()
        calls = []

        class P1:
            def can_parse(self, log: str) -> bool:
                calls.append("p1")
                return False
            def parse(self, log: str) -> list:
                return []

        class P2:
            def can_parse(self, log: str) -> bool:
                calls.append("p2")
                return False
            def parse(self, log: str) -> list:
                return []

        router.register(P1(), priority=200)
        router.register(P2(), priority=50)
        router.parse("test")
        assert calls == ["p2", "p1"]

    def test_merges_results_from_multiple_parsers(self) -> None:
        class PA:
            def can_parse(self, log: str) -> bool:
                return True
            def parse(self, log: str) -> list:
                return [_pf("a")]

        class PB:
            def can_parse(self, log: str) -> bool:
                return True
            def parse(self, log: str) -> list:
                return [_pf("b")]

        router = LogParserRouter()
        router.register(PA())
        router.register(PB())
        failures, excerpt, unparseable = router.parse("log")
        assert len(failures) == 2
        assert not unparseable

    def test_deduplicates_across_parsers(self) -> None:
        class PA:
            def can_parse(self, log: str) -> bool:
                return True
            def parse(self, log: str) -> list:
                return [_pf("same_id")]

        class PB:
            def can_parse(self, log: str) -> bool:
                return True
            def parse(self, log: str) -> list:
                return [_pf("same_id")]

        router = LogParserRouter([PA(), PB()])
        failures, _, _ = router.parse("log")
        assert len(failures) == 1

    def test_unparseable_returns_excerpt(self) -> None:
        router = LogParserRouter()
        failures, excerpt, unparseable = router.parse("no match here\n" * 10)
        assert failures == []
        assert unparseable is True
        assert excerpt is not None

    def test_parser_exception_handled(self) -> None:
        class Bad:
            def can_parse(self, log: str) -> bool:
                return True
            def parse(self, log: str) -> list:
                raise RuntimeError("boom")

        router = LogParserRouter()
        router.register(Bad())
        failures, excerpt, unparseable = router.parse("test")
        assert failures == []
        assert unparseable is True


# =====================================================================
# 15. Config __post_init__ validation
# =====================================================================

class TestBotConfigPostInit:

    def test_negative_values_clamped(self) -> None:
        cfg = BotConfig(
            max_prs_per_day=-1,
            max_open_bot_prs=-5,
            max_failures_per_run=-10,
            max_retries_bedrock=-1,
            daily_token_budget=-100,
        )
        assert cfg.max_prs_per_day == 0
        assert cfg.max_open_bot_prs == 0
        assert cfg.max_failures_per_run == 0
        assert cfg.max_retries_bedrock == 0
        assert cfg.daily_token_budget == 0

    def test_thinking_budget_clamped(self) -> None:
        low = BotConfig(thinking_budget=100)
        assert low.thinking_budget == 1024
        high = BotConfig(thinking_budget=999_999)
        assert high.thinking_budget == 128_000

    def test_invalid_confidence_reset(self) -> None:
        cfg = BotConfig(confidence_threshold="invalid")
        assert cfg.confidence_threshold == "medium"

    def test_valid_confidence_preserved(self) -> None:
        for level in ("high", "medium", "low"):
            cfg = BotConfig(confidence_threshold=level)
            assert cfg.confidence_threshold == level

    def test_max_input_tokens_floor(self) -> None:
        cfg = BotConfig(max_input_tokens=-5)
        assert cfg.max_input_tokens == 1

    def test_max_output_tokens_floor(self) -> None:
        cfg = BotConfig(max_output_tokens=-1)
        assert cfg.max_output_tokens == 1

    def test_flaky_validation_passes_floor(self) -> None:
        cfg = BotConfig(flaky_validation_passes=0)
        assert cfg.flaky_validation_passes == 1


class TestReviewerConfigPostInit:

    def test_negative_values_clamped(self) -> None:
        cfg = ReviewerConfig(
            max_files=-1,
            max_review_comments=-1,
            bedrock_retries=-1,
            github_retries=-1,
            daily_token_budget=-1,
        )
        assert cfg.max_files == 1
        assert cfg.max_review_comments == 1
        assert cfg.bedrock_retries == 0
        assert cfg.github_retries == 0
        assert cfg.daily_token_budget == 0

    def test_max_retries_bedrock_property(self) -> None:
        cfg = ReviewerConfig(bedrock_retries=7)
        assert cfg.max_retries_bedrock == 7

    def test_token_floors(self) -> None:
        cfg = ReviewerConfig(max_input_tokens=-1, max_output_tokens=-1)
        assert cfg.max_input_tokens == 1
        assert cfg.max_output_tokens == 1

"""Tests for the AI fallback log parser.

Covers:
  - Happy path: valid JSON schema response → ParsedFailure list
  - Defensive: invalid JSON, wrong schema, empty response, model exceptions
  - Router integration: fallback is invoked only when deterministic parsers miss
"""

from __future__ import annotations

import json
import typing
from unittest.mock import MagicMock

# Python 3.7 typing.Protocol backfill
if not hasattr(typing, "Protocol"):
    try:
        from typing_extensions import Protocol as _Protocol
        typing.Protocol = _Protocol  # type: ignore[attr-defined]
    except ImportError:
        pass

from scripts.log_parser import LogParserRouter
from scripts.parsers.ai_fallback_parser import AIFallbackParser
from scripts.parsers.tcl_parser import TclTestParser


def _mock_bedrock(response_text: str) -> MagicMock:
    m = MagicMock()
    m.invoke.return_value = response_text
    return m


# --- Response parsing ---

def test_ai_fallback_returns_failures_from_valid_json():
    bedrock = _mock_bedrock(json.dumps({
        "failures": [
            {
                "test_name": "latency: measure",
                "file_path": "tests/unit/latency.tcl",
                "line_number": 42,
                "error_message": "Expected 1 got 0",
                "parser_type": "tcl",
            },
        ]
    }))
    parser = AIFallbackParser(bedrock)
    results = parser.parse("some unparseable log content")
    assert len(results) == 1
    assert results[0].test_name == "latency: measure"
    assert results[0].file_path == "tests/unit/latency.tcl"
    assert results[0].line_number == 42
    assert results[0].error_message == "Expected 1 got 0"
    assert results[0].parser_type == "tcl"
    # Identifier is prefixed with "ai:" so downstream de-dup can distinguish.
    assert results[0].failure_identifier.startswith("ai:")


def test_ai_fallback_empty_failures_list_returns_empty():
    bedrock = _mock_bedrock(json.dumps({"failures": []}))
    parser = AIFallbackParser(bedrock)
    assert parser.parse("content") == []


def test_ai_fallback_invalid_json_returns_empty():
    bedrock = _mock_bedrock("not json at all")
    parser = AIFallbackParser(bedrock)
    assert parser.parse("content") == []


def test_ai_fallback_strips_markdown_code_fences():
    """The model sometimes wraps its JSON in ``` fences despite the prompt."""
    bedrock = _mock_bedrock(
        "```json\n"
        + json.dumps({"failures": [{
            "test_name": "foo",
            "file_path": "tests/foo.tcl",
            "line_number": 1,
            "error_message": "bar",
            "parser_type": "tcl",
        }]})
        + "\n```"
    )
    parser = AIFallbackParser(bedrock)
    results = parser.parse("content")
    assert len(results) == 1


def test_ai_fallback_wrong_schema_returns_empty():
    bedrock = _mock_bedrock(json.dumps({"wrong_key": []}))
    parser = AIFallbackParser(bedrock)
    assert parser.parse("content") == []


def test_ai_fallback_bedrock_exception_returns_empty():
    bedrock = MagicMock()
    bedrock.invoke.side_effect = RuntimeError("bedrock unavailable")
    parser = AIFallbackParser(bedrock)
    assert parser.parse("content") == []


def test_ai_fallback_coerces_unknown_parser_type_to_other():
    bedrock = _mock_bedrock(json.dumps({"failures": [{
        "test_name": "foo", "file_path": "x",
        "line_number": None, "error_message": "fail",
        "parser_type": "invented",
    }]}))
    parser = AIFallbackParser(bedrock)
    results = parser.parse("content")
    assert results[0].parser_type == "other"


def test_ai_fallback_skips_failures_with_empty_error_message():
    bedrock = _mock_bedrock(json.dumps({"failures": [
        {
            "test_name": "foo", "file_path": "x",
            "line_number": None, "error_message": "",
            "parser_type": "other",
        },
        {
            "test_name": "bar", "file_path": "y",
            "line_number": None, "error_message": "real error",
            "parser_type": "other",
        },
    ]}))
    parser = AIFallbackParser(bedrock)
    results = parser.parse("content")
    assert len(results) == 1
    assert results[0].test_name == "bar"


def test_ai_fallback_dedupes_identical_identifiers():
    bedrock = _mock_bedrock(json.dumps({"failures": [
        {
            "test_name": "foo", "file_path": "x",
            "line_number": None, "error_message": "a",
            "parser_type": "other",
        },
        {
            "test_name": "foo", "file_path": "x",
            "line_number": None, "error_message": "b",
            "parser_type": "other",
        },
    ]}))
    parser = AIFallbackParser(bedrock)
    results = parser.parse("content")
    assert len(results) == 1


def test_ai_fallback_enforces_5_failure_cap():
    bedrock = _mock_bedrock(json.dumps({"failures": [
        {
            "test_name": f"t{i}", "file_path": f"f{i}.tcl",
            "line_number": None, "error_message": f"err {i}",
            "parser_type": "tcl",
        }
        for i in range(20)
    ]}))
    parser = AIFallbackParser(bedrock)
    results = parser.parse("content")
    assert len(results) == 5


def test_ai_fallback_empty_log_returns_empty_without_calling_model():
    bedrock = MagicMock()
    parser = AIFallbackParser(bedrock)
    assert parser.parse("") == []
    assert parser.parse("   \n  \n ") == []
    bedrock.invoke.assert_not_called()


def test_ai_fallback_truncates_input_to_max_chars():
    bedrock = _mock_bedrock(json.dumps({"failures": []}))
    parser = AIFallbackParser(bedrock, max_input_chars=100)
    # Feed a 5000-char log; the mock should receive only the last 100.
    parser.parse("x" * 5000)
    call_args = bedrock.invoke.call_args
    # BedrockClient.invoke(system_prompt, user_prompt, *, model_id=...)
    assert call_args is not None
    user_prompt = call_args[0][1]
    assert len(user_prompt) == 100
    assert user_prompt == "x" * 100


# --- Router integration ---

def test_router_invokes_fallback_only_when_deterministic_miss():
    """Fallback should NOT be called if a deterministic parser matched."""
    bedrock = _mock_bedrock(json.dumps({"failures": [{
        "test_name": "x", "file_path": "y",
        "line_number": None, "error_message": "z",
        "parser_type": "other",
    }]}))
    router = LogParserRouter()
    router.register(TclTestParser(), priority=10)
    router.set_fallback(AIFallbackParser(bedrock))

    # TclTestParser matches this content
    log = "[err]: Real test failed in tests/unit/foo.tcl\n"
    results, _, unparseable = router.parse(log)
    assert len(results) == 1
    assert not unparseable
    # Fallback should have been skipped entirely
    bedrock.invoke.assert_not_called()


def test_router_invokes_fallback_when_deterministic_misses():
    """Fallback SHOULD be called if no deterministic parser matched."""
    bedrock = _mock_bedrock(json.dumps({"failures": [{
        "test_name": "extracted by ai", "file_path": "tests/weird.log",
        "line_number": 99, "error_message": "mystery failure",
        "parser_type": "other",
    }]}))
    router = LogParserRouter()
    router.register(TclTestParser(), priority=10)
    router.set_fallback(AIFallbackParser(bedrock))

    # No deterministic parser matches this
    log = "Weird non-standard log output\nwith no known markers\n"
    results, _, unparseable = router.parse(log)
    bedrock.invoke.assert_called_once()
    assert len(results) == 1
    assert results[0].test_name == "extracted by ai"
    assert not unparseable


def test_router_without_fallback_marks_unparseable():
    """No fallback registered → behavior is the unchanged unparseable path."""
    router = LogParserRouter()
    router.register(TclTestParser(), priority=10)
    # no set_fallback call
    log = "Weird non-standard log output\nwith no known markers\n"
    results, excerpt, unparseable = router.parse(log)
    assert results == []
    assert unparseable is True
    assert excerpt is not None


def test_router_fallback_exception_does_not_crash():
    """If the fallback raises, the router falls back to unparseable excerpt."""
    bedrock = MagicMock()
    bedrock.invoke.side_effect = RuntimeError("boom")
    router = LogParserRouter()
    router.register(TclTestParser(), priority=10)
    router.set_fallback(AIFallbackParser(bedrock))
    results, excerpt, unparseable = router.parse("unknown log\n")
    assert results == []
    assert unparseable is True


# --- Config flag ---

def test_config_ai_fallback_parser_enabled_default_true():
    from scripts.config import BotConfig
    cfg = BotConfig()
    assert cfg.ai_fallback_parser_enabled is True


def test_config_ai_fallback_parser_enabled_can_be_disabled_via_yaml():
    from scripts.config import load_config_data
    cfg = load_config_data({"ai_fallback_parser_enabled": False})
    assert cfg.ai_fallback_parser_enabled is False

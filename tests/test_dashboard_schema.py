"""Tests for the dashboard JSON schema validator."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.validate_dashboard_schema import validate

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "dashboard"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def full():
    return _load("full.json")


@pytest.fixture
def empty():
    return _load("empty.json")


@pytest.fixture
def partial():
    return _load("partial.json")


# --- Positive cases ---

def test_full_fixture_is_valid(full):
    assert validate(full) == []


def test_empty_fixture_is_valid(empty):
    assert validate(empty) == []


def test_partial_fixture_is_valid(partial):
    assert validate(partial) == []


# --- Negative cases ---

def test_missing_schema_version(full):
    data = copy.deepcopy(full)
    del data["schema_version"]
    errors = validate(data)
    assert any("schema_version" in e for e in errors)


def test_wrong_schema_version(full):
    data = copy.deepcopy(full)
    data["schema_version"] = 2
    errors = validate(data)
    assert any("schema_version must be 1" in e for e in errors)


def test_missing_required_section(full):
    data = copy.deepcopy(full)
    del data["ci_failures"]
    errors = validate(data)
    assert any("ci_failures" in e for e in errors)


def test_snapshot_type_error(full):
    data = copy.deepcopy(full)
    data["snapshot"] = "not a dict"
    errors = validate(data)
    assert any("snapshot must be an object" in e for e in errors)


def test_snapshot_field_type_error(full):
    data = copy.deepcopy(full)
    data["snapshot"]["failure_incidents"] = "not an int"
    errors = validate(data)
    assert any("snapshot.failure_incidents must be int" in e for e in errors)


def test_section_int_field_type_error(full):
    data = copy.deepcopy(full)
    data["fuzzer"]["runs_analyzed"] = "bad"
    errors = validate(data)
    assert any("fuzzer.runs_analyzed must be int" in e for e in errors)


def test_section_list_field_type_error(full):
    data = copy.deepcopy(full)
    data["agent_outcomes"]["recent_events"] = "not a list"
    errors = validate(data)
    assert any("agent_outcomes.recent_events must be a list" in e for e in errors)


def test_wow_trends_has_data_type_error(full):
    data = copy.deepcopy(full)
    data["wow_trends"]["has_data"] = "yes"
    errors = validate(data)
    assert any("wow_trends.has_data must be bool" in e for e in errors)


def test_generated_at_must_be_string(full):
    data = copy.deepcopy(full)
    data["generated_at"] = 12345
    errors = validate(data)
    assert any("generated_at must be a string" in e for e in errors)


def test_non_dict_root_fails():
    errors = validate("not a dict")
    assert errors == ["root must be an object"]


def test_full_fixture_has_xss_payloads(full):
    """Verify the full fixture contains XSS test strings for frontend testing."""
    text = json.dumps(full)
    assert "<script>" in text
    assert "onerror=" in text

"""Tests for the CI agent event ledger."""

from __future__ import annotations

import builtins
import importlib
import json
import sys
from unittest.mock import MagicMock

from scripts.event_ledger import EventLedger, make_event, parse_events


def test_make_event_is_json_serializable() -> None:
    event = make_event(
        "validation.passed",
        "fp-1",
        created_at="2026-04-08T00:00:00+00:00",
        job_name="daily / linux",
    )

    payload = event.to_dict()

    assert payload["event_type"] == "validation.passed"
    assert payload["subject"] == "fp-1"
    assert payload["attributes"]["job_name"] == "daily / linux"
    assert payload["event_id"]


def test_parse_events_skips_malformed_lines() -> None:
    text = "\n".join([
        json.dumps({"event_id": "ok", "event_type": "pr.created"}),
        "not-json",
        "",
    ])

    events = parse_events(text)

    assert events == [{"event_id": "ok", "event_type": "pr.created"}]


def test_event_ledger_appends_pending_events_to_remote_file() -> None:
    contents = MagicMock()
    contents.sha = "sha-1"
    contents.decoded_content = (
        json.dumps({
            "event_id": "existing",
            "event_type": "failure.observed",
            "created_at": "2026-04-08T00:00:00+00:00",
            "subject": "fp-old",
            "attributes": {},
        }) + "\n"
    ).encode("utf-8")
    repo = MagicMock()
    repo.get_contents.return_value = contents
    gh = MagicMock()
    gh.get_repo.return_value = repo
    ledger = EventLedger(gh, "owner/repo")

    ledger.record(
        "validation.passed",
        "fp-1",
        created_at="2026-04-08T00:01:00+00:00",
        job_name="daily / linux",
    )
    ledger.save()

    repo.update_file.assert_called_once()
    updated_content = repo.update_file.call_args.args[2]
    events = parse_events(updated_content)
    assert [event["event_type"] for event in events] == [
        "failure.observed",
        "validation.passed",
    ]
    assert ledger.pending == []


def test_parse_events_can_import_without_pygithub(monkeypatch) -> None:
    original_import = builtins.__import__

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "github.GithubException":
            raise ModuleNotFoundError("No module named 'github'")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "scripts.event_ledger", raising=False)
    monkeypatch.setattr(builtins, "__import__", _guarded_import)
    module = importlib.import_module("scripts.event_ledger")

    events = module.parse_events('{"event_id":"evt","event_type":"review.summary_posted"}\n')

    assert events == [{"event_id": "evt", "event_type": "review.summary_posted"}]

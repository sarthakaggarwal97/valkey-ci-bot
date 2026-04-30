"""Tests for FailureStore.compact() eviction logic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# failure_store imports github at top-level — skip if unavailable locally.
pytest.importorskip("github")

from scripts.failure_store import FailureStore  # noqa: E402
from scripts.models import FailureStoreEntry, FlakyCampaignState  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _entry(fingerprint: str, status: str, age_days: int, now: datetime) -> FailureStoreEntry:
    updated = now - timedelta(days=age_days)
    return FailureStoreEntry(
        fingerprint=fingerprint,
        failure_identifier=f"test-{fingerprint}",
        test_name=None,
        incident_key=fingerprint,
        error_signature=f"sig-{fingerprint}",
        file_path="src/x.c",
        pr_url=None,
        status=status,
        created_at=_iso(updated),
        updated_at=_iso(updated),
    )


def _campaign(fingerprint: str, status: str, age_days: int, now: datetime) -> FlakyCampaignState:
    updated = now - timedelta(days=age_days)
    return FlakyCampaignState(
        fingerprint=fingerprint,
        history_key=fingerprint,
        failure_identifier=f"test-{fingerprint}",
        workflow_file="daily.yml",
        job_name="job",
        matrix_params={},
        repo_full_name="valkey-io/valkey",
        branch="unstable",
        status=status,
        created_at=_iso(updated),
        updated_at=_iso(updated),
    )


def test_compact_evicts_old_terminal_entries():
    """Old terminal entries get evicted by age."""
    store = FailureStore()
    now = datetime.now(timezone.utc)
    # Three entries: old-terminal, young-terminal, old-active
    store._entries = {
        "old-merged": _entry("old-merged", "merged", 120, now),
        "young-merged": _entry("young-merged", "merged", 30, now),
        "old-processing": _entry("old-processing", "processing", 120, now),
    }
    result = store.compact(max_age_days=90, now=now)
    assert result["evicted_entries"] == 1
    assert "old-merged" not in store._entries
    assert "young-merged" in store._entries
    # Active entries are preserved regardless of age.
    assert "old-processing" in store._entries


def test_compact_enforces_max_entries():
    """Size cap trims oldest terminal entries even if within age window."""
    store = FailureStore()
    now = datetime.now(timezone.utc)
    # 5 terminal entries of varying age, all within age window.
    store._entries = {
        f"e{i}": _entry(f"e{i}", "merged", i, now)
        for i in range(5)
    }
    result = store.compact(max_entries=3, max_age_days=90, now=now)
    assert result["evicted_entries"] == 2
    assert len(store._entries) == 3
    # Oldest (e4, e3) should have been evicted.
    assert "e4" not in store._entries
    assert "e3" not in store._entries
    assert "e0" in store._entries


def test_compact_preserves_active_over_cap():
    """Size cap never evicts non-terminal entries."""
    store = FailureStore()
    now = datetime.now(timezone.utc)
    # 5 active (processing) + 2 old merged — max_entries=3 is impossible
    # without evicting active; only the 2 terminal can be dropped.
    store._entries = {
        f"a{i}": _entry(f"a{i}", "processing", i, now)
        for i in range(5)
    }
    store._entries["m0"] = _entry("m0", "merged", 10, now)
    store._entries["m1"] = _entry("m1", "merged", 20, now)
    store.compact(max_entries=3, max_age_days=90, now=now)
    # Active preserved.
    for i in range(5):
        assert f"a{i}" in store._entries
    # Both terminal dropped.
    assert "m0" not in store._entries
    assert "m1" not in store._entries


def test_compact_evicts_old_terminal_campaigns():
    """Old terminal campaigns get evicted by age."""
    store = FailureStore()
    now = datetime.now(timezone.utc)
    store._campaigns = {
        "old-landed": _campaign("old-landed", "landed", 120, now),
        "young-landed": _campaign("young-landed", "landed", 30, now),
        "old-active": _campaign("old-active", "active", 120, now),
    }
    result = store.compact(max_age_days=90, now=now)
    assert result["evicted_campaigns"] == 1
    assert "old-landed" not in store._campaigns
    assert "young-landed" in store._campaigns
    assert "old-active" in store._campaigns


def test_compact_noop_when_within_limits():
    """Nothing is evicted when state is within all limits."""
    store = FailureStore()
    now = datetime.now(timezone.utc)
    store._entries = {"e1": _entry("e1", "merged", 10, now)}
    result = store.compact(max_entries=500, max_age_days=90, now=now)
    assert result == {
        "evicted_entries": 0,
        "evicted_campaigns": 0,
        "evicted_history": 0,
    }
    assert "e1" in store._entries


def test_compact_handles_bad_updated_at():
    """Entries with malformed updated_at are skipped, not crashed on."""
    store = FailureStore()
    now = datetime.now(timezone.utc)
    entry = _entry("bad", "merged", 120, now)
    entry.updated_at = "not a date"
    store._entries = {"bad": entry}
    # Should not raise, just skip.
    result = store.compact(max_age_days=90, now=now)
    assert result["evicted_entries"] == 0
    assert "bad" in store._entries

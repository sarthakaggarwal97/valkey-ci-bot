# Feature: valkey-ci-bot, Property 20: Failure store serialization round-trip
"""Property tests for failure store serialization round-trip.

Property 20: For any FailureStore containing arbitrary entries, serializing
to JSON and deserializing should produce an equivalent store with all entries
preserved.

**Validates: Requirements 9.5**
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.failure_store import FailureStore
from scripts.models import FailureStoreEntry, FailureReport, RootCauseReport

# --- Strategies ---

_status_values = st.sampled_from(["open", "merged", "abandoned", "processing"])

_optional_text = st.one_of(st.none(), st.text(min_size=1, max_size=60))

_iso_datetime = st.from_regex(
    r"20[0-9]{2}-[01][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-5][0-9]\+00:00",
    fullmatch=True,
)

_entry_strategy = st.fixed_dictionaries(
    {
        "fingerprint": st.text(min_size=1, max_size=64),
        "failure_identifier": st.text(min_size=1, max_size=80),
        "test_name": _optional_text,
        "error_signature": st.text(min_size=1, max_size=100),
        "file_path": st.text(
            alphabet=st.characters(min_codepoint=32, max_codepoint=126),
            min_size=1,
            max_size=80,
        ),
        "pr_url": _optional_text,
        "status": _status_values,
        "created_at": _iso_datetime,
        "updated_at": _iso_datetime,
    }
)


def _entries_strategy():
    """Generate a dict of fingerprint -> FailureStoreEntry."""
    return st.dictionaries(
        keys=st.text(min_size=1, max_size=64),
        values=_entry_strategy,
        min_size=0,
        max_size=10,
    )


# --- Property Tests ---


@settings(max_examples=100)
@given(entries=_entries_strategy())
def test_failure_store_serialization_round_trip(
    entries: dict[str, dict],
) -> None:
    """Property 20: Serializing and deserializing a FailureStore preserves all entries.

    **Validates: Requirements 9.5**
    """
    # Build a FailureStore with the generated entries
    store = FailureStore()
    for fp, raw in entries.items():
        store.entries[fp] = FailureStoreEntry(
            fingerprint=raw["fingerprint"],
            failure_identifier=raw["failure_identifier"],
            test_name=raw["test_name"],
            error_signature=raw["error_signature"],
            file_path=raw["file_path"],
            pr_url=raw["pr_url"],
            status=raw["status"],
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
        )

    # Serialize
    serialized = store.to_dict()

    # Deserialize into a fresh store
    restored = FailureStore()
    restored.from_dict(serialized)

    # Verify entry count matches
    assert len(restored.entries) == len(store.entries)

    # Verify every entry is preserved field-by-field
    for fp, original in store.entries.items():
        assert fp in restored.entries, f"Missing fingerprint key: {fp}"
        restored_entry = restored.entries[fp]
        assert restored_entry.fingerprint == original.fingerprint
        assert restored_entry.failure_identifier == original.failure_identifier
        assert restored_entry.test_name == original.test_name
        assert restored_entry.error_signature == original.error_signature
        assert restored_entry.file_path == original.file_path
        assert restored_entry.pr_url == original.pr_url
        assert restored_entry.status == original.status
        assert restored_entry.created_at == original.created_at
        assert restored_entry.updated_at == original.updated_at
        assert restored_entry.queued_pr_payload == original.queued_pr_payload


def test_record_queued_pr_persists_payload() -> None:
    store = FailureStore()
    report = FailureReport(
        workflow_name="CI",
        job_name="job",
        matrix_params={},
        commit_sha="abc123",
        failure_source="trusted",
        repo_full_name="owner/repo",
        workflow_run_id=7,
        target_branch="unstable",
    )
    root_cause = RootCauseReport(
        description="root cause",
        files_to_change=["src/foo.c"],
        confidence="high",
        rationale="because",
        is_flaky=False,
        flakiness_indicators=None,
    )

    store.record_queued_pr("fp1", report, root_cause, "diff", "unstable")

    entry = store.entries["fp1"]
    assert entry.status == "queued"
    assert entry.queued_pr_payload is not None
    assert entry.queued_pr_payload["patch"] == "diff"


def test_save_creates_bot_data_branch_when_missing() -> None:
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_git_ref.side_effect = [
        Exception("missing bot-data"),
        MagicMock(object=MagicMock(sha="base-sha")),
    ]
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = FailureStore(gh, "owner/repo")

    store.save()

    repo.create_git_ref.assert_called_once_with(
        ref="refs/heads/bot-data",
        sha="base-sha",
    )

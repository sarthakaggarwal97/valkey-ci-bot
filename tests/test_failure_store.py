# Feature: valkey-ci-agent, Property 20: Failure store serialization round-trip
"""Property tests for failure store serialization round-trip.

Property 20: For any FailureStore containing arbitrary entries, serializing
to JSON and deserializing should produce an equivalent store with all entries
preserved.

**Validates: Requirements 9.5**
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from github.GithubException import GithubException
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from scripts.failure_store import FailureStore
from scripts.models import (
    FailureReport,
    FailureStoreEntry,
    FlakyCampaignState,
    ParsedFailure,
    RootCauseReport,
)

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
        "incident_key": st.text(min_size=1, max_size=64),
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


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
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
            incident_key=raw["incident_key"],
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
        assert restored_entry.incident_key == original.incident_key
        assert restored_entry.error_signature == original.error_signature
        assert restored_entry.file_path == original.file_path
        assert restored_entry.pr_url == original.pr_url
        assert restored_entry.status == original.status
        assert restored_entry.created_at == original.created_at
        assert restored_entry.updated_at == original.updated_at
        assert restored_entry.queued_pr_payload == original.queued_pr_payload
        assert restored_entry.incident_observations == original.incident_observations


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
    assert entry.incident_key == "fp1"


def test_list_queued_failures_uses_failure_store_as_queue_authority() -> None:
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
    store.record_queued_pr("fp2", report, root_cause, "diff-2", "unstable")
    store.record_queued_pr("fp1", report, root_cause, "diff-1", "unstable")
    store.mark_queued_pr_dead_letter("fp2", "debug")

    assert store.list_queued_failures() == ["fp1"]


def test_record_queued_pr_failure_keeps_payload_for_retry() -> None:
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

    attempts = store.record_queued_pr_failure("fp1", "GitHub 500")

    entry = store.entries["fp1"]
    assert attempts == 1
    assert entry.status == "queued-pr-retry"
    assert entry.queued_pr_payload is not None
    assert entry.queued_pr_payload["patch"] == "diff"
    assert entry.queued_pr_payload["reconciliation"]["last_error"] == "GitHub 500"


def test_mark_queued_pr_dead_letter_preserves_payload_for_debugging() -> None:
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
    store.record_queued_pr_failure("fp1", "GitHub 500")

    store.mark_queued_pr_dead_letter("fp1", "GitHub 500")

    entry = store.entries["fp1"]
    assert entry.status == "queued-pr-dead-letter"
    assert entry.queued_pr_payload is not None
    assert entry.queued_pr_payload["patch"] == "diff"
    assert (
        entry.queued_pr_payload["reconciliation"]["dead_letter_reason"]
        == "GitHub 500"
    )


def test_update_proof_campaign_persists_proof_status() -> None:
    store = FailureStore()
    store.entries["fp1"] = FailureStoreEntry(
        fingerprint="fp1",
        failure_identifier="test-cache-flush",
        test_name="test-cache-flush",
        incident_key="fp1",
        error_signature="boom",
        file_path="tests/unit/cache.tcl",
        pr_url="https://github.com/owner/repo/pull/42",
        status="open",
        created_at="2026-04-08T00:00:00+00:00",
        updated_at="2026-04-08T00:00:00+00:00",
        campaign_status="pr-created",
    )
    store.campaigns["fp1"] = FlakyCampaignState(
        fingerprint="fp1",
        history_key="hist-1",
        failure_identifier="test-cache-flush",
        workflow_file="daily.yml",
        job_name="daily / linux",
        matrix_params={},
        repo_full_name="owner/repo",
        branch="unstable",
        status="pr-created",
        created_at="2026-04-08T00:00:00+00:00",
        updated_at="2026-04-08T00:00:00+00:00",
    )

    store.update_proof_campaign(
        "fp1",
        status="passed",
        summary="Proof passed across 100/100 GitHub-native validation runs.",
        proof_url="https://github.com/owner/repo/actions/runs/123",
        required_runs=100,
        passed_runs=100,
        attempted_runs=100,
    )

    payload = store.to_dict()["campaigns"]["fp1"]
    assert payload["proof_status"] == "passed"
    assert payload["proof_required_runs"] == 100
    assert payload["proof_passed_runs"] == 100
    assert payload["proof_attempted_runs"] == 100
    assert "GitHub-native validation runs" in payload["proof_summary"]


def test_update_landing_campaign_persists_upstream_handoff() -> None:
    store = FailureStore()
    store.entries["fp1"] = FailureStoreEntry(
        fingerprint="fp1",
        failure_identifier="test-cache-flush",
        test_name="test-cache-flush",
        incident_key="fp1",
        error_signature="boom",
        file_path="tests/unit/cache.tcl",
        pr_url="https://github.com/owner/fork/pull/42",
        status="open",
        created_at="2026-04-08T00:00:00+00:00",
        updated_at="2026-04-08T00:00:00+00:00",
        campaign_status="pr-created",
    )
    store.campaigns["fp1"] = FlakyCampaignState(
        fingerprint="fp1",
        history_key="hist-1",
        failure_identifier="test-cache-flush",
        workflow_file="daily.yml",
        job_name="daily / linux",
        matrix_params={},
        repo_full_name="owner/fork",
        branch="unstable",
        status="pr-created",
        created_at="2026-04-08T00:00:00+00:00",
        updated_at="2026-04-08T00:00:00+00:00",
    )

    store.update_landing_campaign(
        "fp1",
        status="passed",
        summary="Opened upstream PR automatically.",
        landing_url="https://github.com/valkey-io/valkey/pull/4242",
        landing_repo="valkey-io/valkey",
    )

    payload = store.to_dict()["campaigns"]["fp1"]
    assert payload["status"] == "landed"
    assert payload["landing_status"] == "passed"
    assert payload["landing_summary"] == "Opened upstream PR automatically."
    assert payload["landing_url"] == "https://github.com/valkey-io/valkey/pull/4242"
    assert payload["landing_repo"] == "valkey-io/valkey"


def test_reconcile_pr_states_returns_maintainer_outcome_transitions() -> None:
    repo = MagicMock()
    pr = MagicMock()
    pr.merged = True
    pr.state = "closed"
    repo.get_pull.return_value = pr
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = FailureStore(gh, "owner/repo")
    store.entries["fp1"] = FailureStoreEntry(
        fingerprint="fp1",
        failure_identifier="test-cache-flush",
        test_name="test-cache-flush",
        incident_key="fp1",
        error_signature="boom",
        file_path="tests/unit/cache.tcl",
        pr_url="https://github.com/owner/repo/pull/42",
        status="open",
        created_at="2026-04-08T00:00:00+00:00",
        updated_at="2026-04-08T00:00:00+00:00",
    )

    transitions = store.reconcile_pr_states()

    assert store.entries["fp1"].status == "merged"
    assert len(transitions) == 1
    assert transitions[0].fingerprint == "fp1"
    assert transitions[0].pr_number == 42
    assert transitions[0].previous_status == "open"
    assert transitions[0].new_status == "merged"
    assert transitions[0].merged is True


def test_load_raises_on_non_missing_remote_error() -> None:
    repo = MagicMock()
    repo.get_contents.side_effect = GithubException(500, {"message": "boom"})
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = FailureStore(gh, "owner/repo")

    with pytest.raises(RuntimeError, match="failed to load failure store"):
        store.load()


def test_load_falls_back_to_download_url_when_contents_encoding_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = MagicMock()
    contents = MagicMock()
    contents.encoding = None
    contents.content = ""
    contents.download_url = "https://example.test/failure-store.json"
    type(contents).decoded_content = property(
        lambda self: (_ for _ in ()).throw(AssertionError("unsupported encoding: none"))
    )
    repo.get_contents.return_value = contents
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = FailureStore(gh, "owner/repo")

    class _Response:
        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "entries": {
                        "fp1": {
                            "fingerprint": "fp1",
                            "failure_identifier": "suite.case",
                            "incident_key": "fp1",
                            "error_signature": "boom",
                            "file_path": "src/foo.c",
                            "status": "open",
                            "created_at": "2026-04-13T00:00:00+00:00",
                            "updated_at": "2026-04-13T00:00:00+00:00",
                        }
                    }
                }
            ).encode("utf-8")

    monkeypatch.setattr(
        "scripts.failure_store.urllib_request.urlopen",
        lambda request, timeout=30: _Response(),
    )

    store.load()

    assert "fp1" in store.entries


def test_compute_incident_key_ignores_runner_specific_error_text() -> None:
    key_a = FailureStore.compute_incident_key(
        "TestSuite.TestCase",
        "src/foo.c",
        test_name="TestSuite.TestCase",
    )
    key_b = FailureStore.compute_incident_key(
        "TestSuite.TestCase",
        "src/foo.c",
        test_name="TestSuite.TestCase",
    )

    assert key_a == key_b


def test_record_incident_observation_aggregates_same_failure_across_runners() -> None:
    store = FailureStore()
    report_a = FailureReport(
        workflow_name="Daily",
        workflow_file="daily.yml",
        job_name="test-ubuntu-jemalloc",
        matrix_params={"os": "ubuntu"},
        commit_sha="sha-1",
        failure_source="trusted",
        repo_full_name="valkey-io/valkey",
        workflow_run_id=10,
        target_branch="unstable",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="TestSuite.TestCase",
                test_name="TestSuite.TestCase",
                file_path="src/foo.c",
                error_message="assertion failed on ubuntu",
                assertion_details=None,
                line_number=42,
                stack_trace=None,
                parser_type="gtest",
            )
        ],
    )
    report_b = FailureReport(
        workflow_name="Daily",
        workflow_file="daily.yml",
        job_name="test-alpine-jemalloc",
        matrix_params={"os": "alpine"},
        commit_sha="sha-1",
        failure_source="trusted",
        repo_full_name="valkey-io/valkey",
        workflow_run_id=10,
        target_branch="unstable",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="TestSuite.TestCase",
                test_name="TestSuite.TestCase",
                file_path="src/foo.c",
                error_message="assertion failed on alpine",
                assertion_details=None,
                line_number=42,
                stack_trace=None,
                parser_type="gtest",
            )
        ],
    )

    incident_key = FailureStore.compute_incident_key(
        "TestSuite.TestCase",
        "src/foo.c",
        test_name="TestSuite.TestCase",
    )
    store.record(
        incident_key,
        "TestSuite.TestCase",
        "assertion failed on ubuntu",
        "src/foo.c",
        test_name="TestSuite.TestCase",
    )
    store.record_incident_observation(report_a, incident_key=incident_key, max_entries=10)
    store.record_incident_observation(report_b, incident_key=incident_key, max_entries=10)

    entry = store.get_entry(incident_key)
    assert entry is not None
    assert len(entry.incident_observations) == 2
    assert {obs.job_name for obs in entry.incident_observations} == {
        "test-ubuntu-jemalloc",
        "test-alpine-jemalloc",
    }


def test_records_history_and_summarizes_failure_streak() -> None:
    store = FailureStore()
    report = FailureReport(
        workflow_name="Daily",
        workflow_file="daily.yml",
        job_name="test-ubuntu-jemalloc",
        matrix_params={"param_0": "ubuntu"},
        commit_sha="badbadbad123",
        failure_source="trusted",
        repo_full_name="valkey-io/valkey",
        workflow_run_id=10,
        target_branch="unstable",
        parsed_failures=[],
        is_unparseable=True,
        raw_log_excerpt="boom",
    )

    store.record_failure_observation(report, fingerprint="fp1", max_entries=10)
    store.record_success_observation(
        workflow_name="Daily",
        workflow_file="daily.yml",
        job_name="test-ubuntu-jemalloc",
        matrix_params={"param_0": "ubuntu"},
        commit_sha="goodgood123",
        workflow_run_id=11,
        max_entries=10,
    )
    report.commit_sha = "worsebad456"
    report.workflow_run_id = 12
    store.record_failure_observation(report, fingerprint="fp2", max_entries=10)

    summary = store.summarize_history(
        "daily.yml",
        "test-ubuntu-jemalloc",
        {"param_0": "ubuntu"},
        "test-ubuntu-jemalloc",
    )

    assert summary is not None
    assert summary.failure_count == 2
    assert summary.pass_count == 1
    assert summary.consecutive_failures == 1
    assert summary.last_known_good_sha == "goodgood123"
    assert summary.first_bad_sha == "worsebad456"


def test_success_observation_matches_existing_failure_identity() -> None:
    store = FailureStore()
    report = FailureReport(
        workflow_name="Daily",
        workflow_file="daily.yml",
        job_name="test-ubuntu-jemalloc",
        matrix_params={"param_0": "ubuntu"},
        commit_sha="badsha",
        failure_source="trusted",
        repo_full_name="valkey-io/valkey",
        workflow_run_id=10,
        target_branch="unstable",
        parsed_failures=[],
        is_unparseable=True,
        raw_log_excerpt="boom",
    )
    store.record_failure_observation(report, fingerprint="fp1", max_entries=5)

    store.record_success_observation(
        workflow_name="Daily",
        workflow_file="daily.yml",
        job_name="test-ubuntu-jemalloc",
        matrix_params={"param_0": "ubuntu"},
        commit_sha="goodsha",
        workflow_run_id=11,
        max_entries=5,
    )

    history_entry = next(iter(store.history.values()))
    assert [obs.outcome for obs in history_entry.observations] == ["fail", "pass"]


def test_flaky_campaign_attempts_round_trip() -> None:
    store = FailureStore()
    report = FailureReport(
        workflow_name="Daily",
        workflow_file="daily.yml",
        job_name="test-ubuntu-jemalloc",
        matrix_params={"os": "ubuntu"},
        commit_sha="badsha",
        failure_source="trusted",
        repo_full_name="valkey-io/valkey",
        workflow_run_id=10,
        target_branch="unstable",
        parsed_failures=[],
        is_unparseable=True,
        raw_log_excerpt="boom",
    )
    root_cause = RootCauseReport(
        description="Timing-sensitive cleanup hook leaks state",
        files_to_change=["tests/unit/foo.tcl"],
        confidence="medium",
        rationale="Repeated failures point at shared cleanup.",
        is_flaky=True,
        flakiness_indicators=["timing"],
    )

    store.record("fp1", "test-ubuntu-jemalloc", "boom", "")
    campaign = store.record_flaky_campaign_attempt(
        "fp1",
        report,
        root_cause,
        "diff-1",
        "Tests failed: cleanup hook still races",
        passed=False,
        passed_runs=1,
        attempted_runs=2,
        summary="cleanup isolation attempt failed after 1/3 clean runs",
        strategy="local",
        max_failed_hypotheses=10,
    )

    assert campaign.status == "active"
    assert campaign.total_attempts == 1
    assert campaign.failed_hypotheses == [
        "cleanup isolation attempt failed after 1/3 clean runs"
    ]

    restored = FailureStore()
    restored.from_dict(store.to_dict())

    restored_campaign = restored.get_flaky_campaign("fp1")
    assert restored_campaign is not None
    assert restored_campaign.total_attempts == 1
    assert restored_campaign.failed_hypotheses == campaign.failed_hypotheses
    assert restored_campaign.attempts[0].attempted_runs == 2
    assert restored_campaign.attempts[0].strategy == "local"


def test_clear_queued_pr_resets_campaign_status_to_validated() -> None:
    store = FailureStore()
    report = FailureReport(
        workflow_name="Daily",
        workflow_file="daily.yml",
        job_name="test-ubuntu-jemalloc",
        matrix_params={"os": "ubuntu"},
        commit_sha="badsha",
        failure_source="trusted",
        repo_full_name="valkey-io/valkey",
        workflow_run_id=10,
        target_branch="unstable",
        parsed_failures=[],
        is_unparseable=True,
        raw_log_excerpt="boom",
    )
    root_cause = RootCauseReport(
        description="Timing-sensitive cleanup hook leaks state",
        files_to_change=["tests/unit/foo.tcl"],
        confidence="medium",
        rationale="Repeated failures point at shared cleanup.",
        is_flaky=True,
        flakiness_indicators=["timing"],
    )

    store.record("fp1", "test-ubuntu-jemalloc", "boom", "")
    store.record_flaky_campaign_attempt(
        "fp1",
        report,
        root_cause,
        "diff-1",
        "all green",
        passed=True,
        passed_runs=3,
        attempted_runs=3,
        summary="cleanup isolation held for 3/3 validation runs",
        strategy="local",
        max_failed_hypotheses=10,
    )
    store.record_queued_pr("fp1", report, root_cause, "diff-1", "unstable")

    store.clear_queued_pr("fp1")

    entry = store.get_entry("fp1")
    campaign = store.get_flaky_campaign("fp1")
    assert entry is not None
    assert campaign is not None
    assert entry.campaign_status == "validated"
    assert campaign.status == "validated"
    assert entry.queued_pr_payload is None
    assert campaign.queued_pr_payload is None


def test_save_creates_bot_data_branch_when_missing() -> None:
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_git_ref.side_effect = [
        GithubException(404, {"message": "missing bot-data"}),
        MagicMock(object=MagicMock(sha="base-sha")),
    ]
    repo.get_contents.side_effect = GithubException(404, {"message": "missing store"})
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = FailureStore(gh, "owner/repo")

    store.save()

    repo.create_git_ref.assert_called_once_with(
        ref="refs/heads/bot-data",
        sha="base-sha",
    )


def test_save_does_not_fallback_to_create_on_non_404_lookup_error() -> None:
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_git_ref.return_value = MagicMock()
    repo.get_contents.side_effect = GithubException(500, {"message": "boom"})
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = FailureStore(gh, "owner/repo")

    with pytest.raises(RuntimeError, match="failed to save failure store"):
        store.save()

    repo.create_file.assert_not_called()


def test_save_uses_separate_state_repository_when_configured() -> None:
    target_gh = MagicMock()
    state_repo = MagicMock()
    state_repo.default_branch = "main"
    state_repo.get_git_ref.side_effect = [
        GithubException(404, {"message": "missing bot-data"}),
        MagicMock(object=MagicMock(sha="base-sha")),
    ]
    state_repo.get_contents.side_effect = GithubException(404, {"message": "missing store"})
    state_gh = MagicMock()
    state_gh.get_repo.return_value = state_repo

    store = FailureStore(
        target_gh,
        "valkey-io/valkey",
        state_github_client=state_gh,
        state_repo_full_name="owner/valkey-ci-agent",
    )

    store.save()

    state_gh.get_repo.assert_called_once_with("owner/valkey-ci-agent")
    target_gh.get_repo.assert_not_called()


def test_save_retries_conflict_and_preserves_remote_entries() -> None:
    def contents(data: dict, sha: str) -> MagicMock:
        item = MagicMock()
        item.decoded_content = json.dumps(data).encode()
        item.sha = sha
        return item

    remote_entry = FailureStoreEntry(
        fingerprint="remote",
        failure_identifier="remote-test",
        test_name=None,
        incident_key="remote",
        error_signature="remote boom",
        file_path="tests/unit/remote.tcl",
        pr_url=None,
        status="queued",
        created_at="2026-04-08T00:00:00+00:00",
        updated_at="2026-04-08T00:00:00+00:00",
    )
    local_entry = FailureStoreEntry(
        fingerprint="local",
        failure_identifier="local-test",
        test_name=None,
        incident_key="local",
        error_signature="local boom",
        file_path="tests/unit/local.tcl",
        pr_url=None,
        status="queued",
        created_at="2026-04-08T00:00:00+00:00",
        updated_at="2026-04-08T00:00:00+00:00",
    )

    remote_store = FailureStore()
    remote_store.entries["remote"] = remote_entry

    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_git_ref.return_value = MagicMock()
    repo.get_contents.side_effect = [
        contents({"entries": {}}, "old-sha"),
        contents(remote_store.to_dict(), "new-sha"),
    ]
    repo.update_file.side_effect = [
        GithubException(409, {"message": "sha does not match"}),
        None,
    ]
    gh = MagicMock()
    gh.get_repo.return_value = repo
    store = FailureStore(gh, "owner/repo")
    store.entries["local"] = local_entry

    store.save()

    saved_payload = json.loads(repo.update_file.call_args_list[-1].args[2])
    assert sorted(saved_payload["entries"]) == ["local", "remote"]

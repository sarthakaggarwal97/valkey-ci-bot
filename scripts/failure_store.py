"""Failure deduplication and tracking store."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from dataclasses import asdict
from typing import TYPE_CHECKING

from github.GithubException import GithubException

from scripts.models import (
    FailureHistoryEntry,
    FailureHistorySummary,
    FailureObservation,
    FailureReport,
    FailureStoreEntry,
    RootCauseReport,
    failure_report_to_dict,
    root_cause_report_to_dict,
)

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)

_STORE_BRANCH = "bot-data"
_STORE_FILE = "failure-store.json"


class FailureStore:
    """Persistent failure tracking backed by a JSON file on a dedicated branch."""

    def __init__(
        self,
        github_client: Github | None = None,
        repo_full_name: str = "",
        *,
        state_github_client: Github | None = None,
        state_repo_full_name: str | None = None,
    ) -> None:
        self._gh = github_client
        self._repo_name = repo_full_name
        self._state_gh = state_github_client or github_client
        self._state_repo_name = state_repo_full_name or repo_full_name
        self._entries: dict[str, FailureStoreEntry] = {}
        self._history: dict[str, FailureHistoryEntry] = {}

    @property
    def entries(self) -> dict[str, FailureStoreEntry]:
        return self._entries

    @property
    def history(self) -> dict[str, FailureHistoryEntry]:
        return self._history

    @staticmethod
    def compute_fingerprint(
        failure_identifier: str, error_signature: str, file_path: str
    ) -> str:
        """SHA-256 of (failure_identifier, error_signature, file_path)."""
        payload = f"{failure_identifier}\0{error_signature}\0{file_path}"
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def compute_history_key(
        workflow_file: str,
        job_name: str,
        matrix_params: dict[str, str],
        failure_identifier: str,
    ) -> str:
        """Stable identity for timeline tracking across workflow runs."""
        matrix_blob = ",".join(
            f"{key}={value}" for key, value in sorted(matrix_params.items())
        )
        payload = "\0".join([workflow_file, job_name, matrix_blob, failure_identifier])
        return hashlib.sha256(payload.encode()).hexdigest()

    def has_open_pr(self, fingerprint: str) -> bool:
        """Return True if this fingerprint has an open or merged PR."""
        entry = self._entries.get(fingerprint)
        if entry is None:
            return False
        has_pr = entry.status in ("open", "merged")
        if has_pr:
            logger.info(
                "Deduplication check: fingerprint %s has status '%s' (pr=%s), skipping.",
                fingerprint[:12], entry.status, entry.pr_url or "N/A",
            )
        return has_pr

    def get_entry(self, fingerprint: str) -> FailureStoreEntry | None:
        """Return the store entry for a fingerprint, or None if not found."""
        return self._entries.get(fingerprint)

    def record(
        self,
        fingerprint: str,
        failure_identifier: str,
        error_signature: str,
        file_path: str,
        pr_url: str | None = None,
        status: str = "processing",
        test_name: str | None = None,
    ) -> None:
        """Record a failure in the store."""
        now = datetime.now(timezone.utc).isoformat()
        existing = self._entries.get(fingerprint)
        self._entries[fingerprint] = FailureStoreEntry(
            fingerprint=fingerprint,
            failure_identifier=failure_identifier,
            test_name=test_name,
            error_signature=error_signature,
            file_path=file_path,
            pr_url=pr_url if pr_url is not None else (existing.pr_url if existing else None),
            status=status,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            queued_pr_payload=existing.queued_pr_payload if existing else None,
        )
        logger.info(
            "Recorded failure: fingerprint=%s, identifier=%s, status=%s",
            fingerprint[:12], failure_identifier, status,
        )

    def record_queued_pr(
        self,
        fingerprint: str,
        failure_report: FailureReport,
        root_cause: RootCauseReport,
        patch: str,
        target_branch: str,
    ) -> None:
        """Persist a validated PR payload for later reconciliation."""
        if failure_report.parsed_failures:
            parsed_failure = failure_report.parsed_failures[0]
            failure_identifier = parsed_failure.failure_identifier
            error_signature = parsed_failure.error_message
            file_path = parsed_failure.file_path
            test_name = parsed_failure.test_name
        else:
            failure_identifier = failure_report.job_name
            error_signature = failure_report.raw_log_excerpt or ""
            file_path = ""
            test_name = None

        self.record(
            fingerprint,
            failure_identifier,
            error_signature,
            file_path,
            status="queued",
            test_name=test_name,
        )
        self._entries[fingerprint].queued_pr_payload = {
            "failure_report": failure_report_to_dict(failure_report),
            "root_cause": root_cause_report_to_dict(root_cause),
            "patch": patch,
            "target_branch": target_branch,
        }

    def clear_queued_pr(self, fingerprint: str) -> None:
        """Clear any queued PR payload associated with a fingerprint."""
        entry = self._entries.get(fingerprint)
        if entry is not None:
            entry.queued_pr_payload = None
            entry.updated_at = datetime.now(timezone.utc).isoformat()

    def record_failure_observation(
        self,
        report: FailureReport,
        *,
        fingerprint: str,
        max_entries: int,
    ) -> None:
        """Append fail observations for parsed or unparseable failures."""
        if report.parsed_failures:
            for parsed_failure in report.parsed_failures:
                key = self.compute_history_key(
                    report.workflow_file,
                    report.job_name,
                    report.matrix_params,
                    parsed_failure.failure_identifier,
                )
                entry = self._history.setdefault(
                    key,
                    FailureHistoryEntry(
                        key=key,
                        workflow_file=report.workflow_file,
                        job_name=report.job_name,
                        matrix_params=dict(report.matrix_params),
                        failure_identifier=parsed_failure.failure_identifier,
                        test_name=parsed_failure.test_name,
                    ),
                )
                self._append_observation(
                    entry,
                    FailureObservation(
                        outcome="fail",
                        observed_at=datetime.now(timezone.utc).isoformat(),
                        commit_sha=report.commit_sha,
                        workflow_run_id=report.workflow_run_id,
                        workflow_name=report.workflow_name,
                        workflow_file=report.workflow_file,
                        job_name=report.job_name,
                        matrix_params=dict(report.matrix_params),
                        failure_identifier=parsed_failure.failure_identifier,
                        test_name=parsed_failure.test_name,
                        error_signature=parsed_failure.error_message,
                        file_path=parsed_failure.file_path,
                        fingerprint=fingerprint,
                    ),
                    max_entries=max_entries,
                )
            return

        key = self.compute_history_key(
            report.workflow_file,
            report.job_name,
            report.matrix_params,
            report.job_name,
        )
        entry = self._history.setdefault(
            key,
            FailureHistoryEntry(
                key=key,
                workflow_file=report.workflow_file,
                job_name=report.job_name,
                matrix_params=dict(report.matrix_params),
                failure_identifier=report.job_name,
                test_name=None,
            ),
        )
        self._append_observation(
            entry,
            FailureObservation(
                outcome="fail",
                observed_at=datetime.now(timezone.utc).isoformat(),
                commit_sha=report.commit_sha,
                workflow_run_id=report.workflow_run_id,
                workflow_name=report.workflow_name,
                workflow_file=report.workflow_file,
                job_name=report.job_name,
                matrix_params=dict(report.matrix_params),
                failure_identifier=report.job_name,
                test_name=None,
                error_signature=report.raw_log_excerpt or "",
                file_path="",
                fingerprint=fingerprint,
            ),
            max_entries=max_entries,
        )

    def record_success_observation(
        self,
        *,
        workflow_name: str,
        workflow_file: str,
        job_name: str,
        matrix_params: dict[str, str],
        commit_sha: str,
        workflow_run_id: int | None,
        max_entries: int,
    ) -> None:
        """Append inferred pass observations for known failures in a successful job."""
        matched_entries = [
            entry
            for entry in self._history.values()
            if entry.workflow_file == workflow_file
            and entry.job_name == job_name
            and entry.matrix_params == matrix_params
        ]
        for entry in matched_entries:
            self._append_observation(
                entry,
                FailureObservation(
                    outcome="pass",
                    observed_at=datetime.now(timezone.utc).isoformat(),
                    commit_sha=commit_sha,
                    workflow_run_id=workflow_run_id,
                    workflow_name=workflow_name,
                    workflow_file=workflow_file,
                    job_name=job_name,
                    matrix_params=dict(matrix_params),
                    failure_identifier=entry.failure_identifier,
                    test_name=entry.test_name,
                ),
                max_entries=max_entries,
            )

    def summarize_history(
        self,
        workflow_file: str,
        job_name: str,
        matrix_params: dict[str, str],
        failure_identifier: str,
    ) -> FailureHistorySummary | None:
        """Return a derived history summary for a stable failure identity."""
        key = self.compute_history_key(
            workflow_file,
            job_name,
            matrix_params,
            failure_identifier,
        )
        entry = self._history.get(key)
        if entry is None or not entry.observations:
            return None

        observations = entry.observations
        failures = [obs for obs in observations if obs.outcome == "fail"]
        passes = [obs for obs in observations if obs.outcome == "pass"]
        streak = 0
        for observation in reversed(observations):
            if observation.outcome != "fail":
                break
            streak += 1

        latest_failure_sha = failures[-1].commit_sha if failures else None
        last_known_good_sha = None
        first_bad_sha = None
        last_pass_index = -1
        for index in range(len(observations) - 1, -1, -1):
            if observations[index].outcome == "pass":
                last_pass_index = index
                last_known_good_sha = observations[index].commit_sha
                break
        for observation in observations[last_pass_index + 1:]:
            if observation.outcome == "fail":
                first_bad_sha = observation.commit_sha
                break

        return FailureHistorySummary(
            key=entry.key,
            total_observations=len(observations),
            failure_count=len(failures),
            pass_count=len(passes),
            consecutive_failures=streak,
            last_outcome=observations[-1].outcome,
            latest_failure_sha=latest_failure_sha,
            last_known_good_sha=last_known_good_sha,
            first_bad_sha=first_bad_sha,
        )

    @staticmethod
    def _append_observation(
        entry: FailureHistoryEntry,
        observation: FailureObservation,
        *,
        max_entries: int,
    ) -> None:
        """Append an observation, avoiding duplicate run/outcome pairs."""
        if entry.observations:
            latest = entry.observations[-1]
            if (
                latest.workflow_run_id == observation.workflow_run_id
                and latest.outcome == observation.outcome
                and latest.commit_sha == observation.commit_sha
            ):
                return
        entry.observations.append(observation)
        if max_entries > 0 and len(entry.observations) > max_entries:
            entry.observations[:] = entry.observations[-max_entries:]

    def mark_abandoned(self, fingerprint: str) -> None:
        """Mark a failure entry as abandoned (PR closed without merge)."""
        entry = self._entries.get(fingerprint)
        if entry:
            entry.status = "abandoned"
            entry.updated_at = datetime.now(timezone.utc).isoformat()

    def reconcile_pr_states(self) -> None:
        """Reconcile store entries against actual PR states via GitHub API."""
        if not self._gh or not self._repo_name:
            logger.warning("Cannot reconcile: no GitHub client or repo configured.")
            return

        repo = self._gh.get_repo(self._repo_name)
        for fingerprint, entry in self._entries.items():
            if entry.status not in ("open", "processing"):
                continue
            if not entry.pr_url:
                continue
            try:
                # Extract PR number from URL
                pr_number = int(entry.pr_url.rstrip("/").split("/")[-1])
                pr = repo.get_pull(pr_number)
                if pr.merged:
                    entry.status = "merged"
                elif pr.state == "closed":
                    entry.status = "abandoned"
                entry.updated_at = datetime.now(timezone.utc).isoformat()
            except Exception as exc:
                logger.warning("Failed to reconcile PR for %s: %s", fingerprint, exc)

    def to_dict(self) -> dict:
        """Serialize the store to a JSON-compatible dict."""
        return {
            "entries": {
                fp: {
                    "fingerprint": e.fingerprint,
                    "failure_identifier": e.failure_identifier,
                    "test_name": e.test_name,
                    "error_signature": e.error_signature,
                    "file_path": e.file_path,
                    "pr_url": e.pr_url,
                    "status": e.status,
                    "created_at": e.created_at,
                    "updated_at": e.updated_at,
                    "queued_pr_payload": e.queued_pr_payload,
                }
                for fp, e in self._entries.items()
            },
            "history": {
                key: asdict(entry)
                for key, entry in self._history.items()
            },
        }

    def from_dict(self, data: dict) -> None:
        """Deserialize the store from a dict."""
        self._entries.clear()
        self._history.clear()

        entries_raw = data.get("entries") if isinstance(data.get("entries"), dict) else data
        if not isinstance(entries_raw, dict):
            return
        for fp, raw in entries_raw.items():
            self._entries[fp] = FailureStoreEntry(
                fingerprint=raw["fingerprint"],
                failure_identifier=raw["failure_identifier"],
                test_name=raw.get("test_name"),
                error_signature=raw["error_signature"],
                file_path=raw["file_path"],
                pr_url=raw.get("pr_url"),
                status=raw["status"],
                created_at=raw["created_at"],
                updated_at=raw["updated_at"],
                queued_pr_payload=raw.get("queued_pr_payload"),
            )
        history_raw = data.get("history", {}) if isinstance(data, dict) else {}
        if not isinstance(history_raw, dict):
            return
        for key, raw in history_raw.items():
            if not isinstance(raw, dict):
                continue
            observations_raw = raw.get("observations", [])
            observations: list[FailureObservation] = []
            if isinstance(observations_raw, list):
                for observation_raw in observations_raw:
                    if not isinstance(observation_raw, dict):
                        continue
                    observations.append(
                        FailureObservation(
                            outcome=str(observation_raw.get("outcome", "")),
                            observed_at=str(observation_raw.get("observed_at", "")),
                            commit_sha=str(observation_raw.get("commit_sha", "")),
                            workflow_run_id=observation_raw.get("workflow_run_id"),
                            workflow_name=str(observation_raw.get("workflow_name", "")),
                            workflow_file=str(observation_raw.get("workflow_file", "")),
                            job_name=str(observation_raw.get("job_name", "")),
                            matrix_params=dict(observation_raw.get("matrix_params", {})),
                            failure_identifier=str(observation_raw.get("failure_identifier", "")),
                            test_name=observation_raw.get("test_name"),
                            error_signature=str(observation_raw.get("error_signature", "")),
                            file_path=str(observation_raw.get("file_path", "")),
                            fingerprint=observation_raw.get("fingerprint"),
                        )
                    )
            self._history[key] = FailureHistoryEntry(
                key=str(raw.get("key", key)),
                workflow_file=str(raw.get("workflow_file", "")),
                job_name=str(raw.get("job_name", "")),
                matrix_params=dict(raw.get("matrix_params", {})),
                failure_identifier=str(raw.get("failure_identifier", "")),
                test_name=raw.get("test_name"),
                observations=observations,
            )

    def _ensure_store_branch(self, repo) -> None:
        """Create the data branch from the default branch when missing."""
        try:
            repo.get_git_ref(f"heads/{_STORE_BRANCH}")
            return
        except GithubException as exc:
            if exc.status != 404:
                raise
        except FileNotFoundError:
            pass

        base_ref = repo.get_git_ref(f"heads/{repo.default_branch}")
        repo.create_git_ref(
            ref=f"refs/heads/{_STORE_BRANCH}",
            sha=base_ref.object.sha,
        )

    def load(self) -> None:
        """Load the store from the dedicated branch via GitHub API."""
        if not self._state_gh or not self._state_repo_name:
            logger.info("No GitHub client; starting with empty store.")
            return
        try:
            repo = self._state_gh.get_repo(self._state_repo_name)
            contents = repo.get_contents(_STORE_FILE, ref=_STORE_BRANCH)
            if isinstance(contents, list):
                raise ValueError("Failure store path resolved to a directory.")
            data = json.loads(contents.decoded_content.decode())
            self.from_dict(data)
            logger.info("Loaded %d entries from failure store.", len(self._entries))
        except Exception as exc:
            logger.info("Could not load failure store (may not exist yet): %s", exc)
            self._entries.clear()
            self._history.clear()

    def save(self) -> None:
        """Save the store to the dedicated branch via GitHub API."""
        if not self._state_gh or not self._state_repo_name:
            logger.warning("Cannot save: no GitHub client or repo configured.")
            return
        try:
            repo = self._state_gh.get_repo(self._state_repo_name)
            self._ensure_store_branch(repo)
            content = json.dumps(self.to_dict(), indent=2)
            try:
                existing = repo.get_contents(_STORE_FILE, ref=_STORE_BRANCH)
            except GithubException as exc:
                if exc.status != 404:
                    raise
                existing = None
            except FileNotFoundError:
                existing = None

            if isinstance(existing, list):
                raise ValueError("Failure store path resolved to a directory.")
            if existing is None:
                repo.create_file(
                    _STORE_FILE, "Initialize failure store", content,
                    branch=_STORE_BRANCH,
                )
            else:
                repo.update_file(
                    _STORE_FILE, "Update failure store", content,
                    existing.sha, branch=_STORE_BRANCH,
                )
            logger.info("Saved %d entries to failure store.", len(self._entries))
        except Exception as exc:
            logger.error("Failed to save failure store: %s", exc)

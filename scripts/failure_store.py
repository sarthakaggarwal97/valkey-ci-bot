"""Failure deduplication and tracking store."""

from __future__ import annotations

import hashlib
import json
import logging
from base64 import b64decode
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from github.GithubException import GithubException

from scripts.models import (
    FlakyCampaignAttempt,
    FlakyCampaignState,
    FailureHistoryEntry,
    FailureHistorySummary,
    FailureObservation,
    FailureReport,
    FailureStoreEntry,
    RootCauseReport,
    flaky_campaign_state_from_dict,
    flaky_campaign_state_to_dict,
    failure_report_to_dict,
    root_cause_report_to_dict,
)

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)

_STORE_BRANCH = "bot-data"
_STORE_FILE = "failure-store.json"
_MAX_ERROR_SIGNATURE_CHARS = 10_000
_ACTIVE_QUEUE_STATUSES = {"queued", "queued-pr-retry"}


@dataclass
class PRStateTransition:
    """One observed transition for an agent-created PR."""

    fingerprint: str
    pr_url: str
    pr_number: int
    previous_status: str
    new_status: str
    github_state: str
    merged: bool
    rejection_feedback: str = ""


def _is_missing_store_error(exc: Exception) -> bool:
    """Return True when the remote failure store branch or file is absent."""
    if isinstance(exc, GithubException):
        return exc.status == 404
    return isinstance(exc, FileNotFoundError)


def _parse_pr_url(pr_url: str) -> tuple[str, int] | None:
    """Extract ``owner/repo`` and PR number from a GitHub pull request URL."""
    parsed = urllib_parse.urlparse(pr_url)
    if parsed.netloc not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] != "pull":
        return None
    try:
        return f"{parts[0]}/{parts[1]}", int(parts[3])
    except ValueError:
        return None


def _read_repo_file_text(contents: Any) -> str:
    """Read a GitHub contents response as UTF-8 text with API fallbacks."""
    try:
        return contents.decoded_content.decode()
    except AssertionError:
        encoding = getattr(contents, "encoding", None)
        content = getattr(contents, "content", None)
        if isinstance(content, str) and encoding == "base64":
            return b64decode(content.encode("utf-8")).decode()

        download_url = getattr(contents, "download_url", None)
        if isinstance(download_url, str) and download_url:
            request = urllib_request.Request(
                download_url,
                headers={"Accept": "application/vnd.github.raw+json"},
            )
            with urllib_request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        raise


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
        self._campaigns: dict[str, FlakyCampaignState] = {}

    @property
    def entries(self) -> dict[str, FailureStoreEntry]:
        return self._entries

    @property
    def history(self) -> dict[str, FailureHistoryEntry]:
        return self._history

    @property
    def campaigns(self) -> dict[str, FlakyCampaignState]:
        return self._campaigns

    @staticmethod
    def compute_fingerprint(
        failure_identifier: str, error_signature: str, file_path: str
    ) -> str:
        """SHA-256 of (failure_identifier, error_signature, file_path)."""
        payload = f"{failure_identifier}\0{error_signature}\0{file_path}"
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def compute_incident_key(
        failure_identifier: str,
        file_path: str,
        *,
        test_name: str | None = None,
    ) -> str:
        """Stable identity for one underlying failure across runners.

        Uses the parsed failure identity and source file path, but intentionally
        ignores runner-specific job or matrix details and raw error text.
        """
        stable_identifier = test_name or failure_identifier
        payload = f"{stable_identifier}\0{file_path}"
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _entry_incident_key(entry: FailureStoreEntry) -> str:
        """Return the canonical incident key for a persisted entry."""
        if entry.incident_key:
            return entry.incident_key
        return FailureStore.compute_incident_key(
            entry.failure_identifier,
            entry.file_path,
            test_name=entry.test_name,
        )

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
        entry = self.get_entry(fingerprint)
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
        entry = self._entries.get(fingerprint)
        if entry is not None:
            return entry
        for candidate in self._entries.values():
            if self._entry_incident_key(candidate) == fingerprint:
                return candidate
        return None

    def has_queued_pr_payload(self, fingerprint: str) -> bool:
        """Return whether an incident already has a persisted validated fix."""
        entry = self.get_entry(fingerprint)
        return bool(entry and isinstance(entry.queued_pr_payload, dict))

    def list_queued_failures(self) -> list[str]:
        """Return actively queued incidents in FIFO-ish order."""
        queued_entries = [
            entry
            for entry in self._entries.values()
            if entry.status in _ACTIVE_QUEUE_STATUSES
            and isinstance(entry.queued_pr_payload, dict)
        ]
        queued_entries.sort(
            key=lambda entry: (entry.updated_at, entry.created_at, entry.fingerprint)
        )
        return [entry.fingerprint for entry in queued_entries]

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
        existing = self.get_entry(fingerprint)
        store_key = existing.fingerprint if existing is not None else fingerprint
        # Truncate large error signatures to prevent store bloat.
        if len(error_signature) > _MAX_ERROR_SIGNATURE_CHARS:
            error_signature = error_signature[:_MAX_ERROR_SIGNATURE_CHARS] + "\n[truncated]"
        self._entries[store_key] = FailureStoreEntry(
            fingerprint=store_key,
            failure_identifier=failure_identifier,
            test_name=test_name,
            incident_key=fingerprint,
            error_signature=error_signature,
            file_path=file_path,
            pr_url=pr_url if pr_url is not None else (existing.pr_url if existing else None),
            status=status,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            queued_pr_payload=existing.queued_pr_payload if existing else None,
            campaign_status=existing.campaign_status if existing else None,
            incident_observations=(
                list(existing.incident_observations)
                if existing is not None
                else []
            ),
        )
        logger.info(
            "Recorded failure: fingerprint=%s, identifier=%s, status=%s",
            fingerprint[:12], failure_identifier, status,
        )

    def record_incident_observation(
        self,
        report: FailureReport,
        *,
        incident_key: str,
        max_entries: int,
    ) -> None:
        """Attach one runner-specific observation to a canonical incident entry."""
        entry = self.get_entry(incident_key)
        if entry is None:
            self.record(
                incident_key,
                report.job_name,
                report.raw_log_excerpt or "",
                "",
                status="processing",
            )
            entry = self.get_entry(incident_key)
        if entry is None:
            return

        if report.parsed_failures:
            parsed_failure = report.parsed_failures[0]
            observation = FailureObservation(
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
                fingerprint=incident_key,
                incident_key=incident_key,
            )
        else:
            observation = FailureObservation(
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
                error_signature=(report.raw_log_excerpt or "")[:_MAX_ERROR_SIGNATURE_CHARS],
                file_path="",
                fingerprint=incident_key,
                incident_key=incident_key,
            )
        self._append_entry_observation(
            entry,
            observation,
            max_entries=max_entries,
        )

    def get_flaky_campaign(self, fingerprint: str) -> FlakyCampaignState | None:
        """Return the stored flaky campaign state for a fingerprint."""
        return self._campaigns.get(fingerprint)

    def _get_or_create_campaign(
        self,
        fingerprint: str,
        report: FailureReport,
        failure_identifier: str,
    ) -> FlakyCampaignState:
        campaign = self._campaigns.get(fingerprint)
        if campaign is not None:
            return campaign
        now = datetime.now(timezone.utc).isoformat()
        history_key = self.compute_history_key(
            report.workflow_file,
            report.job_name,
            report.matrix_params,
            failure_identifier,
        )
        campaign = FlakyCampaignState(
            fingerprint=fingerprint,
            history_key=history_key,
            failure_identifier=failure_identifier,
            workflow_file=report.workflow_file,
            job_name=report.job_name,
            matrix_params=dict(report.matrix_params),
            repo_full_name=report.repo_full_name,
            branch=report.target_branch,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self._campaigns[fingerprint] = campaign
        return campaign

    def record_flaky_campaign_attempt(
        self,
        fingerprint: str,
        report: FailureReport,
        root_cause: RootCauseReport,
        patch: str,
        validation_output: str,
        *,
        passed: bool,
        passed_runs: int,
        attempted_runs: int,
        summary: str,
        strategy: str,
        max_failed_hypotheses: int,
    ) -> FlakyCampaignState:
        """Append one experiment attempt to the persistent flaky campaign."""
        failure_identifier = (
            report.parsed_failures[0].failure_identifier
            if report.parsed_failures
            else report.job_name
        )
        campaign = self._get_or_create_campaign(
            fingerprint,
            report,
            failure_identifier,
        )
        now = datetime.now(timezone.utc).isoformat()
        attempt_number = campaign.total_attempts + 1
        campaign.total_attempts = attempt_number
        campaign.updated_at = now
        campaign.root_cause = root_cause_report_to_dict(root_cause)
        campaign.current_patch = patch
        campaign.last_validation_output = validation_output
        campaign.last_strategy = strategy
        if not campaign.best_validation_output or passed:
            campaign.best_validation_output = validation_output
        attempt = FlakyCampaignAttempt(
            attempt_number=attempt_number,
            created_at=now,
            patch=patch,
            summary=summary,
            strategy=strategy,
            validation_output=validation_output,
            passed=passed,
            passed_runs=passed_runs,
            attempted_runs=attempted_runs,
        )
        campaign.attempts.append(attempt)
        if passed:
            campaign.consecutive_full_passes = passed_runs
            campaign.status = "validated"
        else:
            campaign.status = "active"
            campaign.consecutive_full_passes = 0
            if max_failed_hypotheses == 0:
                # 0 means unlimited — keep all failed hypotheses
                if summary not in campaign.failed_hypotheses:
                    campaign.failed_hypotheses.append(summary)
            else:
                if summary not in campaign.failed_hypotheses:
                    campaign.failed_hypotheses.append(summary)
            if max_failed_hypotheses > 0:
                campaign.failed_hypotheses = campaign.failed_hypotheses[-max_failed_hypotheses:]
        entry = self._entries.get(fingerprint)
        if entry is not None:
            entry.campaign_status = campaign.status
            entry.updated_at = now
        return campaign

    def mark_flaky_campaign_status(
        self,
        fingerprint: str,
        status: str,
        *,
        queued_pr_payload: dict | None = None,
    ) -> None:
        """Update the campaign lifecycle status for a fingerprint."""
        campaign = self._campaigns.get(fingerprint)
        if campaign is None:
            return
        campaign.status = status
        campaign.updated_at = datetime.now(timezone.utc).isoformat()
        if queued_pr_payload is not None:
            campaign.queued_pr_payload = queued_pr_payload
        entry = self.get_entry(fingerprint)
        if entry is not None:
            entry.campaign_status = status
            entry.updated_at = campaign.updated_at

    def update_proof_campaign(
        self,
        fingerprint: str,
        *,
        status: str,
        summary: str = "",
        proof_url: str = "",
        required_runs: int = 0,
        passed_runs: int = 0,
        attempted_runs: int = 0,
    ) -> None:
        """Persist GitHub-native proof status for an existing flaky campaign."""
        campaign = self._campaigns.get(fingerprint)
        if campaign is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        campaign.proof_status = status
        if summary:
            campaign.proof_summary = summary
        if proof_url:
            campaign.proof_url = proof_url
        if required_runs > 0:
            campaign.proof_required_runs = required_runs
        campaign.proof_passed_runs = max(0, passed_runs)
        campaign.proof_attempted_runs = max(0, attempted_runs)
        if status in {"pending", "running"} and not campaign.proof_started_at:
            campaign.proof_started_at = now
        if status and not campaign.proof_started_at:
            campaign.proof_started_at = now
        campaign.proof_updated_at = now
        campaign.updated_at = now
        entry = self.get_entry(fingerprint)
        if entry is not None:
            entry.updated_at = now

    def update_landing_campaign(
        self,
        fingerprint: str,
        *,
        status: str,
        summary: str = "",
        landing_url: str = "",
        landing_repo: str = "",
    ) -> None:
        """Persist upstream landing status for an existing flaky campaign."""
        campaign = self._campaigns.get(fingerprint)
        if campaign is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        campaign.landing_status = status
        if summary:
            campaign.landing_summary = summary
        if landing_url:
            campaign.landing_url = landing_url
        if landing_repo:
            campaign.landing_repo = landing_repo
        campaign.landing_updated_at = now
        campaign.updated_at = now
        if status == "passed":
            campaign.status = "landed"
        elif status == "failed":
            campaign.status = "landing-failed"
        entry = self.get_entry(fingerprint)
        if entry is not None:
            entry.campaign_status = campaign.status
            entry.updated_at = now

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
        entry = self.get_entry(fingerprint)
        if entry is None:
            return
        entry.queued_pr_payload = {
            "failure_report": failure_report_to_dict(failure_report),
            "root_cause": root_cause_report_to_dict(root_cause),
            "patch": patch,
            "target_branch": target_branch,
        }
        self.mark_flaky_campaign_status(
            fingerprint,
            "queued",
            queued_pr_payload=entry.queued_pr_payload,
        )

    def clear_queued_pr(self, fingerprint: str) -> None:
        """Clear any queued PR payload associated with a fingerprint."""
        entry = self.get_entry(fingerprint)
        if entry is not None:
            entry.queued_pr_payload = None
            entry.updated_at = datetime.now(timezone.utc).isoformat()
        campaign = self._campaigns.get(fingerprint)
        if campaign is not None:
            campaign.queued_pr_payload = None
            if campaign.status == "queued":
                campaign.status = "validated"
            campaign.updated_at = datetime.now(timezone.utc).isoformat()
            if entry is not None:
                entry.campaign_status = campaign.status
                entry.updated_at = campaign.updated_at

    def record_queued_pr_failure(self, fingerprint: str, error: str) -> int:
        """Record a failed reconciliation attempt while keeping the fix queued."""
        now = datetime.now(timezone.utc).isoformat()
        entry = self.get_entry(fingerprint)
        if entry is None:
            return 0
        payload = dict(entry.queued_pr_payload or {})
        reconciliation = payload.get("reconciliation", {})
        if not isinstance(reconciliation, dict):
            reconciliation = {}
        attempts = int(reconciliation.get("attempts", 0)) + 1
        reconciliation.update({
            "attempts": attempts,
            "last_error": str(error),
            "last_attempt_at": now,
        })
        payload["reconciliation"] = reconciliation
        entry.queued_pr_payload = payload
        entry.status = "queued-pr-retry"
        entry.updated_at = now
        campaign = self._campaigns.get(fingerprint)
        if campaign is not None:
            campaign.queued_pr_payload = payload
            campaign.status = "queued-pr-retry"
            campaign.updated_at = now
            entry.campaign_status = campaign.status
        return attempts

    def mark_queued_pr_dead_letter(self, fingerprint: str, error: str) -> None:
        """Move a queued PR payload out of the active retry queue."""
        now = datetime.now(timezone.utc).isoformat()
        entry = self.get_entry(fingerprint)
        if entry is not None:
            payload = dict(entry.queued_pr_payload or {})
            reconciliation = payload.get("reconciliation", {})
            if not isinstance(reconciliation, dict):
                reconciliation = {}
            reconciliation.update({
                "dead_lettered_at": now,
                "dead_letter_reason": str(error),
            })
            payload["reconciliation"] = reconciliation
            entry.queued_pr_payload = payload
            entry.status = "queued-pr-dead-letter"
            entry.updated_at = now
        campaign = self._campaigns.get(fingerprint)
        if campaign is not None:
            campaign.status = "queued-pr-dead-letter"
            campaign.updated_at = now
            if entry is not None:
                campaign.queued_pr_payload = entry.queued_pr_payload
                entry.campaign_status = campaign.status

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
                error_signature=(report.raw_log_excerpt or "")[:_MAX_ERROR_SIGNATURE_CHARS],
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

    @staticmethod
    def _append_entry_observation(
        entry: FailureStoreEntry,
        observation: FailureObservation,
        *,
        max_entries: int,
    ) -> None:
        """Append one incident-level observation to a store entry."""
        if entry.incident_observations:
            latest = entry.incident_observations[-1]
            if (
                latest.workflow_run_id == observation.workflow_run_id
                and latest.job_name == observation.job_name
                and latest.outcome == observation.outcome
                and latest.commit_sha == observation.commit_sha
            ):
                return
        entry.incident_observations.append(observation)
        if max_entries > 0 and len(entry.incident_observations) > max_entries:
            entry.incident_observations[:] = entry.incident_observations[-max_entries:]

    def mark_abandoned(self, fingerprint: str) -> None:
        """Mark a failure entry as abandoned (PR closed without merge)."""
        entry = self.get_entry(fingerprint)
        if entry:
            entry.status = "abandoned"
            entry.updated_at = datetime.now(timezone.utc).isoformat()
            if entry.campaign_status in {"active", "validated", "queued"}:
                entry.campaign_status = "abandoned"
        campaign = self._campaigns.get(fingerprint)
        if campaign is not None:
            campaign.status = "abandoned"
            campaign.updated_at = datetime.now(timezone.utc).isoformat()

    def reconcile_pr_states(self) -> list[PRStateTransition]:
        """Reconcile store entries against actual PR states via GitHub API."""
        if not self._gh or not self._repo_name:
            logger.warning("Cannot reconcile: no GitHub client or repo configured.")
            return []

        repo_cache: dict[str, Any] = {}
        transitions: list[PRStateTransition] = []
        for fingerprint, entry in self._entries.items():
            if entry.status not in ("open", "processing"):
                continue
            if not entry.pr_url:
                continue
            try:
                parsed_pr = _parse_pr_url(entry.pr_url)
                if parsed_pr is None:
                    logger.warning(
                        "Failed to parse PR URL for %s: %s",
                        fingerprint[:12],
                        entry.pr_url,
                    )
                    continue
                repo_name, pr_number = parsed_pr
                repo = repo_cache.get(repo_name)
                if repo is None:
                    repo = self._gh.get_repo(repo_name)
                    repo_cache[repo_name] = repo
                pr = repo.get_pull(pr_number)
                previous_status = entry.status
                campaign = self._campaigns.get(fingerprint)
                landing_complete = bool(
                    campaign is not None and campaign.landing_status == "passed"
                )
                rejection_feedback = ""
                if pr.merged:
                    entry.status = "merged"
                    if campaign is not None:
                        campaign.status = "merged" if not landing_complete else "landed"
                        campaign.updated_at = datetime.now(timezone.utc).isoformat()
                        entry.campaign_status = campaign.status
                elif pr.state == "closed":
                    entry.status = "abandoned"
                    # Scrape review comments as rejection feedback for future
                    # campaigns so the agent avoids repeating the same mistakes.
                    rejection_parts: list[str] = []
                    try:
                        for review in pr.get_reviews():
                            body = (review.body or "").strip()
                            if body and review.state in (
                                "CHANGES_REQUESTED",
                                "COMMENTED",
                            ):
                                rejection_parts.append(body[:500])
                        for comment in pr.get_issue_comments():
                            body = (comment.body or "").strip()
                            if body and not body.startswith("<!--"):
                                rejection_parts.append(body[:500])
                    except Exception as feedback_exc:
                        logger.debug(
                            "Could not scrape review feedback for %s: %s",
                            entry.pr_url, feedback_exc,
                        )
                    rejection_feedback = "\n---\n".join(rejection_parts[:5])
                    if rejection_feedback and campaign is not None:
                        hypothesis = (
                            f"PR {entry.pr_url} was closed by maintainer. "
                            f"Feedback: {rejection_feedback[:1000]}"
                        )
                        if hypothesis not in campaign.failed_hypotheses:
                            campaign.failed_hypotheses.append(hypothesis)
                    if campaign is not None:
                        campaign.status = "abandoned" if not landing_complete else "landed"
                        campaign.updated_at = datetime.now(timezone.utc).isoformat()
                        entry.campaign_status = campaign.status
                entry.updated_at = datetime.now(timezone.utc).isoformat()
                if entry.status != previous_status:
                    transitions.append(
                        PRStateTransition(
                            fingerprint=fingerprint,
                            pr_url=entry.pr_url,
                            pr_number=pr_number,
                            previous_status=previous_status,
                            new_status=entry.status,
                            github_state=str(pr.state),
                            merged=bool(pr.merged),
                            rejection_feedback=rejection_feedback,
                        )
                    )
            except Exception as exc:
                logger.warning("Failed to reconcile PR for %s: %s", fingerprint, exc)
        return transitions

    def to_dict(self) -> dict:
        """Serialize the store to a JSON-compatible dict."""
        return {
            "entries": {
                fp: {
                    "fingerprint": e.fingerprint,
                    "failure_identifier": e.failure_identifier,
                    "test_name": e.test_name,
                    "incident_key": e.incident_key,
                    "error_signature": e.error_signature,
                    "file_path": e.file_path,
                    "pr_url": e.pr_url,
                    "status": e.status,
                    "created_at": e.created_at,
                    "updated_at": e.updated_at,
                    "queued_pr_payload": e.queued_pr_payload,
                    "campaign_status": e.campaign_status,
                    "incident_observations": [asdict(obs) for obs in e.incident_observations],
                }
                for fp, e in self._entries.items()
            },
            "history": {
                key: asdict(entry)
                for key, entry in self._history.items()
            },
            "campaigns": {
                fp: flaky_campaign_state_to_dict(state)
                for fp, state in self._campaigns.items()
            },
        }

    def from_dict(self, data: dict) -> None:
        """Deserialize the store from a dict."""
        self._entries.clear()
        self._history.clear()
        self._campaigns.clear()

        entries_raw = data.get("entries") if isinstance(data.get("entries"), dict) else data
        if not isinstance(entries_raw, dict):
            return
        for fp, raw in entries_raw.items():
            self._entries[fp] = FailureStoreEntry(
                fingerprint=raw["fingerprint"],
                failure_identifier=raw["failure_identifier"],
                test_name=raw.get("test_name"),
                incident_key=str(raw.get("incident_key", fp)),
                error_signature=raw["error_signature"],
                file_path=raw["file_path"],
                pr_url=raw.get("pr_url"),
                status=raw["status"],
                created_at=raw["created_at"],
                updated_at=raw["updated_at"],
                queued_pr_payload=raw.get("queued_pr_payload"),
                campaign_status=raw.get("campaign_status"),
                incident_observations=[
                    FailureObservation(**obs)
                    for obs in raw.get("incident_observations", [])
                    if isinstance(obs, dict)
                ],
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
        campaigns_raw = data.get("campaigns", {}) if isinstance(data, dict) else {}
        if isinstance(campaigns_raw, dict):
            for fingerprint, raw in campaigns_raw.items():
                if not isinstance(raw, dict):
                    continue
                self._campaigns[str(fingerprint)] = flaky_campaign_state_from_dict(raw)

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
            data = json.loads(_read_repo_file_text(contents))
            self.from_dict(data)
            logger.info("Loaded %d entries from failure store.", len(self._entries))
        except Exception as exc:
            if not _is_missing_store_error(exc):
                raise RuntimeError(f"failed to load failure store: {exc}") from exc
            logger.info("Could not load failure store (may not exist yet): %s", exc)
            self._entries.clear()
            self._history.clear()
            self._campaigns.clear()

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

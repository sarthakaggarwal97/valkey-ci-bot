"""Failure deduplication and tracking store."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from github.GithubException import GithubException

from scripts.models import (
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

    @property
    def entries(self) -> dict[str, FailureStoreEntry]:
        return self._entries

    @staticmethod
    def compute_fingerprint(
        failure_identifier: str, error_signature: str, file_path: str
    ) -> str:
        """SHA-256 of (failure_identifier, error_signature, file_path)."""
        payload = f"{failure_identifier}\0{error_signature}\0{file_path}"
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
        }

    def from_dict(self, data: dict) -> None:
        """Deserialize the store from a dict."""
        self._entries.clear()
        for fp, raw in data.items():
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

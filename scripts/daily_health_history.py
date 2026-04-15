"""Persistent history for daily health run snapshots."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from github.GithubException import GithubException

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]

_HISTORY_BRANCH = "bot-data"
_HISTORY_ROOT = "dashboard-history/daily-health"
_MAX_PERSIST_ATTEMPTS = 3


def history_root() -> str:
    """Return the canonical history root within the data branch."""
    return _HISTORY_ROOT


def snapshot_relative_path(workflow_file: str, date: str) -> str:
    """Return the branch-relative path for one workflow/date snapshot."""
    workflow = str(workflow_file or "").strip()
    day = str(date or "").strip()
    if not workflow or not day:
        raise ValueError("workflow_file and date are required")
    return str(PurePosixPath(_HISTORY_ROOT) / workflow / f"{day}.json")


def merge_runs(
    preferred_runs: list[JsonObject],
    fallback_runs: list[JsonObject],
) -> list[JsonObject]:
    """Merge two run lists, preferring entries from the first list."""

    def key_for(run: JsonObject) -> tuple[str, str]:
        workflow = str(run.get("workflow", "")).strip()
        date = str(run.get("date", "")).strip()
        if workflow or date:
            return workflow, date
        return "", str(run.get("run_id", "")).strip()

    merged: dict[tuple[str, str, str], JsonObject] = {}
    for run in fallback_runs:
        if isinstance(run, dict):
            merged[key_for(run)] = dict(run)
    for run in preferred_runs:
        if isinstance(run, dict):
            merged[key_for(run)] = dict(run)
    return list(merged.values())


def load_history_runs(
    history_dir: str | Path | None,
    *,
    workflows: list[str] | tuple[str, ...] | None = None,
    expected_dates: list[str] | None = None,
) -> list[JsonObject]:
    """Load stored run snapshots from a local history checkout."""
    if not history_dir:
        return []
    root = Path(history_dir)
    if not root.exists():
        return []

    workflow_filter = {str(item).strip() for item in (workflows or []) if str(item).strip()}
    date_filter = {str(item).strip() for item in (expected_dates or []) if str(item).strip()}
    loaded: dict[tuple[str, str], JsonObject] = {}

    workflow_dirs: list[Path]
    if workflow_filter:
        workflow_dirs = [root / workflow for workflow in sorted(workflow_filter)]
    else:
        workflow_dirs = [path for path in sorted(root.iterdir()) if path.is_dir()]

    for workflow_dir in workflow_dirs:
        if not workflow_dir.exists():
            continue
        for snapshot_path in sorted(workflow_dir.glob("*.json")):
            if date_filter and snapshot_path.stem not in date_filter:
                continue
            try:
                payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("Skipping unreadable history snapshot %s", snapshot_path)
                continue
            run = _run_from_snapshot_payload(payload)
            if run is None:
                continue
            workflow = str(run.get("workflow", "")).strip()
            date = str(run.get("date", "")).strip()
            key = (workflow, date) if workflow or date else ("", str(run.get("run_id", "")).strip())
            loaded[key] = run
    return list(loaded.values())


def _run_from_snapshot_payload(payload: Any) -> JsonObject | None:
    if not isinstance(payload, dict):
        return None
    raw_run = payload.get("run", payload)
    if not isinstance(raw_run, dict):
        return None
    run = dict(raw_run)
    if not str(run.get("workflow", "")).strip():
        run["workflow"] = str(payload.get("workflow", "")).strip()
    if not str(run.get("date", "")).strip():
        run["date"] = str(payload.get("date", "")).strip()
    if not str(run.get("repo", "")).strip():
        run["repo"] = str(payload.get("repo", "")).strip()
    if not str(run.get("captured_at", "")).strip():
        run["captured_at"] = str(payload.get("captured_at", "")).strip()
    workflow = str(run.get("workflow", "")).strip()
    date = str(run.get("date", "")).strip()
    if not workflow or not date:
        return None
    return run


def _snapshot_payload(run: JsonObject, *, repo_full_name: str = "") -> JsonObject:
    workflow = str(run.get("workflow", "")).strip()
    date = str(run.get("date", "")).strip()
    return {
        "schema_version": 1,
        "repo": repo_full_name or str(run.get("repo", "")).strip(),
        "workflow": workflow,
        "date": date,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "run": dict(run),
    }


def _github_status(exc: Exception) -> int | None:
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


def _is_missing_error(exc: Exception) -> bool:
    if isinstance(exc, GithubException):
        return exc.status == 404
    return isinstance(exc, FileNotFoundError)


def _is_write_conflict(exc: Exception) -> bool:
    status = _github_status(exc)
    if status in {409, 422}:
        return True
    message = str(exc).lower()
    return "sha" in message or "already exists" in message or "conflict" in message


class DailyHealthHistoryStore:
    """Persists normalized daily-health run snapshots on the data branch."""

    def __init__(
        self,
        github_client: "Github | None" = None,
        repo_full_name: str = "",
        *,
        mirror_dir: str | Path | None = None,
    ) -> None:
        self._gh = github_client
        self._repo_name = repo_full_name
        self._mirror_dir = Path(mirror_dir) if mirror_dir else None

    def save_runs(
        self,
        runs: list[JsonObject],
        *,
        repo_full_name: str = "",
    ) -> dict[str, int]:
        """Persist a batch of normalized run snapshots."""
        summary = {
            "saved": 0,
            "skipped": 0,
            "mirrored": 0,
        }
        if not runs:
            return summary

        repo = None
        if self._gh and self._repo_name:
            repo = self._gh.get_repo(self._repo_name)
            self._ensure_history_branch(repo)
        elif self._gh or self._repo_name:
            logger.warning("Daily health history store is missing a GitHub client or repo name.")

        for run in runs:
            if not isinstance(run, dict):
                continue
            workflow = str(run.get("workflow", "")).strip()
            date = str(run.get("date", "")).strip()
            if not workflow or not date:
                continue
            payload = _snapshot_payload(run, repo_full_name=repo_full_name)
            content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
            if repo is not None:
                outcome = self._save_remote_snapshot(repo, workflow, date, content)
                summary[outcome] += 1
            if self._mirror_dir is not None:
                self._write_mirror_snapshot(workflow, date, content)
                summary["mirrored"] += 1
        return summary

    def _ensure_history_branch(self, repo: Any) -> None:
        try:
            repo.get_git_ref(f"heads/{_HISTORY_BRANCH}")
            return
        except GithubException as exc:
            if exc.status != 404:
                raise
        except FileNotFoundError:
            pass

        base_ref = repo.get_git_ref(f"heads/{repo.default_branch}")
        repo.create_git_ref(
            ref=f"refs/heads/{_HISTORY_BRANCH}",
            sha=base_ref.object.sha,
        )

    def _save_remote_snapshot(
        self,
        repo: Any,
        workflow: str,
        date: str,
        content: str,
    ) -> str:
        path = snapshot_relative_path(workflow, date)
        for attempt in range(1, _MAX_PERSIST_ATTEMPTS + 1):
            try:
                existing = repo.get_contents(path, ref=_HISTORY_BRANCH)
            except Exception as exc:
                if not _is_missing_error(exc):
                    raise
                existing = None

            if isinstance(existing, list):
                raise ValueError(f"history path resolved to a directory: {path}")

            if existing is not None:
                existing_text = existing.decoded_content.decode("utf-8")
                if existing_text == content:
                    return "skipped"

            try:
                if existing is None:
                    repo.create_file(
                        path,
                        f"Add daily health snapshot for {workflow} {date}",
                        content,
                        branch=_HISTORY_BRANCH,
                    )
                else:
                    repo.update_file(
                        path,
                        f"Update daily health snapshot for {workflow} {date}",
                        content,
                        existing.sha,
                        branch=_HISTORY_BRANCH,
                    )
                return "saved"
            except Exception as exc:
                if attempt < _MAX_PERSIST_ATTEMPTS and _is_write_conflict(exc):
                    logger.info(
                        "Daily health snapshot write conflict for %s on attempt %d/%d; retrying.",
                        path,
                        attempt,
                        _MAX_PERSIST_ATTEMPTS,
                    )
                    continue
                raise
        return "skipped"

    def _write_mirror_snapshot(self, workflow: str, date: str, content: str) -> None:
        if self._mirror_dir is None:
            return
        target = self._mirror_dir / workflow / f"{date}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

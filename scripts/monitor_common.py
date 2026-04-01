"""Shared helpers for workflow monitor CLIs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from scripts.config import BotConfig, load_config


def configure_monitor_logging(verbose: bool) -> None:
    """Configure process logging for workflow monitor entrypoints."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def build_monitor_key(target_repo: str, workflow_file: str, event: str) -> str:
    """Build a stable key for persisted monitor state."""
    return f"{target_repo}:{workflow_file}:{event}"


def fetch_recent_completed_runs(
    *,
    target_gh: Any,
    target_repo: str,
    workflow_file: str,
    event: str,
    max_runs: int,
    last_seen_run_id: int,
) -> list[Any]:
    """Fetch recent completed workflow runs newer than the stored watermark."""
    repo = target_gh.get_repo(target_repo)
    workflow = repo.get_workflow(workflow_file)
    runs = workflow.get_runs(event=event, status="completed")

    fresh_runs: list[Any] = []
    for index, run in enumerate(runs):
        if index >= max_runs:
            break
        if run.id <= last_seen_run_id:
            break
        fresh_runs.append(run)

    fresh_runs.sort(key=lambda run: run.id)
    return fresh_runs


def load_local_bot_config(config_path: str) -> BotConfig:
    """Load a checked-in bot config when present, else fall back to defaults."""
    path = Path(config_path)
    if not path.exists():
        return BotConfig()
    return load_config(path)

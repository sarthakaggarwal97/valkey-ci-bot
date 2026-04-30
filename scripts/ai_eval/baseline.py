"""Capture baseline fixtures from historical pipeline runs.

Walks the last N days of CI failures / PR reviews from the bot-data history
and captures inputs + outputs as static baseline fixtures for replay eval.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _parse_since(since: str) -> timedelta:
    """Parse a duration string like '90d', '7d', '24h' into a timedelta."""
    since = since.strip().lower()
    if since.endswith("d"):
        return timedelta(days=int(since[:-1]))
    if since.endswith("h"):
        return timedelta(hours=int(since[:-1]))
    if since.endswith("w"):
        return timedelta(weeks=int(since[:-1]))
    raise ValueError(f"Invalid duration: {since} (use Nd, Nh, Nw)")


def _entry_is_recent(entry: dict, cutoff: datetime) -> bool:
    """Check if a failure store entry is newer than the cutoff."""
    updated_at = entry.get("updated_at", "")
    if not updated_at:
        return False
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        return dt > cutoff
    except (ValueError, TypeError):
        return False


def capture_from_failure_store(
    store_path: Path,
    cutoff: datetime,
    output_dir: Path,
) -> int:
    """Capture baseline fixtures from a local failure store JSON.

    Returns the number of fixtures written.
    """
    if not store_path.exists():
        logger.warning("Failure store not found: %s", store_path)
        return 0

    try:
        store_data = json.loads(store_path.read_text())
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse store %s: %s", store_path, exc)
        return 0

    entries = store_data.get("entries", {})
    if not isinstance(entries, dict):
        logger.warning("Store has no 'entries' dict")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for fingerprint, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        if not _entry_is_recent(entry, cutoff):
            continue

        fixture: dict[str, Any] = {
            "fixture_id": fingerprint,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source": "failure_store",
            "entry": entry,
            # The baseline fixture captures the entry as-is; downstream
            # eval can diff new pipeline output against this snapshot.
        }
        output_path = output_dir / f"{fingerprint}.json"
        output_path.write_text(json.dumps(fixture, indent=2))
        count += 1

    logger.info("Captured %d baseline fixtures to %s", count, output_dir)
    return count


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Capture baseline fixtures from history")
    parser.add_argument(
        "--since", default="90d",
        help="Capture entries newer than this (e.g. 90d, 30d, 7d)",
    )
    parser.add_argument(
        "--store", default="failure-store.json",
        help="Path to failure store JSON file",
    )
    parser.add_argument(
        "--out", default="scripts/ai_eval/fixtures/baseline",
        help="Output directory for baseline fixtures",
    )
    args = parser.parse_args(argv)

    try:
        delta = _parse_since(args.since)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    cutoff = datetime.now(timezone.utc) - delta
    output_dir = Path(args.out)
    store_path = Path(args.store)

    count = capture_from_failure_store(store_path, cutoff, output_dir)
    print(f"Captured {count} baseline fixtures from {args.store} to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Backfill durable daily-health history snapshots from live GitHub runs."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from github import Auth, Github

from scripts.daily_health_history import DailyHealthHistoryStore, load_history_runs
from scripts.daily_health_report import fetch_daily_runs

logger = logging.getLogger(__name__)


def _expected_dates(days: int) -> list[str]:
    if days <= 0:
        return []
    end_day = datetime.now(timezone.utc).date()
    start_day = end_day - timedelta(days=days - 1)
    return [
        (start_day + timedelta(days=offset)).isoformat()
        for offset in range(days)
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default="valkey-io/valkey",
        help="Repository full name to inspect (default: valkey-io/valkey)",
    )
    parser.add_argument(
        "--workflow",
        default=["daily.yml"],
        nargs="+",
        help="Workflow file name(s) to backfill.",
    )
    parser.add_argument(
        "--branch",
        default="unstable",
        help="Branch to inspect (default: unstable)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Number of recent days to backfill (default: 14)",
    )
    parser.add_argument(
        "--token",
        default="",
        help="GitHub token for reading workflow runs (or set GITHUB_TOKEN).",
    )
    parser.add_argument(
        "--state-token",
        default="",
        help="GitHub token for writing history snapshots (or set GITHUB_TOKEN).",
    )
    parser.add_argument(
        "--state-repo",
        required=True,
        help="Repository that owns the bot-data branch snapshots.",
    )
    parser.add_argument(
        "--mirror-dir",
        default="",
        help="Optional local directory to mirror persisted snapshots into.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    read_token = args.token or os.environ.get("GITHUB_TOKEN", "")
    write_token = args.state_token or os.environ.get("GITHUB_TOKEN", "")
    if not read_token:
        logger.error("GitHub read token required via --token or GITHUB_TOKEN.")
        return 1
    if not write_token:
        logger.error("GitHub state token required via --state-token or GITHUB_TOKEN.")
        return 1

    read_gh = Github(auth=Auth.Token(read_token))
    write_gh = Github(auth=Auth.Token(write_token))
    store = DailyHealthHistoryStore(
        write_gh,
        args.state_repo,
        mirror_dir=args.mirror_dir or None,
    )

    summary: dict[str, Any] = {
        "repo": args.repo,
        "branch": args.branch,
        "days": args.days,
        "state_repo": args.state_repo,
        "mirror_dir": args.mirror_dir,
        "workflows": [],
        "saved": 0,
        "skipped": 0,
        "mirrored": 0,
    }

    expected = set(_expected_dates(args.days))

    for workflow in args.workflow:
        logger.info(
            "Backfilling %s for %s on branch %s (%d days).",
            workflow,
            args.repo,
            args.branch,
            args.days,
        )
        runs = fetch_daily_runs(read_gh, args.repo, workflow, args.branch, args.days)
        batch = store.save_runs(runs, repo_full_name=args.repo)

        # Detect dates still missing after fetch + history.
        covered_dates = {
            str(run.get("date", "")).strip()
            for run in runs
            if str(run.get("date", "")).strip()
        }
        if args.mirror_dir:
            for hist_run in load_history_runs(
                args.mirror_dir,
                workflows=[workflow],
                expected_dates=sorted(expected),
            ):
                date = str(hist_run.get("date", "")).strip()
                if date:
                    covered_dates.add(date)
        missing_dates = sorted(expected - covered_dates)
        if missing_dates:
            logger.warning(
                "%s has %d missing date(s) in the %d-day window: %s",
                workflow,
                len(missing_dates),
                args.days,
                ", ".join(missing_dates),
            )

        summary["workflows"].append(
            {
                "workflow": workflow,
                "runs_found": len(runs),
                "missing_dates": missing_dates,
                **batch,
            }
        )
        for key in ("saved", "skipped", "mirrored"):
            summary[key] += int(batch.get(key, 0))

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

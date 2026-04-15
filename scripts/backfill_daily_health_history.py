"""Backfill durable daily-health history snapshots from live GitHub runs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

from github import Auth, Github

from scripts.daily_health_history import DailyHealthHistoryStore
from scripts.daily_health_report import fetch_daily_runs

logger = logging.getLogger(__name__)


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
        summary["workflows"].append(
            {
                "workflow": workflow,
                "runs_found": len(runs),
                **batch,
            }
        )
        for key in ("saved", "skipped", "mirrored"):
            summary[key] += int(batch.get(key, 0))

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

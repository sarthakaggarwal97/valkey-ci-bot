"""Observability reports for the evidence-first AI pipeline.

Subcommands:
  --needs-human    Show entries awaiting human follow-up
  --stage-latencies  Show p50/p90/p99 latency per stage
  --token-cost     Show token usage per stage
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def report_needs_human(store_data: dict, as_json: bool = False) -> str:
    """Report all needs-human entries grouped by rejection reason."""
    entries = store_data.get("entries", {})
    by_reason: dict[str, list[dict]] = defaultdict(list)

    for fp, entry in entries.items():
        if isinstance(entry, dict) and entry.get("status") == "needs-human":
            reason = entry.get("rejection_reason", "unknown")
            by_reason[reason].append({
                "fingerprint": fp,
                "failure_identifier": entry.get("failure_identifier", ""),
                "updated_at": entry.get("updated_at", ""),
            })

    if as_json:
        return json.dumps(dict(by_reason), indent=2)

    lines: list[str] = []
    total = sum(len(v) for v in by_reason.values())
    lines.append(f"Needs-human queue: {total} entries\n")
    for reason, items in sorted(by_reason.items()):
        lines.append(f"  {reason} ({len(items)}):")
        for item in items[:10]:
            lines.append(f"    - {item['failure_identifier']} (updated {item['updated_at']})")
        if len(items) > 10:
            lines.append(f"    ... and {len(items) - 10} more")
    return "\n".join(lines)


def report_stage_latencies(log_lines: list[str]) -> str:
    """Parse structured stage logs and report latency percentiles."""
    by_stage: dict[str, list[int]] = defaultdict(list)

    for line in log_lines:
        try:
            data = json.loads(line)
            stage = data.get("stage", "")
            duration = data.get("duration_ms", 0)
            if stage and duration:
                by_stage[stage].append(duration)
        except (json.JSONDecodeError, TypeError):
            continue

    lines: list[str] = ["Stage latencies (ms):\n"]
    for stage, durations in sorted(by_stage.items()):
        durations.sort()
        n = len(durations)
        p50 = durations[n // 2] if n else 0
        p90 = durations[int(n * 0.9)] if n else 0
        p99 = durations[int(n * 0.99)] if n else 0
        lines.append(f"  {stage}: p50={p50} p90={p90} p99={p99} (n={n})")
    return "\n".join(lines)


def report_token_cost(log_lines: list[str]) -> str:
    """Sum token usage per stage from structured logs."""
    by_stage: dict[str, dict[str, int]] = defaultdict(lambda: {"in": 0, "out": 0, "calls": 0})

    for line in log_lines:
        try:
            data = json.loads(line)
            stage = data.get("stage", "")
            if stage:
                by_stage[stage]["in"] += data.get("tokens_in", 0)
                by_stage[stage]["out"] += data.get("tokens_out", 0)
                by_stage[stage]["calls"] += 1
        except (json.JSONDecodeError, TypeError):
            continue

    lines: list[str] = ["Token usage by stage:\n"]
    total_in = total_out = 0
    for stage, counts in sorted(by_stage.items()):
        lines.append(f"  {stage}: in={counts['in']:,} out={counts['out']:,} calls={counts['calls']}")
        total_in += counts["in"]
        total_out += counts["out"]
    lines.append(f"\n  Total: in={total_in:,} out={total_out:,}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI pipeline observability reports")
    parser.add_argument("--needs-human", action="store_true")
    parser.add_argument("--stage-latencies", action="store_true")
    parser.add_argument("--token-cost", action="store_true")
    parser.add_argument("--store", default="failure-store.json", help="Path to failure store JSON")
    parser.add_argument("--logs", default=None, help="Path to structured log file")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args(argv)

    if args.needs_human:
        store_path = Path(args.store)
        if store_path.exists():
            store_data = json.loads(store_path.read_text())
        else:
            store_data = {"entries": {}}
        print(report_needs_human(store_data, args.json))
        return 0

    log_lines: list[str] = []
    if args.logs:
        log_path = Path(args.logs)
        if log_path.exists():
            log_lines = log_path.read_text().splitlines()

    if args.stage_latencies:
        print(report_stage_latencies(log_lines))
        return 0

    if args.token_cost:
        print(report_token_cost(log_lines))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

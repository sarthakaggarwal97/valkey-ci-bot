"""Capture gold fixtures for the AI eval harness.

Usage:
    python -m scripts.ai_eval.capture_gold --fixture-id my-test \\
        --evidence /tmp/evidence.json \\
        --expected-keywords "race condition,dictResize"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture a gold fixture for AI eval")
    parser.add_argument("--fixture-id", required=True, help="Unique fixture identifier")
    parser.add_argument("--evidence", required=True, help="Path to EvidencePack JSON")
    parser.add_argument("--output-dir", default="scripts/ai_eval/fixtures/gold/ci_failures")
    parser.add_argument("--expected-keywords", default="", help="Comma-separated expected root cause keywords")
    parser.add_argument("--expected-rejection-reason", default="", help="Expected rejection reason (empty = no rejection)")
    parser.add_argument("--expected-rejection-stage", default="", help="Stage where rejection is expected")
    args = parser.parse_args(argv)

    evidence_path = Path(args.evidence)
    if not evidence_path.exists():
        print(f"Evidence file not found: {evidence_path}", file=sys.stderr)
        return 1

    evidence = json.loads(evidence_path.read_text())

    fixture: dict = {
        "fixture_id": args.fixture_id,
        "evidence_pack": evidence,
    }

    if args.expected_keywords:
        fixture["expected_root_cause_keywords"] = [
            kw.strip() for kw in args.expected_keywords.split(",") if kw.strip()
        ]

    if args.expected_rejection_reason:
        fixture["expected_rejection"] = {
            "reason": args.expected_rejection_reason,
            "at_stage": args.expected_rejection_stage or "root_cause",
        }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{args.fixture_id}.json"
    output_path.write_text(json.dumps(fixture, indent=2))
    print(f"Fixture saved to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

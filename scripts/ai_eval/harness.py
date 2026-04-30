"""AI evaluation harness — runs stages against fixtures and scores results."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from scripts.ai_eval.scoring import ScoringResult, score_fix_patch, score_rejection, score_root_cause

logger = logging.getLogger(__name__)


def load_fixtures(fixtures_dir: str | Path) -> list[dict]:
    """Load all JSON fixture files from a directory tree."""
    fixtures_path = Path(fixtures_dir)
    fixtures: list[dict] = []
    for f in sorted(fixtures_path.rglob("*.json")):
        try:
            data = json.loads(f.read_text())
            data["_fixture_path"] = str(f)
            data["_fixture_id"] = f.stem
            fixtures.append(data)
        except Exception as exc:
            logger.warning("Failed to load fixture %s: %s", f, exc)
    return fixtures


def score_fixture(fixture: dict, stage_outputs: dict | None = None) -> list[ScoringResult]:
    """Score one fixture against its expected annotations."""
    fixture_id = fixture.get("_fixture_id", "unknown")
    results: list[ScoringResult] = []

    # Root cause scoring
    expected_keywords = fixture.get("expected_root_cause_keywords", [])
    if expected_keywords and stage_outputs:
        accepted = stage_outputs.get("root_cause", {}).get("accepted", {})
        chain = accepted.get("causal_chain", []) if accepted else []
        results.append(score_root_cause(chain, expected_keywords, fixture_id))

    # Fix patch scoring
    expected_fix = fixture.get("expected_fix_properties")
    if expected_fix and stage_outputs:
        patch = stage_outputs.get("tournament", {}).get("winning", {}).get("candidate", {}).get("patch", "")
        results.append(score_fix_patch(patch, expected_fix, fixture_id))

    # Rejection scoring
    expected_rejection = fixture.get("expected_rejection")
    actual_reason = None
    if stage_outputs:
        rr = stage_outputs.get("rejection_reason")
        actual_reason = rr
    results.append(score_rejection(actual_reason, expected_rejection, fixture_id))

    return results


def run_deterministic(fixtures_dir: str | Path, outputs_dir: str | Path | None = None) -> dict:
    """Run deterministic scoring on all fixtures. Returns summary report."""
    fixtures = load_fixtures(fixtures_dir)
    if not fixtures:
        return {"error": "No fixtures found", "total": 0}

    all_results: list[ScoringResult] = []
    for fixture in fixtures:
        # Load stored stage outputs if available
        stage_outputs = fixture.get("stage_outputs")
        if not stage_outputs and outputs_dir:
            output_path = Path(outputs_dir) / f"{fixture.get('_fixture_id', '')}.json"
            if output_path.exists():
                stage_outputs = json.loads(output_path.read_text())

        results = score_fixture(fixture, stage_outputs)
        all_results.extend(results)

    # Aggregate
    by_scorer: dict[str, list[ScoringResult]] = {}
    for r in all_results:
        by_scorer.setdefault(r.scorer, []).append(r)

    report: dict[str, Any] = {"total_fixtures": len(fixtures), "scorers": {}}
    for scorer, results in by_scorer.items():
        passed = sum(1 for r in results if r.passed)
        avg_score = sum(r.score for r in results) / len(results) if results else 0
        report["scorers"][scorer] = {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "avg_score": round(avg_score, 3),
        }

    report["overall_passed"] = all(
        s["failed"] == 0 for s in report["scorers"].values()
    )
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the eval harness."""
    import argparse

    parser = argparse.ArgumentParser(description="AI eval harness")
    parser.add_argument("--mode", choices=["deterministic", "live"], default="deterministic")
    parser.add_argument("--fixtures", default="scripts/ai_eval/fixtures/gold")
    parser.add_argument("--outputs", default=None)
    args = parser.parse_args(argv)

    if args.mode == "deterministic":
        report = run_deterministic(args.fixtures, args.outputs)
        print(json.dumps(report, indent=2))
        return 0 if report.get("overall_passed", False) else 1
    else:
        print("Live mode requires Bedrock credentials — not yet implemented")
        return 1


if __name__ == "__main__":
    sys.exit(main())

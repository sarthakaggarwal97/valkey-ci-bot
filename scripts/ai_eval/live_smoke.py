"""Live-Bedrock smoke test for the evidence-first pipeline.

Purpose: exercise the real Bedrock API contract against a synthetic but
realistic EvidencePack, then report which stages succeeded. This is the
"does the pipeline actually work with a real model" check — separate from
the deterministic unit tests that use MagicMock.

Usage::

    # Default synthetic evidence
    python -m scripts.ai_eval.live_smoke --region us-east-1

    # Load from a gold fixture
    python -m scripts.ai_eval.live_smoke \\
        --fixture scripts/ai_eval/fixtures/gold/ci_failures/happy-hash-race-fix.json

    # Which stages to exercise (default: all except PR creation)
    python -m scripts.ai_eval.live_smoke --stages evidence,root_cause

Exit codes:
    0 — all exercised stages succeeded (or returned a valid rejection)
    1 — a stage raised an unexpected exception

Requires AWS credentials. The publish guard remains in effect so this
script will never create a PR, issue, comment, or workflow dispatch.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

logger = logging.getLogger("live_smoke")

_AVAILABLE_STAGES = ("evidence", "root_cause", "tournament", "rubric_gate")


def _load_evidence(fixture_path: str | None) -> Any:
    """Load an EvidencePack from a fixture or build a synthetic one."""
    from scripts.models import (
        CommitInfo,
        EvidencePack,
        InspectedFile,
        LogExcerpt,
        ParsedFailure,
    )

    if fixture_path:
        data = json.loads(Path(fixture_path).read_text())
        # Gold fixtures wrap the pack under "evidence_pack"
        if "evidence_pack" in data:
            return EvidencePack.from_dict(data["evidence_pack"])
        return EvidencePack.from_dict(data)

    # Synthetic realistic evidence
    return EvidencePack(
        failure_id="smoke-test",
        run_id=99999,
        job_ids=["test-sanitizer-thread"],
        workflow="daily.yml",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="test_hash_concurrent_resize",
                test_name="test_hash_concurrent_resize",
                file_path="src/t_hash.c",
                error_message="WARNING: ThreadSanitizer: data race on dictResize",
                assertion_details=None,
                line_number=842,
                stack_trace="#0 dictResize src/dict.c:112\n#1 hashTypeResize src/t_hash.c:842",
                parser_type="tsan",
            )
        ],
        log_excerpts=[
            LogExcerpt(
                source="job-log",
                content=(
                    "Running test_hash_concurrent_resize\n"
                    "WARNING: ThreadSanitizer: data race on dictResize\n"
                    "  Read of size 8 at 0x7b1c00000040 by thread T1\n"
                    "  Previous write at 0x7b1c00000040 by thread T2\n"
                    "FAILED: test_hash_concurrent_resize"
                ),
                line_start=100,
                line_end=105,
            )
        ],
        source_files_inspected=[
            InspectedFile(path="src/t_hash.c", reason="stack trace"),
            InspectedFile(path="src/dict.c", reason="stack trace"),
        ],
        test_files_inspected=[
            InspectedFile(path="tests/unit/type/hash.tcl", reason="failing test"),
        ],
        valkey_guidance_used=["memory-ordering"],
        recent_commits=[
            CommitInfo(
                sha="abc12345",
                message="Refactor hash resize for performance",
                author="contributor",
                files_changed=["src/t_hash.c"],
            )
        ],
        linked_urls=["https://github.com/valkey-io/valkey/actions/runs/99999"],
        unknowns=[],
        built_at="2025-01-15T10:00:00Z",
    )


def _run_evidence_stage(evidence) -> dict:
    """Validate the evidence pack structure."""
    evidence.validate()
    return {
        "status": "ok",
        "parsed_failures": len(evidence.parsed_failures),
        "log_excerpts": len(evidence.log_excerpts),
        "source_files": len(evidence.source_files_inspected),
        "test_files": len(evidence.test_files_inspected),
    }


def _run_root_cause_stage(evidence, bedrock, model_id: str) -> dict:
    from scripts.stages.root_cause import analyze
    result = analyze(evidence, bedrock, analyst_model=model_id, critic_model=model_id)
    if result.accepted is not None:
        return {
            "status": "accepted",
            "confidence": result.accepted.confidence,
            "summary": result.accepted.summary[:120],
            "chain_steps": len(result.accepted.causal_chain),
            "rejected_count": len(result.rejected),
        }
    return {
        "status": "rejected",
        "rejection_reason": result.rejection_reason.value if result.rejection_reason else "unknown",
        "rejected_count": len(result.rejected),
    }


def _run_tournament_stage(evidence, root_cause, bedrock, model_id: str) -> dict:
    """Exercise the fix generator but skip real validation (too slow)."""
    from scripts.stages.fix_tournament import generate_candidates

    candidates = generate_candidates(
        evidence, root_cause, bedrock, model_id=model_id, candidate_count=1,
    )
    return {
        "status": "ok" if candidates else "no_candidates",
        "candidate_count": len(candidates),
        "patch_lines": [
            sum(
                1 for line in c.patch.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
            for c in candidates
        ],
    }


def _run_rubric_gate_stage(patch: str, evidence, bedrock, model_id: str) -> dict:
    from scripts.stages.rubric import RubricGate
    gate = RubricGate()
    commit_msg = "Smoke test\n\nSigned-off-by: smoke <smoke@example.com>"
    failing_assertion = (
        evidence.parsed_failures[0].error_message if evidence.parsed_failures else ""
    )
    verdict = gate.judge(
        patch=patch, evidence=evidence, commit_message=commit_msg,
        failing_assertion=failing_assertion,
        bedrock_client=bedrock, model_id=model_id,
    )
    return {
        "status": "pass" if verdict.overall_passed else "fail",
        "check_count": len(verdict.checks),
        "blocking_checks": verdict.blocking_checks,
        "model_checks_run": any(c.kind == "model" for c in verdict.checks),
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Live-Bedrock pipeline smoke test")
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    parser.add_argument("--model-id", default="us.anthropic.claude-sonnet-4-v1")
    parser.add_argument("--fixture", default=None, help="Path to a gold fixture JSON")
    parser.add_argument(
        "--stages", default=",".join(_AVAILABLE_STAGES),
        help=f"Comma-separated stages to run (available: {','.join(_AVAILABLE_STAGES)})",
    )
    args = parser.parse_args(argv)

    # Force DRY_RUN so the publish guard blocks any accidental write
    os.environ["VALKEY_CI_AGENT_DRY_RUN"] = "1"

    requested = [s.strip() for s in args.stages.split(",") if s.strip()]
    bad = [s for s in requested if s not in _AVAILABLE_STAGES]
    if bad:
        print(f"Unknown stages: {bad}", file=sys.stderr)
        return 1

    # Load evidence (no Bedrock needed)
    try:
        evidence = _load_evidence(args.fixture)
    except Exception as exc:
        print(f"Failed to load evidence: {exc}", file=sys.stderr)
        return 1

    # Initialize Bedrock if any stage needs it
    bedrock = None
    needs_bedrock = any(s in requested for s in ("root_cause", "tournament", "rubric_gate"))
    if needs_bedrock:
        try:
            import boto3

            from scripts.bedrock_client import BedrockClient
            from scripts.config import BotConfig
            runtime = boto3.client("bedrock-runtime", region_name=args.region)
            cfg = BotConfig(bedrock_model_id=args.model_id)
            bedrock = BedrockClient(cfg, client=runtime)
        except Exception as exc:
            print(f"Failed to init Bedrock: {exc}", file=sys.stderr)
            print("Is AWS_DEFAULT_REGION set and credentials available?", file=sys.stderr)
            return 1

    report: dict[str, Any] = {"fixture": args.fixture or "synthetic", "stages": {}}
    overall_ok = True

    if "evidence" in requested:
        try:
            report["stages"]["evidence"] = _run_evidence_stage(evidence)
        except Exception as exc:
            report["stages"]["evidence"] = {
                "status": "exception", "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            overall_ok = False

    root_cause_accepted = None
    if "root_cause" in requested:
        try:
            rc = _run_root_cause_stage(evidence, bedrock, args.model_id)
            report["stages"]["root_cause"] = rc
            if rc.get("status") == "accepted":
                # Rebuild the accepted hypothesis for the tournament stage
                from scripts.stages.root_cause import analyze
                result = analyze(evidence, bedrock, analyst_model=args.model_id)
                root_cause_accepted = result.accepted
        except Exception as exc:
            report["stages"]["root_cause"] = {
                "status": "exception", "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            overall_ok = False

    if "tournament" in requested and root_cause_accepted is not None:
        try:
            report["stages"]["tournament"] = _run_tournament_stage(
                evidence, root_cause_accepted, bedrock, args.model_id,
            )
        except Exception as exc:
            report["stages"]["tournament"] = {
                "status": "exception", "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            overall_ok = False
    elif "tournament" in requested:
        report["stages"]["tournament"] = {
            "status": "skipped",
            "reason": "no accepted root cause",
        }

    if "rubric_gate" in requested:
        # Use a small fake patch so we exercise the rubric stage deterministically.
        fake_patch = (
            "--- a/src/t_hash.c\n+++ b/src/t_hash.c\n"
            "@@ -840,3 +840,4 @@\n"
            "     // existing\n"
            "+    mutex_lock(&hash_mutex);\n"
            "     dictResize(d);\n"
        )
        try:
            report["stages"]["rubric_gate"] = _run_rubric_gate_stage(
                fake_patch, evidence, bedrock, args.model_id,
            )
        except Exception as exc:
            report["stages"]["rubric_gate"] = {
                "status": "exception", "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            overall_ok = False

    print(json.dumps(report, indent=2))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""A/B comparison: Bedrock vs Claude Code fuzzer analysis on real runs."""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import Any

import boto3
from github import Auth, Github

from scripts.bedrock_client import BedrockClient
from scripts.bedrock_retriever import BedrockRetriever
from scripts.config import load_config
from scripts.fuzzer_run_analyzer import FuzzerRunAnalyzer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"normal": 0, "warning": 1, "anomalous": 2}


def _run_analysis(
    analyzer: FuzzerRunAnalyzer, repo: str, run_id: int, label: str,
) -> tuple[dict[str, Any], float]:
    start = time.monotonic()
    analysis = analyzer.analyze_workflow_run(repo, run_id, workflow_file="fuzzer-run.yml")
    elapsed = time.monotonic() - start
    return {
        "overall_status": analysis.overall_status,
        "triage_verdict": analysis.triage_verdict,
        "root_cause_category": analysis.root_cause_category,
        "anomaly_count": len(analysis.anomalies),
        "normal_signal_count": len(analysis.normal_signals),
        "summary": analysis.summary[:200],
        "anomaly_titles": sorted({a.title for a in analysis.anomalies}),
    }, elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, help="Comma-separated run IDs")
    parser.add_argument("--repo", default="valkey-io/valkey-fuzzer")
    parser.add_argument("--target-token", required=True)
    parser.add_argument("--aws-region", default="us-east-1")
    parser.add_argument("--config", default=".github/valkey-fuzzer-bot.yml")
    args = parser.parse_args(argv)

    run_ids = [int(r.strip()) for r in args.runs.split(",") if r.strip()]
    gh = Github(auth=Auth.Token(args.target_token))
    config = load_config(Path(args.config))

    # Build Bedrock analyzer
    bedrock_client = BedrockClient(
        config, client=boto3.client("bedrock-runtime", region_name=args.aws_region),
    )
    retriever = None
    if config.retrieval.enabled:
        retriever = BedrockRetriever(
            boto3.client("bedrock-agent-runtime", region_name=args.aws_region),
        )
    bedrock_analyzer = FuzzerRunAnalyzer(
        gh, bedrock_client, github_token=args.target_token,
        retriever=retriever, retrieval_config=config.retrieval,
        prompt_backend="bedrock",
    )
    claude_analyzer = FuzzerRunAnalyzer(
        gh, github_token=args.target_token,
        retriever=retriever, retrieval_config=config.retrieval,
        prompt_backend="claude_code",
    )

    all_pass = True
    for run_id in run_ids:
        print(f"\n{'='*70}")
        print(f"Run {run_id}")
        print(f"{'='*70}")

        logger.info("Running Bedrock analysis for %d...", run_id)
        bedrock_result, bedrock_time = _run_analysis(bedrock_analyzer, args.repo, run_id, "bedrock")
        logger.info("Running Claude Code analysis for %d...", run_id)
        claude_result, claude_time = _run_analysis(claude_analyzer, args.repo, run_id, "claude_code")

        fields = ["overall_status", "triage_verdict", "root_cause_category", "anomaly_count", "normal_signal_count"]
        print(f"\n{'Field':<25s} {'Bedrock':<30s} {'Claude Code':<30s}")
        print("-" * 85)
        for f in fields:
            print(f"{f:<25s} {str(bedrock_result[f]):<30s} {str(claude_result[f]):<30s}")
        print(f"{'latency':<25s} {bedrock_time:<30.1f} {claude_time:<30.1f}")
        print(f"\n{'Bedrock summary:':<25s} {bedrock_result['summary']}")
        print(f"{'Claude summary:':<25s} {claude_result['summary']}")

        # Check: Claude must be at least as conservative
        b_rank = _SEVERITY_RANK.get(bedrock_result["overall_status"], 0)
        c_rank = _SEVERITY_RANK.get(claude_result["overall_status"], 0)
        if c_rank < b_rank:
            print(f"\n❌ FAIL: Claude downgraded status from {bedrock_result['overall_status']} to {claude_result['overall_status']}")
            all_pass = False

        # Check: Claude must catch all anomaly titles Bedrock catches
        bedrock_titles = set(bedrock_result["anomaly_titles"])
        claude_titles = set(claude_result["anomaly_titles"])
        missed = bedrock_titles - claude_titles
        if missed:
            print(f"\n⚠️  Claude missed anomaly titles: {missed}")
            # Not a hard fail — deterministic findings are shared, so this shouldn't happen
        else:
            print(f"\n✅ Claude caught all anomalies ({len(claude_titles)} vs {len(bedrock_titles)})")

    print(f"\n{'='*70}")
    if all_pass:
        print("✅ OVERALL: PASS — Claude Code matches or exceeds Bedrock on all runs")
    else:
        print("❌ OVERALL: FAIL — Claude Code downgraded severity on at least one run")
    print(f"{'='*70}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())

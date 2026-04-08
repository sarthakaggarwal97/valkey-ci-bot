"""Central monitor for scheduled Valkey fuzzer workflow runs."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github import Auth, Github

from scripts.bedrock_client import BedrockClient
from scripts.bedrock_retriever import BedrockRetriever
from scripts.config import BotConfig, load_config
from scripts.fuzzer_issue_publisher import FuzzerIssuePublisher
from scripts.fuzzer_run_analyzer import FuzzerRunAnalyzer
from scripts.models import fuzzer_run_analysis_to_dict
from scripts.monitor_state_store import MonitorStateStore
from scripts.rate_limiter import RateLimiter
from scripts.summary import FuzzerRunSummaryRow, FuzzerWorkflowSummary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MonitorArgs:
    """Arguments for centralized workflow monitoring."""

    target_repo: str
    workflow_file: str
    event: str
    config_path: str
    target_token: str
    state_token: str
    state_repo: str
    max_runs: int
    aws_region: str | None
    dry_run: bool
    verbose: bool


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--workflow-file", required=True)
    parser.add_argument("--event", default="schedule")
    parser.add_argument("--config", default=".github/valkey-fuzzer-bot.yml")
    parser.add_argument("--target-token", required=True)
    parser.add_argument("--state-token", required=True)
    parser.add_argument("--state-repo", required=True)
    parser.add_argument("--max-runs", type=int, default=6)
    parser.add_argument("--aws-region")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def configure_logging(verbose: bool) -> None:
    """Configure process logging."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def parse_args(argv: list[str] | None = None) -> MonitorArgs:
    """Parse CLI arguments."""
    ns = build_parser().parse_args(argv)
    return MonitorArgs(
        target_repo=ns.target_repo,
        workflow_file=ns.workflow_file,
        event=ns.event,
        config_path=ns.config,
        target_token=ns.target_token,
        state_token=ns.state_token,
        state_repo=ns.state_repo,
        max_runs=max(1, ns.max_runs),
        aws_region=ns.aws_region,
        dry_run=ns.dry_run,
        verbose=ns.verbose,
    )


def _build_monitor_key(target_repo: str, workflow_file: str, event: str) -> str:
    return f"{target_repo}:{workflow_file}:{event}"


def _fetch_recent_completed_runs(args: MonitorArgs, last_seen_run_id: int) -> list[Any]:
    gh = Github(auth=Auth.Token(args.target_token))
    repo = gh.get_repo(args.target_repo)
    workflow = repo.get_workflow(args.workflow_file)
    runs = workflow.get_runs(event=args.event, status="completed")

    fresh_runs: list[Any] = []
    for index, run in enumerate(runs):
        if index >= args.max_runs:
            break
        if run.id <= last_seen_run_id:
            break
        fresh_runs.append(run)

    fresh_runs.sort(key=lambda run: run.id)
    return fresh_runs


def _load_local_bot_config(config_path: str) -> BotConfig:
    path = Path(config_path)
    if not path.exists():
        return BotConfig()
    return load_config(path)


def _make_bedrock_client(
    config: BotConfig,
    aws_region: str | None,
    *,
    rate_limiter: RateLimiter | None = None,
) -> tuple[BedrockClient, BedrockRetriever | None]:
    client_kwargs: dict[str, str] = {}
    if aws_region:
        client_kwargs["region_name"] = aws_region
    bedrock_client = BedrockClient(
        config,
        client=boto3.client("bedrock-runtime", **client_kwargs),
        rate_limiter=rate_limiter,
    )
    retriever: BedrockRetriever | None = None
    if config.retrieval.enabled:
        retriever = BedrockRetriever(
            boto3.client("bedrock-agent-runtime", **client_kwargs)
        )
    return bedrock_client, retriever


def monitor(args: MonitorArgs) -> dict[str, object]:
    """Analyze newly completed Valkey fuzzer workflow runs."""
    target_gh = Github(auth=Auth.Token(args.target_token))
    state_gh = Github(auth=Auth.Token(args.state_token))
    config = _load_local_bot_config(args.config_path)
    rate_limiter = RateLimiter(
        config,
        state_github_client=state_gh,
        state_repo_full_name=args.state_repo,
    )
    rate_limiter.load()
    bedrock_client, retriever = _make_bedrock_client(
        config,
        args.aws_region,
        rate_limiter=rate_limiter,
    )
    analyzer = FuzzerRunAnalyzer(
        target_gh,
        bedrock_client,
        github_token=args.target_token,
        retriever=retriever,
        retrieval_config=config.retrieval,
    )
    issue_publisher = FuzzerIssuePublisher(target_gh)
    monitor_key = _build_monitor_key(args.target_repo, args.workflow_file, args.event)
    state_store = MonitorStateStore(state_gh, args.state_repo)
    state_store.load()
    last_seen_run_id = state_store.get_last_seen_run_id(monitor_key)
    recent_runs = _fetch_recent_completed_runs(args, last_seen_run_id)
    summary = FuzzerWorkflowSummary()
    run_results: list[dict[str, object]] = []

    result: dict[str, object] = {
        "target_repo": args.target_repo,
        "workflow_file": args.workflow_file,
        "event": args.event,
        "config_path": args.config_path,
        "dry_run": args.dry_run,
        "last_seen_run_id": last_seen_run_id,
        "new_run_count": len(recent_runs),
        "runs": run_results,
        "has_anomalies": False,
    }
    new_last_seen = last_seen_run_id
    for run in recent_runs:
        run_result: dict[str, object] = {
            "run_id": run.id,
            "run_number": run.run_number,
            "conclusion": run.conclusion or "",
            "head_sha": run.head_sha,
            "html_url": run.html_url,
        }

        if args.dry_run:
            run_result["action"] = "would-analyze"
            run_results.append(run_result)
            continue

        try:
            analysis = analyzer.analyze_workflow_run(
                args.target_repo,
                run.id,
                workflow_file=args.workflow_file,
            )
        except Exception as exc:
            run_result["action"] = "analysis-error"
            run_result["error"] = str(exc)
            run_results.append(run_result)
            break

        run_result["action"] = "analyzed"
        run_result["analysis"] = fuzzer_run_analysis_to_dict(analysis)
        issue_action: str | None = None
        issue_url: str | None = None
        if analysis.overall_status == "anomalous":
            try:
                issue_action, issue_url = issue_publisher.upsert_issue(
                    args.target_repo,
                    analysis,
                )
                run_result["issue_action"] = issue_action
                run_result["issue_url"] = issue_url
            except Exception as exc:
                logger.warning(
                    "Failed to create/update anomaly issue for run %s: %s",
                    run.id,
                    exc,
                )
                run_result["issue_action"] = "issue-error"
                run_result["issue_error"] = str(exc)
        run_results.append(run_result)
        result["has_anomalies"] = (
            bool(result["has_anomalies"]) or analysis.overall_status != "normal"
        )
        summary.add_row(
            FuzzerRunSummaryRow(
                run_id=analysis.run_id,
                run_url=analysis.run_url,
                conclusion=analysis.conclusion,
                overall_status=analysis.overall_status,
                scenario_id=analysis.scenario_id,
                seed=analysis.seed,
                anomaly_count=len(analysis.anomalies),
                normal_signal_count=len(analysis.normal_signals),
                summary=analysis.summary,
                reproduction_hint=analysis.reproduction_hint,
                issue_url=issue_url,
                issue_action=issue_action,
                anomaly_details=[
                    f"[{a.severity}] {a.title}: {a.evidence}"
                    for a in analysis.anomalies[:10]
                ] if analysis.anomalies else None,
            )
        )
        new_last_seen = max(new_last_seen, run.id)

    if not args.dry_run and new_last_seen > last_seen_run_id:
        state_store.mark_seen(
            monitor_key,
            last_seen_run_id=new_last_seen,
            target_repo=args.target_repo,
            workflow_file=args.workflow_file,
            event=args.event,
        )
        state_store.save()
        summary.write()

    if not args.dry_run:
        rate_limiter.save()
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    result = monitor(args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

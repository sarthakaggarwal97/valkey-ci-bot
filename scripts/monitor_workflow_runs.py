"""Central monitor for scheduled workflow failures in external repositories."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github import Auth, Github

from scripts.config import BotConfig, load_config
from scripts.failure_detector import FailureDetector
from scripts.failure_store import FailureStore
from scripts.main import run_pipeline
from scripts.rate_limiter import RateLimiter
from scripts.monitor_state_store import MonitorStateStore

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
    queue_only: bool
    verbose: bool


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--workflow-file", required=True)
    parser.add_argument("--event", default="schedule")
    parser.add_argument("--config", default=".github/ci-failure-bot.yml")
    parser.add_argument("--target-token", required=True)
    parser.add_argument("--state-token", required=True)
    parser.add_argument("--state-repo", required=True)
    parser.add_argument("--max-runs", type=int, default=14)
    parser.add_argument("--aws-region")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--queue-only", action="store_true")
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
        queue_only=ns.queue_only,
        verbose=ns.verbose,
    )


def _build_monitor_key(target_repo: str, workflow_file: str, event: str) -> str:
    """Build a stable monitor key for persisted state."""
    return f"{target_repo}:{workflow_file}:{event}"


def _fetch_recent_completed_runs(args: MonitorArgs, last_seen_run_id: int) -> list[Any]:
    """Fetch recent completed workflow runs newer than the stored watermark."""
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
    """Load local agent config when present, else fall back to defaults."""
    path = Path(config_path)
    if not path.exists():
        return BotConfig()
    return load_config(path)


def _record_successful_job_observations(
    *,
    target_gh: Github,
    state_gh: Github,
    target_repo: str,
    state_repo: str,
    workflow_run_id: int,
    workflow_name: str,
    workflow_file: str,
    commit_sha: str,
    max_history_entries: int,
) -> None:
    """Persist inferred pass observations for successful jobs in a run."""
    repo = target_gh.get_repo(target_repo)
    workflow_run = repo.get_workflow_run(workflow_run_id)
    store = FailureStore(
        target_gh,
        target_repo,
        state_github_client=state_gh,
        state_repo_full_name=state_repo,
    )
    store.load()
    changed = False
    for job in workflow_run.jobs():
        if job.conclusion != "success":
            continue
        store.record_success_observation(
            workflow_name=workflow_name,
            workflow_file=workflow_file,
            job_name=job.name or "",
            matrix_params=FailureDetector.extract_matrix_params(job.name or ""),
            commit_sha=commit_sha,
            workflow_run_id=workflow_run_id,
            max_entries=max_history_entries,
        )
        changed = True
    if changed:
        store.save()


def monitor(args: MonitorArgs) -> dict[str, object]:
    """Monitor new workflow runs and process newly failed ones."""
    target_gh = Github(auth=Auth.Token(args.target_token))
    state_gh = Github(auth=Auth.Token(args.state_token))
    config = _load_local_bot_config(args.config_path)
    monitor_key = _build_monitor_key(
        args.target_repo,
        args.workflow_file,
        args.event,
    )
    state_store = MonitorStateStore(
        state_gh,
        args.state_repo,
    )
    state_store.load()
    last_seen_run_id = state_store.get_last_seen_run_id(monitor_key)
    recent_runs = _fetch_recent_completed_runs(args, last_seen_run_id)
    run_results: list[dict[str, object]] = []

    result: dict[str, object] = {
        "target_repo": args.target_repo,
        "workflow_file": args.workflow_file,
        "event": args.event,
        "config_path": args.config_path,
        "dry_run": args.dry_run,
        "queue_only": args.queue_only,
        "last_seen_run_id": last_seen_run_id,
        "new_run_count": len(recent_runs),
        "runs": run_results,
    }
    new_last_seen = last_seen_run_id
    if recent_runs:
        for run in recent_runs:
            run_result: dict[str, object] = {
                "run_id": run.id,
                "run_number": run.run_number,
                "conclusion": run.conclusion or "",
                "head_sha": run.head_sha,
                "html_url": run.html_url,
            }

            if run.conclusion != "failure":
                if not args.dry_run:
                    _record_successful_job_observations(
                        target_gh=target_gh,
                        state_gh=state_gh,
                        target_repo=args.target_repo,
                        state_repo=args.state_repo,
                        workflow_run_id=run.id,
                        workflow_name=getattr(run, "name", "") or "",
                        workflow_file=args.workflow_file,
                        commit_sha=run.head_sha,
                        max_history_entries=max(1, config.max_history_entries_per_test),
                    )
                run_result["action"] = "skip-non-failure"
                run_results.append(run_result)
                new_last_seen = max(new_last_seen, run.id)
                continue

            if args.dry_run:
                run_result["action"] = "would-process-failure"
                run_results.append(run_result)
                continue

            _record_successful_job_observations(
                target_gh=target_gh,
                state_gh=state_gh,
                target_repo=args.target_repo,
                state_repo=args.state_repo,
                workflow_run_id=run.id,
                workflow_name=getattr(run, "name", "") or "",
                workflow_file=args.workflow_file,
                commit_sha=run.head_sha,
                max_history_entries=max(1, config.max_history_entries_per_test),
            )

            try:
                pipeline_result = run_pipeline(
                    args.target_repo,
                    run.id,
                    args.config_path,
                    args.target_token,
                    aws_region=args.aws_region,
                    state_github_token=args.state_token,
                    state_repo_name=args.state_repo,
                    allow_pr_creation=not args.queue_only,
                )
            except Exception as exc:
                logger.error(
                    "Pipeline error for run %d: %s. "
                    "Advancing watermark to avoid re-processing.",
                    run.id,
                    exc,
                )
                run_result["action"] = "pipeline-error"
                run_result["error"] = str(exc)
                run_results.append(run_result)
                # Advance the watermark so the monitor does not get stuck
                # retrying a permanently failing run on every invocation.
                new_last_seen = max(new_last_seen, run.id)
                continue

            run_result["action"] = "processed-failure"
            run_result["failure_reports"] = len(pipeline_result.reports)
            run_result["job_outcomes"] = pipeline_result.job_outcomes
            run_results.append(run_result)
            new_last_seen = max(new_last_seen, run.id)

    result["new_last_seen_run_id"] = new_last_seen
    queue_state = RateLimiter(
        BotConfig(),
        state_github_client=state_gh,
        state_repo_full_name=args.state_repo,
    )
    queue_state.load()
    queued_failures = queue_state.get_queued_failures()
    result["queued_failure_count"] = len(queued_failures)
    result["has_queued_failures"] = bool(queued_failures)

    if not args.dry_run and new_last_seen > last_seen_run_id:
        state_store.mark_seen(
            monitor_key,
            last_seen_run_id=new_last_seen,
            target_repo=args.target_repo,
            workflow_file=args.workflow_file,
            event=args.event,
        )
        state_store.save()

    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv)
    configure_logging(args.verbose)
    try:
        result = monitor(args)
    except Exception as exc:
        logger.error("Workflow monitoring failed: %s", exc)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

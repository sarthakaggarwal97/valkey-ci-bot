"""Pipeline orchestrator — CLI entry point for the CI Failure Agent."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from github import Auth, Github

from scripts.bedrock_client import BedrockClient
from scripts.bedrock_retriever import BedrockRetriever
from scripts.config import BotConfig, ProjectContext, load_config, load_config_text
from scripts.event_ledger import EventLedger
from scripts.failure_detector import FailureDetector
from scripts.failure_store import FailureStore
from scripts.fix_generator import FixGenerator
from scripts.log_parser import LogParserRouter
from scripts.log_retriever import LogRetriever
from scripts.models import (
    FailedJob,
    FailureHistorySummary,
    FailureReport,
    RootCauseReport,
    ValidationResult,
    WorkflowRun,
    failure_report_from_dict,
    failure_report_to_dict,
    root_cause_report_from_dict,
)
from scripts.parsers.build_error_parser import BuildErrorParser
from scripts.parsers.gtest_parser import GTestParser
from scripts.parsers.module_api_parser import ModuleApiParser
from scripts.parsers.rdma_parser import RdmaParser
from scripts.parsers.sanitizer_parser import SanitizerParser
from scripts.parsers.sentinel_cluster_parser import SentinelClusterParser
from scripts.parsers.tcl_parser import TclTestParser
from scripts.parsers.valgrind_parser import ValgrindParser
from scripts.pr_manager import PRManager
from scripts.publish_guard import check_publish_allowed
from scripts.rate_limiter import RateLimiter
from scripts.root_cause_analyzer import RootCauseAnalyzer
from scripts.summary import ApprovalCandidate, ApprovalSummary, PRSummaryComment, WorkflowSummary
from scripts.validation_runner import ValidationRunner
from scripts.valkey_repo_context import (
    apply_valkey_runtime_defaults,
    load_valkey_repo_context,
)

logger = logging.getLogger(__name__)
_PROOF_WORKFLOW_FILE = "prove-daily-fix.yml"


def _build_parser_router() -> LogParserRouter:
    """Create a parser router with all supported parsers registered.

    Lower priority = tried first. All matching parsers contribute results.
    """
    router = LogParserRouter()
    router.register(SanitizerParser(), priority=10)
    router.register(ValgrindParser(), priority=20)
    router.register(BuildErrorParser(), priority=30)
    router.register(GTestParser(), priority=40)
    router.register(ModuleApiParser(), priority=50)
    router.register(RdmaParser(), priority=60)
    router.register(SentinelClusterParser(), priority=70)
    router.register(TclTestParser(), priority=80)
    return router
@dataclass
class PipelineResult:
    """Result of a full pipeline run with per-job visibility."""

    reports: list[FailureReport]
    job_outcomes: list[dict[str, str]]

    @staticmethod
    def empty() -> "PipelineResult":
        return PipelineResult(reports=[], job_outcomes=[])


@dataclass
class PreparedFailureCandidate:
    """Pre-parsed failure candidate selected for full analysis."""

    job: FailedJob
    report: FailureReport
    fingerprint: str | None
    parse_duration: float

def _build_workflow_run(gh: Github, repo_name: str, run_id: int) -> WorkflowRun:
    """Fetch a workflow run from GitHub and convert to our model."""
    repo = gh.get_repo(repo_name)
    run = repo.get_workflow_run(run_id)
    head_repo = run.head_repository
    return WorkflowRun(
        id=run.id,
        name=run.name or "",
        event=run.event,
        head_sha=run.head_sha,
        head_branch=run.head_branch or "",
        head_repository=head_repo.full_name if head_repo else repo_name,
        is_fork=head_repo.full_name != repo_name if head_repo else False,
        conclusion=run.conclusion or "",
        workflow_file=run.path.split("/")[-1] if run.path else "",
    )


def _load_runtime_config(
    gh: Github,
    repo_name: str,
    config_path: str,
    *,
    ref: str | None = None,
) -> BotConfig:
    """Load config from disk when present, otherwise from the consumer repo."""
    local_path = Path(config_path)
    if local_path.exists():
        return load_config(local_path)

    try:
        repo = gh.get_repo(repo_name)
        config_ref = ref or repo.default_branch
        contents = repo.get_contents(config_path, ref=config_ref)
        if isinstance(contents, list):
            raise ValueError("Config path resolved to a directory.")
        text = contents.decoded_content.decode("utf-8", errors="replace")
        return load_config_text(
            text,
            source=f"{repo_name}@{config_ref}:{config_path}",
        )
    except Exception as exc:
        logger.warning(
            "Could not load config %s from %s%s: %s. Using defaults.",
            config_path,
            repo_name,
            f" at {ref}" if ref else "",
            exc,
        )
        return BotConfig()


def _collect_source_files(
    report: FailureReport,
    root_cause_analyzer: RootCauseAnalyzer,
    project: ProjectContext,
) -> dict[str, str]:
    """Retrieve relevant source files for a failure report."""
    source_files: dict[str, str] = {}
    for parsed_failure in report.parsed_failures:
        relevant = root_cause_analyzer.identify_relevant_files(parsed_failure, project)
        contents = root_cause_analyzer._retrieve_file_contents(
            report.commit_sha,
            relevant,
            repo_name=report.repo_full_name,
        )
        source_files.update(contents)
    return source_files


def _load_root_cause_target_files(
    report: FailureReport,
    root_cause: RootCauseReport,
    root_cause_analyzer: RootCauseAnalyzer,
    source_files: dict[str, str],
) -> dict[str, str]:
    """Ensure fix generation has contents for files explicitly marked for change."""
    missing_paths = [
        path
        for path in root_cause.files_to_change
        if path and path not in source_files
    ]
    if not missing_paths:
        return source_files

    try:
        contents = root_cause_analyzer._retrieve_file_contents(
            report.commit_sha,
            missing_paths,
            repo_name=report.repo_full_name,
        )
    except Exception as exc:
        logger.warning(
            "Failed to retrieve root-cause target files for job %s: %s",
            report.job_name,
            exc,
        )
        return source_files

    merged = dict(source_files)
    merged.update(contents)
    return merged


def _build_pr_summary_comment(
    *,
    detection_duration: float,
    parsing_duration: float,
    analysis_duration: float,
    generation_duration: float,
    validation_duration: float,
    pr_creation_duration: float,
    fix_retries: int,
    validation_retries: int,
) -> PRSummaryComment:
    """Build the processing summary comment posted to created PRs."""
    comment = PRSummaryComment(
        fix_retries=fix_retries,
        validation_retries=validation_retries,
    )
    comment.add_step("detection", detection_duration)
    comment.add_step("parsing", parsing_duration)
    comment.add_step("analysis", analysis_duration)
    comment.add_step("generation", generation_duration)
    comment.add_step("validation", validation_duration)
    comment.add_step("pr_creation", pr_creation_duration)
    comment.total_duration_seconds = (
        detection_duration
        + parsing_duration
        + analysis_duration
        + generation_duration
        + validation_duration
        + pr_creation_duration
    )
    return comment


def _build_workflow_run_url(report: FailureReport) -> str:
    """Return the best available workflow-run URL for a failure report."""
    repo_name = report.repo_full_name
    if report.workflow_run_id is not None and repo_name:
        return f"https://github.com/{repo_name}/actions/runs/{report.workflow_run_id}"
    if report.commit_sha and repo_name:
        return f"https://github.com/{repo_name}/commit/{report.commit_sha}"
    return f"https://github.com/{repo_name}" if repo_name else ""


def _is_single_run_candidate(
    report: FailureReport,
    root_cause: RootCauseReport,
) -> bool:
    """Return whether one strong signal is enough to queue or create a fix."""
    if report.is_unparseable or root_cause.is_flaky or root_cause.confidence != "high":
        return False
    return True


def _apply_history_summary(
    root_cause: RootCauseReport,
    history_summary: FailureHistorySummary | None,
) -> None:
    """Copy derived failure-history metadata onto the root cause report."""
    if history_summary is None:
        return
    streak = history_summary.consecutive_failures
    failures = history_summary.failure_count
    root_cause.failure_streak = streak if isinstance(streak, int) else 0
    root_cause.total_failure_observations = failures if isinstance(failures, int) else 0
    root_cause.last_known_good_sha = (
        history_summary.last_known_good_sha
        if isinstance(history_summary.last_known_good_sha, str)
        else None
    )
    root_cause.first_bad_sha = (
        history_summary.first_bad_sha
        if isinstance(history_summary.first_bad_sha, str)
        else None
    )


def _should_queue_validated_fix(
    report: FailureReport,
    root_cause: RootCauseReport,
    history_summary: FailureHistorySummary | None,
    config: BotConfig,
) -> tuple[bool, str]:
    """Decide whether a validated fix has enough evidence for queueing.

    If the fix has already been generated and validated, always queue it.
    History is tracked for observability but never gates queueing.
    """
    if _is_single_run_candidate(report, root_cause):
        return True, "high-confidence-build-failure"

    if root_cause.is_flaky:
        return True, "flaky-test-fix"

    streak = (
        history_summary.consecutive_failures
        if history_summary and isinstance(history_summary.consecutive_failures, int)
        else 0
    )
    if streak >= max(1, config.min_failure_streak_before_queue):
        return True, "history-threshold-met"

    # A validated fix should always be queued — the analysis and validation
    # stages already confirmed the fix is sound.
    return True, "validated-fix"



def _fix_retry_count(fix_generator: FixGenerator) -> int:
    """Return the number of internal retries from the most recent generation."""
    attempts = getattr(fix_generator, "last_attempt_count", 0)
    if not isinstance(attempts, int):
        return 0
    return max(0, attempts - 1)


def _summarize_flaky_attempt(
    *,
    root_cause: RootCauseReport,
    validation_result: ValidationResult,
    required_passes: int,
) -> str:
    """Build a compact backlog entry for a flaky-failure experiment."""
    outcome = (
        f"held for {validation_result.passed_runs}/{required_passes} validation runs"
        if validation_result.passed
        else f"failed after {validation_result.passed_runs}/{required_passes} clean runs"
    )
    detail = validation_result.output.strip().splitlines()[0] if validation_result.output.strip() else ""
    detail = detail[:180]
    parts = [root_cause.description.strip(), outcome]
    if detail:
        parts.append(detail)
    return " | ".join(part for part in parts if part)


def _required_validation_runs(
    report: FailureReport,
    root_cause: RootCauseReport,
    config: BotConfig,
) -> int:
    """Return the consecutive validation pass target for one failure."""
    repeat_count = max(
        1,
        config.flaky_validation_passes
        if root_cause.is_flaky and config.flaky_campaign_enabled
        else 1,
    )
    workflow_file = (report.workflow_file or "").strip()
    if (
        workflow_file
        and workflow_file in config.soak_validation_workflows
        and config.soak_validation_passes > 1
    ):
        repeat_count = max(repeat_count, config.soak_validation_passes)
    return repeat_count


def _parse_pr_number(pr_url: str) -> int | None:
    """Extract a pull request number from a GitHub PR URL."""
    try:
        return int(pr_url.rstrip("/").split("/")[-1])
    except (TypeError, ValueError):
        return None


def _dispatch_workflow(
    *,
    repo_full_name: str,
    workflow_file: str,
    ref: str,
    token: str,
    inputs: dict[str, str],
) -> None:
    """Dispatch a GitHub Actions workflow using the REST API."""
    check_publish_allowed(
        target_repo=repo_full_name,
        action="workflow_dispatch",
        context=f"{workflow_file}@{ref}",
    )
    url = (
        f"https://api.github.com/repos/{repo_full_name}/actions/workflows/"
        f"{workflow_file}/dispatches"
    )
    payload = json.dumps({"ref": ref, "inputs": inputs}).encode("utf-8")
    request = urllib_request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "valkey-ci-agent",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=30):
            return
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"workflow-dispatch-failed: {workflow_file} {exc.code} {detail}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"workflow-dispatch-failed: {workflow_file} transport error: {exc}"
        ) from exc


def _dispatch_proof_campaign(
    *,
    state_gh: Github,
    state_repo_name: str,
    state_github_token: str,
    target_repo_name: str,
    pr_url: str,
    fingerprint: str,
    report: FailureReport,
    repeat_count: int,
    config_path: str,
) -> None:
    """Dispatch the GitHub-native proof workflow for one draft PR."""
    pr_number = _parse_pr_number(pr_url)
    if pr_number is None:
        raise RuntimeError(f"proof-dispatch-invalid-pr-url: {pr_url}")
    state_repo = state_gh.get_repo(state_repo_name)
    _dispatch_workflow(
        repo_full_name=state_repo_name,
        workflow_file=_PROOF_WORKFLOW_FILE,
        ref=state_repo.default_branch,
        token=state_github_token,
        inputs={
            "target_repo": target_repo_name,
            "pr_number": str(pr_number),
            "fingerprint": fingerprint,
            "config_path": config_path,
            "repeat_count": str(max(1, repeat_count)),
            "failure_report_json": json.dumps(
                failure_report_to_dict(report),
                separators=(",", ":"),
            ),
        },
    )


def _retrieve_failure_report(
    job: FailedJob,
    workflow_run: WorkflowRun,
    failure_source: str,
    log_retriever: LogRetriever,
    parser_router: LogParserRouter,
) -> FailureReport | None:
    """Retrieve logs and parse one failed job into a report."""
    # Retrieve log
    try:
        log_content = log_retriever.get_job_log(
            workflow_run.head_repository, job.id
        )
    except Exception as exc:
        logger.error("Log retrieval failed for job %s: %s", job.name, exc)
        return None

    if not log_content:
        logger.warning("Empty log for job %s, skipping.", job.name)
        return None

    # Parse log
    parsed_failures, raw_excerpt, is_unparseable = parser_router.parse(log_content)

    # Build failure report
    report = FailureReport(
        workflow_name=workflow_run.name,
        job_name=job.name,
        matrix_params=job.matrix_params,
        commit_sha=workflow_run.head_sha,
        failure_source=failure_source,
        parsed_failures=parsed_failures,
        raw_log_excerpt=raw_excerpt,
        is_unparseable=is_unparseable,
        workflow_file=workflow_run.workflow_file,
        repo_full_name=workflow_run.head_repository,
        workflow_run_id=workflow_run.id,
        target_branch=workflow_run.head_branch,
    )

    if is_unparseable:
        logger.warning(
            "Job %s flagged as unparseable — no structured failures extracted.",
            job.name,
        )

    return report


def _record_or_skip_failure(
    report: FailureReport,
    failure_store: FailureStore,
    *,
    seen_incidents: set[str] | None = None,
    max_history_entries: int = 0,
) -> FailureReport | None:
    """Apply dedupe / persistence rules to a parsed failure report."""
    # Check deduplication for each parsed failure
    if report.parsed_failures:
        first = report.parsed_failures[0]
        incident_key = failure_store.compute_incident_key(
            first.failure_identifier,
            first.file_path,
            test_name=first.test_name,
        )
        existing = failure_store.get_entry(incident_key)
        if seen_incidents is not None and incident_key in seen_incidents:
            if existing is not None:
                failure_store.record_incident_observation(
                    report,
                    incident_key=incident_key,
                    max_entries=max_history_entries,
                )
            logger.info(
                "Skipping duplicate incident within run: identifier=%s, incident=%s",
                first.failure_identifier, incident_key[:12],
            )
            return None
        if existing and failure_store.has_queued_pr_payload(incident_key):
            failure_store.record_incident_observation(
                report,
                incident_key=incident_key,
                max_entries=max_history_entries,
            )
            logger.info(
                "Skipping already queued/deferred failure: identifier=%s, incident=%s",
                first.failure_identifier, incident_key[:12],
            )
            return None
        if failure_store.has_open_pr(incident_key):
            failure_store.record_incident_observation(
                report,
                incident_key=incident_key,
                max_entries=max_history_entries,
            )
            logger.info(
                "Skipping duplicate failure: identifier=%s, incident=%s",
                first.failure_identifier, incident_key[:12],
            )
            return None
        # Record as processing
        failure_store.record(
            incident_key, first.failure_identifier, first.error_message,
            first.file_path, test_name=first.test_name,
        )
        failure_store.record_incident_observation(
            report,
            incident_key=incident_key,
            max_entries=max_history_entries,
        )
        if seen_incidents is not None:
            seen_incidents.add(incident_key)
    elif report.is_unparseable:
        incident_key = failure_store.compute_incident_key(report.job_name, "")
        existing = failure_store.get_entry(incident_key)
        if seen_incidents is not None and incident_key in seen_incidents:
            if existing is not None:
                failure_store.record_incident_observation(
                    report,
                    incident_key=incident_key,
                    max_entries=max_history_entries,
                )
            logger.info(
                "Skipping duplicate unparseable incident within run: job=%s, incident=%s",
                report.job_name, incident_key[:12],
            )
            return None
        if existing and failure_store.has_queued_pr_payload(incident_key):
            failure_store.record_incident_observation(
                report,
                incident_key=incident_key,
                max_entries=max_history_entries,
            )
            logger.info(
                "Skipping already queued/deferred unparseable failure: job=%s, incident=%s",
                report.job_name, incident_key[:12],
            )
            return None
        if failure_store.has_open_pr(incident_key):
            failure_store.record_incident_observation(
                report,
                incident_key=incident_key,
                max_entries=max_history_entries,
            )
            logger.info(
                "Skipping duplicate unparseable failure: job=%s, incident=%s",
                report.job_name, incident_key[:12],
            )
            return None
        failure_store.record(
            incident_key,
            report.job_name,
            report.raw_log_excerpt or "",
            "",
        )
        failure_store.record_incident_observation(
            report,
            incident_key=incident_key,
            max_entries=max_history_entries,
        )
        if seen_incidents is not None:
            seen_incidents.add(incident_key)

    return report


def _process_failure(
    job: FailedJob,
    workflow_run: WorkflowRun,
    failure_source: str,
    log_retriever: LogRetriever,
    parser_router: LogParserRouter,
    failure_store: FailureStore,
    *,
    seen_incidents: set[str] | None = None,
    max_history_entries: int = 0,
) -> FailureReport | None:
    """Process a single failed job through Detect → Parse stages.

    Returns a FailureReport, or None if the failure should be skipped.
    """
    report = _retrieve_failure_report(
        job,
        workflow_run,
        failure_source,
        log_retriever,
        parser_router,
    )
    if report is None:
        return None
    return _record_or_skip_failure(
        report,
        failure_store,
        seen_incidents=seen_incidents,
        max_history_entries=max_history_entries,
    )


def _analyze_and_fix(
    report: FailureReport,
    root_cause_analyzer: RootCauseAnalyzer,
    fix_generator: FixGenerator,
    project: "ProjectContext",
    failed_hypotheses: list[str] | None = None,
    metrics: dict[str, float | int] | None = None,
) -> tuple[RootCauseReport | None, str | None]:
    """Run Analyze → Fix stages on a single failure report.

    Returns (root_cause, diff) where either may be None if the stage
    was skipped or failed.
    """
    # Analyze root cause
    analyze_start = time.perf_counter()
    try:
        root_cause = root_cause_analyzer.analyze(report, project)
    except Exception as exc:
        logger.error("Root cause analysis failed for job %s: %s", report.job_name, exc)
        return None, None
    finally:
        if metrics is not None:
            metrics["analysis_duration"] = time.perf_counter() - analyze_start

    # Check for analysis failure
    if root_cause.description.startswith("analysis-failed"):
        logger.warning("Analysis failed for job %s: %s", report.job_name, root_cause.rationale)
        return root_cause, None

    # Skip fix generation for low confidence non-flaky failures.
    # Flaky tests always get a fix attempt — green CI is the priority.
    if root_cause.confidence == "low" and not root_cause.is_flaky:
        logger.warning(
            "Skipping fix generation for job %s: low-confidence.",
            report.job_name,
        )
        return root_cause, None

    # Skip when the analysis identified no files to change — the model
    # cannot produce a valid patch without a target.
    if not root_cause.files_to_change:
        logger.warning(
            "Skipping fix generation for job %s: no files_to_change identified.",
            report.job_name,
        )
        return root_cause, None

    # Collect source files for fix generation
    try:
        source_files = _collect_source_files(report, root_cause_analyzer, project)
    except Exception as exc:
        logger.warning("Failed to retrieve source files for job %s: %s", report.job_name, exc)
        source_files = {}
    source_files = _load_root_cause_target_files(
        report, root_cause, root_cause_analyzer, source_files,
    )

    # Generate fix
    generation_start = time.perf_counter()
    try:
        if failed_hypotheses:
            diff = fix_generator.generate(
                root_cause,
                source_files,
                failed_hypotheses=failed_hypotheses,
                repo_ref=report.commit_sha,
            )
        else:
            diff = fix_generator.generate(
                root_cause,
                source_files,
                repo_ref=report.commit_sha,
            )
    except Exception as exc:
        logger.error("Fix generation failed for job %s: %s", report.job_name, exc)
        return root_cause, None
    finally:
        if metrics is not None:
            metrics["generation_duration"] = time.perf_counter() - generation_start
            metrics["fix_retries"] = metrics.get("fix_retries", 0) + _fix_retry_count(
                fix_generator,
            )

    return root_cause, diff


def _validate_fix(
    report: FailureReport,
    root_cause: RootCauseReport,
    diff: str,
    validation_runner: ValidationRunner,
    fix_generator: FixGenerator,
    config: BotConfig,
    root_cause_analyzer: RootCauseAnalyzer,
    project: ProjectContext,
    failure_store: FailureStore | None = None,
    fingerprint: str | None = None,
    metrics: dict[str, float | int] | None = None,
) -> str | None:
    """Run validation with retry-driven fix regeneration.

    Returns the validated diff on success, or ``None`` if validation fails.
    """
    use_flaky_campaign = root_cause.is_flaky and config.flaky_campaign_enabled
    max_validation_attempts = config.max_retries_validation + 1
    if use_flaky_campaign:
        max_validation_attempts = max(
            max_validation_attempts,
            max(1, config.flaky_max_attempts_per_run),
        )
    repeat_count = _required_validation_runs(report, root_cause, config)
    current_diff = diff
    failed_hypotheses: list[str] = []
    if use_flaky_campaign and failure_store is not None and fingerprint:
        existing_campaign = failure_store.get_flaky_campaign(fingerprint)
        if existing_campaign is not None:
            failed_hypotheses = list(existing_campaign.failed_hypotheses)

    for attempt in range(max_validation_attempts):
        # Validate the fix
        validation_start = time.perf_counter()
        try:
            if repeat_count > 1:
                result = validation_runner.validate(
                    current_diff,
                    report,
                    repeat_count=repeat_count,
                )
            else:
                result = validation_runner.validate(current_diff, report)
        except Exception as exc:
            logger.error(
                "Validation error for job %s (attempt %d/%d): %s",
                report.job_name, attempt + 1, max_validation_attempts, exc,
            )
            return None
        finally:
            if metrics is not None:
                metrics["validation_duration"] = metrics.get("validation_duration", 0.0) + (
                    time.perf_counter() - validation_start
                )

        if use_flaky_campaign and failure_store is not None and fingerprint:
            summary = _summarize_flaky_attempt(
                root_cause=root_cause,
                validation_result=result,
                required_passes=repeat_count,
            )
            campaign = failure_store.record_flaky_campaign_attempt(
                fingerprint,
                report,
                root_cause,
                current_diff,
                result.output,
                passed=result.passed,
                passed_runs=result.passed_runs,
                attempted_runs=result.attempted_runs,
                summary=summary,
                strategy=result.strategy,
                max_failed_hypotheses=config.flaky_max_failed_hypotheses,
            )
            failed_hypotheses = list(campaign.failed_hypotheses)

        if result.passed:
            logger.info(
                "Validation passed for job %s on attempt %d/%d.",
                report.job_name, attempt + 1, max_validation_attempts,
            )
            break

        # Validation failed
        logger.warning(
            "Validation failed for job %s (attempt %d/%d): %s",
            report.job_name, attempt + 1, max_validation_attempts,
            result.output[:200],
        )

        # If retries remain, regenerate fix with validation failure context
        if attempt + 1 < max_validation_attempts:
            logger.info(
                "Retrying fix generation with validation failure context for job %s.",
                report.job_name,
            )
            if metrics is not None:
                metrics["validation_retries"] = metrics.get("validation_retries", 0) + 1
            try:
                source_files = _collect_source_files(
                    report, root_cause_analyzer, project,
                )
            except Exception as exc:
                logger.warning("Failed to retrieve source files for retry: %s", exc)
                source_files = {}
            source_files = _load_root_cause_target_files(
                report, root_cause, root_cause_analyzer, source_files,
            )

            try:
                generation_start = time.perf_counter()
                if failed_hypotheses:
                    new_diff = fix_generator.generate(
                        root_cause,
                        source_files,
                        validation_error=result.output,
                        failed_hypotheses=failed_hypotheses,
                        repo_ref=report.commit_sha,
                    )
                else:
                    new_diff = fix_generator.generate(
                        root_cause,
                        source_files,
                        validation_error=result.output,
                        repo_ref=report.commit_sha,
                    )
            except Exception as exc:
                logger.error("Fix regeneration failed for job %s: %s", report.job_name, exc)
                return None
            finally:
                if metrics is not None:
                    metrics["generation_duration"] = metrics.get("generation_duration", 0.0) + (
                        time.perf_counter() - generation_start
                    )
                    metrics["fix_retries"] = metrics.get("fix_retries", 0) + _fix_retry_count(
                        fix_generator,
                    )

            if new_diff is None:
                logger.error(
                    "Fix regeneration returned None for job %s, abandoning.",
                    report.job_name,
                )
                return None
            current_diff = new_diff
        else:
            # Retries exhausted — abandon the fix
            logger.error(
                "Validation failed after %d attempts for job %s, abandoning fix.",
                max_validation_attempts, report.job_name,
            )
            return None
    # Note: the for/else is intentionally omitted here — every loop
    # iteration either breaks (on pass) or returns None (on exhausted
    # retries / errors), so the else clause is unreachable.

    return current_diff


def _create_pr_from_validated_fix(
    report: FailureReport,
    root_cause: RootCauseReport,
    validated_diff: str,
    pr_manager: PRManager,
    rate_limiter: RateLimiter,
) -> str | None:
    """Create a PR from a validated patch."""
    target_branch = report.target_branch or "unstable"

    try:
        pr_url = pr_manager.create_pr(
            validated_diff, report, root_cause, target_branch,
        )
        rate_limiter.record_pr_created()
        logger.info("PR created for job %s: %s", report.job_name, pr_url)
        return pr_url
    except ValueError:
        # fork-pr-no-write-access
        logger.warning("Skipping PR creation for fork failure: %s", report.job_name)
        return None
    except RuntimeError as exc:
        logger.error("PR creation failed for job %s: %s", report.job_name, exc)
        return None


def _validate_and_create_pr(
    report: FailureReport,
    root_cause: RootCauseReport,
    diff: str,
    validation_runner: ValidationRunner,
    fix_generator: FixGenerator,
    pr_manager: PRManager,
    rate_limiter: RateLimiter,
    failure_store: FailureStore,
    config: BotConfig,
    root_cause_analyzer: RootCauseAnalyzer,
    project: ProjectContext,
) -> str | None:
    """Run Validate → PR stages with validation-failure retry loop."""
    del failure_store
    validated_diff = _validate_fix(
        report,
        root_cause,
        diff,
        validation_runner,
        fix_generator,
        config,
        root_cause_analyzer,
        project,
    )
    if validated_diff is None:
        return None
    return _create_pr_from_validated_fix(
        report,
        root_cause,
        validated_diff,
        pr_manager,
        rate_limiter,
    )


def run_pipeline(
    repo_name: str,
    run_id: int,
    config_path: str,
    github_token: str,
    aws_region: str | None = None,
    state_github_token: str | None = None,
    state_repo_name: str | None = None,
    allow_pr_creation: bool = True,
    rate_limiter: RateLimiter | None = None,
    validation_runner: ValidationRunner | None = None,
    pr_manager: PRManager | None = None,
) -> PipelineResult:
    """Execute the full pipeline: Detect → Parse → Analyze → Fix → Validate → PR."""
    gh = Github(auth=Auth.Token(github_token))
    state_gh = Github(auth=Auth.Token(state_github_token or github_token))
    state_repo = state_repo_name or repo_name

    # Fetch workflow run before config so remote config loads can use the failing SHA.
    try:
        workflow_run = _build_workflow_run(gh, repo_name, run_id)
    except Exception as exc:
        logger.error("Failed to fetch workflow run %d: %s", run_id, exc)
        return PipelineResult.empty()

    valkey_context = load_valkey_repo_context(gh, repo_name, ref=workflow_run.head_sha)
    config = _load_runtime_config(
        gh, repo_name, config_path, ref=workflow_run.head_sha,
    )
    config = apply_valkey_runtime_defaults(config, valkey_context)
    if (
        config.monitored_workflows
        and workflow_run.workflow_file
        and workflow_run.workflow_file not in config.monitored_workflows
    ):
        logger.info(
            "Workflow file %s is not monitored by config, skipping run %d.",
            workflow_run.workflow_file, run_id,
        )
        return PipelineResult.empty()

    # Build components
    detector = FailureDetector(gh)
    log_retriever = LogRetriever(gh, token=github_token)
    parser_router = _build_parser_router()
    failure_store = FailureStore(
        gh,
        repo_name,
        state_github_client=state_gh,
        state_repo_full_name=state_repo,
    )

    # Build rate limiter
    if rate_limiter is None:
        rate_limiter = RateLimiter(
            config,
            gh,
            repo_name,
            state_github_client=state_gh,
            state_repo_full_name=state_repo,
        )
    rate_limiter.load()
    event_ledger = EventLedger(
        gh,
        repo_name,
        state_github_client=state_gh,
        state_repo_full_name=state_repo,
    )
    event_ledger.record(
        "workflow.run_seen",
        str(run_id),
        repo=repo_name,
        workflow_name=workflow_run.name,
        workflow_file=workflow_run.workflow_file,
        head_sha=workflow_run.head_sha,
        head_branch=workflow_run.head_branch,
    )

    # Build Bedrock-backed components
    bedrock_kwargs: dict = {}
    if aws_region:
        bedrock_kwargs["client"] = boto3.client("bedrock-runtime", region_name=aws_region)
    bedrock_client = BedrockClient(config, rate_limiter=rate_limiter, **bedrock_kwargs)
    retriever = None
    retrieval_enabled = config.retrieval.enabled and any([
        config.retrieval.code_knowledge_base_id,
        config.retrieval.docs_knowledge_base_id,
    ])
    if retrieval_enabled:
        retriever = BedrockRetriever(
            boto3.client("bedrock-agent-runtime", region_name=aws_region),
            metric_recorder=rate_limiter.record_ai_metric,
        )
    root_cause_analyzer = RootCauseAnalyzer(bedrock_client, gh, thinking_budget=config.thinking_budget)
    root_cause_analyzer.with_retriever(retriever, config.retrieval)
    fix_generator = FixGenerator(
        bedrock_client, config,
        github_client=gh, repo_full_name=repo_name,
    )
    fix_generator.with_retriever(retriever, config.retrieval)

    # Build validation runner and PR manager (allow injection for testing)
    if validation_runner is None:
        clone_url = f"https://github.com/{repo_name}.git"
        validation_runner = ValidationRunner(
            config,
            repo_clone_url=clone_url,
            github_client=gh,
            repo_full_name=repo_name,
        )
    if pr_manager is None:
        pr_manager = PRManager(gh, repo_name, failure_store)

    # Load existing failure store
    failure_store.load()

    # Trust classification
    failure_source = FailureDetector.classify_trust(workflow_run, repo_name)
    if failure_source == "untrusted-fork":
        logger.info("Untrusted fork failure for run %d, skipping privileged stages.", run_id)
        event_ledger.record(
            "workflow.skipped",
            str(run_id),
            reason="untrusted-fork",
            repo=repo_name,
        )
        event_ledger.save()
        return PipelineResult.empty()

    # Detect failed jobs
    detect_start = time.perf_counter()
    try:
        failed_jobs = detector.detect(workflow_run)
    except Exception as exc:
        logger.error("Failure detection failed for run %d: %s", run_id, exc)
        event_ledger.record(
            "workflow.detection_failed",
            str(run_id),
            repo=repo_name,
            error=str(exc),
        )
        event_ledger.save()
        return PipelineResult.empty()
    detect_duration = time.perf_counter() - detect_start

    if not failed_jobs:
        logger.info("No actionable failures in run %d.", run_id)
        event_ledger.record(
            "workflow.no_failures",
            str(run_id),
            repo=repo_name,
        )
        event_ledger.save()
        return PipelineResult.empty()

    # Workflow summary collector
    summary = WorkflowSummary(mode="analyze")
    approval_summary = ApprovalSummary()

    # Pre-scan all failed jobs so only structured, unique incidents consume the cap.
    failed_jobs.sort(key=lambda j: j.name)
    reports: list[FailureReport] = []
    prepared_candidates: list[PreparedFailureCandidate] = []
    seen_incidents: set[str] = set()
    max_history_entries = max(1, config.max_history_entries_per_test)

    for job in failed_jobs:
        parse_start = time.perf_counter()
        report = _retrieve_failure_report(
            job,
            workflow_run,
            failure_source,
            log_retriever,
            parser_router,
        )
        parse_duration = time.perf_counter() - parse_start
        if not report:
            summary.add_result(job.name, "", "skipped")
            continue

        report = _record_or_skip_failure(
            report,
            failure_store,
            seen_incidents=seen_incidents,
            max_history_entries=max_history_entries,
        )
        if not report:
            summary.add_result(job.name, "", "skipped")
            continue

        fingerprint = _get_report_fingerprint(report, failure_store)
        if fingerprint:
            failure_store.record_failure_observation(
                report,
                fingerprint=fingerprint,
                max_entries=max_history_entries,
            )
        event_ledger.record(
            "failure.observed",
            fingerprint or job.name,
            repo=report.repo_full_name or repo_name,
            workflow_run_id=report.workflow_run_id,
            workflow_file=report.workflow_file,
            job_name=report.job_name,
            parsed=bool(report.parsed_failures),
            unparseable=report.is_unparseable,
        )

        if report.is_unparseable or not report.parsed_failures:
            logger.info(
                "Skipping job %s from analysis: unparseable failures do not consume "
                "the per-run analysis cap.",
                job.name,
            )
            summary.add_result(job.name, "", "unparseable")
            continue

        prepared_candidates.append(
            PreparedFailureCandidate(
                job=job,
                report=report,
                fingerprint=fingerprint,
                parse_duration=parse_duration,
            )
        )

    if config.max_failures_per_run > 0 and len(prepared_candidates) > config.max_failures_per_run:
        skipped = prepared_candidates[config.max_failures_per_run:]
        for candidate in skipped:
            logger.warning(
                "Skipping job %s: skipped-rate-limit (max %d structured failures per run exceeded).",
                candidate.job.name, config.max_failures_per_run,
            )
            summary.add_result(candidate.job.name, "", "skipped-rate-limit")
        prepared_candidates = prepared_candidates[: config.max_failures_per_run]

    # Process each selected structured failure through Analyze → Fix → Validate → PR
    # Cache root cause results so correlated failures (same commit + error
    # signature) share a single analysis instead of redundant Bedrock calls.
    _root_cause_cache: dict[str, tuple[RootCauseReport | None, str]] = {}

    for candidate in prepared_candidates:
        job = candidate.job
        report = candidate.report
        fingerprint = candidate.fingerprint
        parse_duration = candidate.parse_duration
        try:
            metrics: dict[str, float | int] = {
                "analysis_duration": 0.0,
                "generation_duration": 0.0,
                "validation_duration": 0.0,
                "fix_retries": 0,
                "validation_retries": 0,
            }
            # Check token budget before Bedrock-backed stages
            if not rate_limiter.can_use_tokens(1):
                logger.warning(
                    "Skipping job %s: token-budget-exhausted.", job.name,
                )
                summary.add_result(job.name, "", "skipped-token-budget-exhausted")
                continue

            reports.append(report)
            existing_campaign = (
                failure_store.get_flaky_campaign(fingerprint)
                if fingerprint
                else None
            )

            failure_id = report.parsed_failures[0].failure_identifier if report.parsed_failures else ""
            domain_context = (
                valkey_context.render_failure_guidance(report)
                if valkey_context is not None
                else ""
            )
            root_cause_analyzer.with_domain_context(domain_context)
            fix_generator.with_domain_context(domain_context)

            # Cross-failure correlation: reuse root cause analysis for
            # failures with the same commit + error signature.
            correlation_key = f"{report.commit_sha}:{failure_id}"
            cached_root_cause = _root_cause_cache.get(correlation_key)
            if cached_root_cause is not None:
                root_cause, _ = cached_root_cause[0], cached_root_cause[1]
                logger.info(
                    "Reusing cached root cause for job %s (correlation=%s).",
                    job.name, correlation_key[:40],
                )
                # Still generate a fresh fix for this specific job
                diff = None
                if root_cause and not root_cause.description.startswith("analysis-failed"):
                    if root_cause.confidence != "low" or root_cause.is_flaky:
                        if root_cause.files_to_change:
                            try:
                                source_files = _collect_source_files(
                                    report, root_cause_analyzer, config.project,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Failed to retrieve source files for job %s: %s",
                                    job.name, exc,
                                )
                                source_files = {}
                            source_files = _load_root_cause_target_files(
                                report, root_cause, root_cause_analyzer, source_files,
                            )
                            try:
                                diff = fix_generator.generate(
                                    root_cause, source_files,
                                    repo_ref=report.commit_sha,
                                    failed_hypotheses=(
                                        list(existing_campaign.failed_hypotheses)
                                        if existing_campaign is not None
                                        else None
                                    ),
                                )
                            except Exception as exc:
                                logger.error(
                                    "Fix generation failed for correlated job %s: %s",
                                    job.name, exc,
                                )
            else:
                # Analyze → Fix
                root_cause, diff = _analyze_and_fix(
                    report,
                    root_cause_analyzer,
                    fix_generator,
                    config.project,
                    failed_hypotheses=(
                        list(existing_campaign.failed_hypotheses)
                        if existing_campaign is not None
                        else None
                    ),
                    metrics=metrics,
                )
                _root_cause_cache[correlation_key] = (root_cause, failure_id)

            if not diff or not root_cause:
                outcome = "analysis-failed" if not root_cause else "no-fix-generated"
                event_ledger.record(
                    "fix.skipped",
                    fingerprint or job.name,
                    job_name=job.name,
                    failure_identifier=failure_id,
                    outcome=outcome,
                )
                summary.add_result(job.name, failure_id, outcome)
                continue

            event_ledger.record(
                "root_cause.analyzed",
                fingerprint or job.name,
                job_name=job.name,
                failure_identifier=failure_id,
                confidence=root_cause.confidence,
                is_flaky=root_cause.is_flaky,
                files_to_change=root_cause.files_to_change,
            )

            # Validate → PR (only if we have a diff)
            validated_diff = _validate_fix(
                report,
                root_cause,
                diff,
                validation_runner,
                fix_generator,
                config,
                root_cause_analyzer,
                config.project,
                failure_store=failure_store,
                fingerprint=fingerprint,
                metrics=metrics,
            )
            if validated_diff is None:
                event_ledger.record(
                    "validation.failed",
                    fingerprint or job.name,
                    job_name=job.name,
                    failure_identifier=failure_id,
                    confidence=root_cause.confidence,
                    is_flaky=root_cause.is_flaky,
                )
                summary.add_result(job.name, failure_id, "validation-failed")
                continue

            event_ledger.record(
                "validation.passed",
                fingerprint or job.name,
                job_name=job.name,
                failure_identifier=failure_id,
                confidence=root_cause.confidence,
                is_flaky=root_cause.is_flaky,
            )

            history_summary = None
            if report.parsed_failures:
                history_summary = failure_store.summarize_history(
                    report.workflow_file,
                    report.job_name,
                    report.matrix_params,
                    report.parsed_failures[0].failure_identifier,
                )
            else:
                history_summary = failure_store.summarize_history(
                    report.workflow_file,
                    report.job_name,
                    report.matrix_params,
                    report.job_name,
                )
            _apply_history_summary(root_cause, history_summary)

            should_queue, queue_reason = _should_queue_validated_fix(
                report,
                root_cause,
                history_summary,
                config,
            )
            if not should_queue:
                summary.add_result(job.name, failure_id, queue_reason)
                continue

            if not allow_pr_creation:
                if fingerprint:
                    failure_store.record_queued_pr(
                        fingerprint,
                        report,
                        root_cause,
                        validated_diff,
                        report.target_branch or "unstable",
                    )
                    logger.info(
                        "Queued validated fix for job %s pending manual approval, "
                        "fingerprint=%s.",
                        job.name,
                        fingerprint[:12],
                    )
                    approval_summary.add_candidate(
                        ApprovalCandidate(
                            job_name=job.name,
                            failure_identifier=failure_id,
                            workflow_run_url=_build_workflow_run_url(report),
                            confidence=root_cause.confidence,
                            is_flaky=root_cause.is_flaky,
                            failure_streak=root_cause.failure_streak,
                            total_failure_observations=root_cause.total_failure_observations,
                            last_known_good_sha=root_cause.last_known_good_sha,
                            first_bad_sha=root_cause.first_bad_sha,
                            files_to_change=root_cause.files_to_change,
                            rationale=root_cause.rationale,
                        )
                    )
                    event_ledger.record(
                        "fix.queued",
                        fingerprint,
                        job_name=job.name,
                        failure_identifier=failure_id,
                        reason="manual-approval",
                        target_branch=report.target_branch or "unstable",
                    )
                summary.add_result(job.name, failure_id, "queued-manual-approval")
            elif rate_limiter.reserve_pr_creation():
                pr_creation_start = time.perf_counter()
                pr_url = _create_pr_from_validated_fix(
                    report,
                    root_cause,
                    validated_diff,
                    pr_manager,
                    rate_limiter,
                )
                pr_creation_duration = time.perf_counter() - pr_creation_start
                if pr_url:
                    event_ledger.record(
                        "pr.created",
                        fingerprint or job.name,
                        job_name=job.name,
                        failure_identifier=failure_id,
                        pr_url=pr_url,
                    )
                    if fingerprint:
                        failure_store.mark_flaky_campaign_status(
                            fingerprint,
                            "pr-created",
                        )
                    pr_manager.post_summary_comment(
                        pr_url,
                        _build_pr_summary_comment(
                            detection_duration=detect_duration,
                            parsing_duration=parse_duration,
                            analysis_duration=float(metrics["analysis_duration"]),
                            generation_duration=float(metrics["generation_duration"]),
                            validation_duration=float(metrics["validation_duration"]),
                            pr_creation_duration=pr_creation_duration,
                            fix_retries=int(metrics["fix_retries"]),
                            validation_retries=int(metrics["validation_retries"]),
                        ),
                    )
                    summary.add_result(job.name, failure_id, "pr-created")
                else:
                    event_ledger.record(
                        "pr.creation_failed",
                        fingerprint or job.name,
                        job_name=job.name,
                        failure_identifier=failure_id,
                    )
                    summary.add_result(job.name, failure_id, "pr-creation-failed")
            else:
                if fingerprint:
                    failure_store.record_queued_pr(
                        fingerprint,
                        report,
                        root_cause,
                        validated_diff,
                        report.target_branch or "unstable",
                    )
                    logger.warning(
                        "Skipping PR creation for job %s: "
                        "daily-rate-limit, fingerprint=%s queued.",
                        job.name, fingerprint[:12],
                    )
                    event_ledger.record(
                        "fix.queued",
                        fingerprint,
                        job_name=job.name,
                        failure_identifier=failure_id,
                        reason="rate-limit",
                        target_branch=report.target_branch or "unstable",
                    )
                summary.add_result(job.name, failure_id, "queued-rate-limit")
        except Exception as exc:
            logger.error("Error processing job %s: %s", job.name, exc)
            event_ledger.record(
                "job.error",
                job.name,
                workflow_run_id=run_id,
                error=str(exc),
            )
            summary.add_result(job.name, "", "error", error=str(exc))
            continue

    # Save failure store and rate limiter state
    failure_store.save()
    rate_limiter.save()
    event_ledger.save()

    # Emit workflow summary
    summary.write()
    if not allow_pr_creation:
        approval_summary.write()

    logger.info("Processed %d failures from run %d.", len(reports), run_id)
    job_outcomes = [
        {
            "job_name": r.job_name,
            "failure_identifier": r.failure_identifier,
            "outcome": r.outcome,
            **({"error": r.error} if r.error else {}),
        }
        for r in summary.results
    ]
    return PipelineResult(reports=reports, job_outcomes=job_outcomes)


def _get_report_fingerprint(
    report: FailureReport, failure_store: FailureStore
) -> str | None:
    """Compute the canonical incident key for a report."""
    if not report.parsed_failures:
        return failure_store.compute_incident_key(
            report.job_name, "",
        )
    first = report.parsed_failures[0]
    return failure_store.compute_incident_key(
        first.failure_identifier,
        first.file_path,
        test_name=first.test_name,
    )


def run_reconciliation(
    repo_name: str,
    config_path: str,
    github_token: str,
    aws_region: str | None = None,
    state_github_token: str | None = None,
    state_repo_name: str | None = None,
    rate_limiter: RateLimiter | None = None,
    *,
    draft_prs: bool = False,
) -> int:
    """Drain queued failures when rate limits have reset.

    Called during scheduled reconciliation runs. Processes queued
    failures up to the daily PR limit.

    Returns the number of queued failures successfully processed.
    """
    gh = Github(auth=Auth.Token(github_token))
    state_gh = Github(auth=Auth.Token(state_github_token or github_token))
    state_repo = state_repo_name or repo_name
    dispatch_token = state_github_token or github_token
    config = _load_runtime_config(gh, repo_name, config_path)

    # Build rate limiter
    if rate_limiter is None:
        rate_limiter = RateLimiter(
            config,
            gh,
            repo_name,
            state_github_client=state_gh,
            state_repo_full_name=state_repo,
        )
    rate_limiter.load()

    failure_store = FailureStore(
        gh,
        repo_name,
        state_github_client=state_gh,
        state_repo_full_name=state_repo,
    )
    failure_store.load()
    pr_state_transitions = failure_store.reconcile_pr_states()
    pr_manager = PRManager(gh, repo_name, failure_store)
    event_ledger = EventLedger(
        gh,
        repo_name,
        state_github_client=state_gh,
        state_repo_full_name=state_repo,
    )
    for transition in pr_state_transitions:
        event_type = (
            "pr.merged"
            if transition.new_status == "merged"
            else "pr.closed_without_merge"
        )
        event_ledger.record(
            event_type,
            transition.fingerprint,
            pr_url=transition.pr_url,
            pr_number=transition.pr_number,
            previous_status=transition.previous_status,
            new_status=transition.new_status,
            github_state=transition.github_state,
            merged=transition.merged,
            source="reconciliation",
        )

    queued = failure_store.list_queued_failures()
    if not queued:
        failure_store.save()
        event_ledger.save()
        logger.info("No queued failures to drain.")
        return 0

    logger.info("Reconciliation: %d queued failure(s) to process.", len(queued))

    # Build components needed for queued PR creation.
    # Workflow summary collector
    summary = WorkflowSummary(mode="reconcile")

    processed = 0
    for fingerprint in list(queued):
        # Check if we can still create PRs
        if not rate_limiter.reserve_pr_creation():
            logger.info(
                "Rate limit reached during reconciliation drain, stopping. "
                "%d failure(s) remain queued.", len(queued) - processed,
            )
            break

        # Look up the failure in the store to get context
        entry = failure_store.get_entry(fingerprint)
        if entry is None:
            logger.warning(
                "Queued fingerprint %s not found in failure store.",
                fingerprint[:12],
            )
            summary.add_result("", fingerprint[:12], "dequeued-not-found")
            processed += 1
            continue

        # Skip if already has an open PR
        if failure_store.has_open_pr(fingerprint):
            logger.info(
                "Queued fingerprint %s already has an open PR, clearing queue payload.",
                fingerprint[:12],
            )
            failure_store.clear_queued_pr(fingerprint)
            summary.add_result("", entry.failure_identifier, "dequeued-has-open-pr")
            processed += 1
            continue

        if not entry.queued_pr_payload:
            logger.warning(
                "Queued fingerprint %s has no persisted PR payload.",
                fingerprint[:12],
            )
            failure_store.clear_queued_pr(fingerprint)
            summary.add_result("", entry.failure_identifier, "dequeued-missing-payload")
            processed += 1
            continue

        logger.info(
            "Processing queued failure %s (%s).",
            fingerprint[:12], entry.failure_identifier,
        )

        payload = entry.queued_pr_payload
        report = failure_report_from_dict(payload.get("failure_report", {}))
        root_cause = root_cause_report_from_dict(payload.get("root_cause", {}))
        patch = str(payload.get("patch", ""))
        target_branch = str(
            payload.get("target_branch") or report.target_branch or "unstable"
        )

        try:
            pr_url = pr_manager.create_pr(
                patch,
                report,
                root_cause,
                target_branch,
                draft=draft_prs,
            )
            rate_limiter.record_pr_created()
            failure_store.mark_flaky_campaign_status(fingerprint, "pr-created")
            failure_store.clear_queued_pr(fingerprint)
            event_ledger.record(
                "pr.created",
                fingerprint,
                job_name=report.job_name,
                failure_identifier=entry.failure_identifier,
                pr_url=pr_url,
                source="reconciliation",
            )
            if draft_prs and dispatch_token:
                proof_runs = _required_validation_runs(report, root_cause, config)
                if proof_runs > 1:
                    failure_store.update_proof_campaign(
                        fingerprint,
                        status="pending",
                        proof_url=pr_url,
                        required_runs=proof_runs,
                    )
                    try:
                        _dispatch_proof_campaign(
                            state_gh=state_gh,
                            state_repo_name=state_repo,
                            state_github_token=dispatch_token,
                            target_repo_name=repo_name,
                            pr_url=pr_url,
                            fingerprint=fingerprint,
                            report=report,
                            repeat_count=proof_runs,
                            config_path=config_path,
                        )
                        event_ledger.record(
                            "proof.dispatched",
                            fingerprint,
                            job_name=report.job_name,
                            failure_identifier=entry.failure_identifier,
                            pr_url=pr_url,
                            proof_runs=proof_runs,
                            source="reconciliation",
                        )
                    except RuntimeError as exc:
                        failure_store.update_proof_campaign(
                            fingerprint,
                            status="dispatch-failed",
                            summary=str(exc),
                            proof_url=pr_url,
                            required_runs=proof_runs,
                        )
                        event_ledger.record(
                            "proof.dispatch_failed",
                            fingerprint,
                            job_name=report.job_name,
                            failure_identifier=entry.failure_identifier,
                            pr_url=pr_url,
                            proof_runs=proof_runs,
                            error=str(exc),
                            source="reconciliation",
                        )
                        logger.warning(
                            "Proof workflow dispatch failed for %s: %s",
                            fingerprint[:12],
                            exc,
                        )
            summary.add_result(
                report.job_name, entry.failure_identifier, "pr-created",
            )
            logger.info(
                "Created queued PR for fingerprint %s: %s",
                fingerprint[:12], pr_url,
            )
        except (RuntimeError, ValueError) as exc:
            attempts = failure_store.record_queued_pr_failure(fingerprint, str(exc))
            event_ledger.record(
                "pr.creation_failed",
                fingerprint,
                job_name=report.job_name,
                failure_identifier=entry.failure_identifier,
                source="reconciliation",
                attempts=attempts,
                error=str(exc),
            )
            if config.queued_pr_max_attempts > 0 and attempts >= config.queued_pr_max_attempts:
                failure_store.mark_queued_pr_dead_letter(fingerprint, str(exc))
                event_ledger.record(
                    "fix.dead_lettered",
                    fingerprint,
                    job_name=report.job_name,
                    failure_identifier=entry.failure_identifier,
                    attempts=attempts,
                    error=str(exc),
                )
                summary.add_result(
                    report.job_name,
                    entry.failure_identifier,
                    "queued-pr-dead-letter",
                    error=str(exc),
                )
                processed += 1
                continue
            summary.add_result(
                report.job_name, entry.failure_identifier, "pr-creation-failed",
                error=str(exc),
            )
            logger.warning(
                "Queued PR creation failed for fingerprint %s; keeping payload "
                "queued for a future reconciliation attempt: %s",
                fingerprint[:12],
                exc,
            )
            continue
        processed += 1

    # Save state
    failure_store.save()
    rate_limiter.save()
    event_ledger.save()

    # Emit workflow summary
    summary.write()

    logger.info("Reconciliation complete: %d failure(s) processed.", processed)
    return processed


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="CI Failure Agent Pipeline")
    parser.add_argument("--repo", required=True, help="Repository full name (owner/repo)")
    parser.add_argument("--run-id", type=int, default=None, help="Workflow run ID (required for analyze mode)")
    parser.add_argument("--config", default=".github/ci-failure-bot.yml", help="Config file path")
    parser.add_argument("--token", required=True, help="GitHub token")
    parser.add_argument("--state-token", default=None, help="GitHub token for agent-state storage")
    parser.add_argument("--state-repo", default=None, help="Repository full name used for agent-state persistence")
    parser.add_argument("--aws-region", default=None, help="AWS region for Bedrock client")
    parser.add_argument("--mode", default="analyze", choices=["analyze", "reconcile"],
                        help="Pipeline mode: 'analyze' for normal failure processing, 'reconcile' for draining queued failures")
    parser.add_argument(
        "--queue-only",
        action="store_true",
        help="Analyze and validate fixes, but queue them for approval instead of opening PRs.",
    )
    parser.add_argument(
        "--draft-prs",
        action="store_true",
        help="Open draft pull requests when reconciling queued fixes.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.mode == "reconcile":
        count = run_reconciliation(
            args.repo,
            args.config,
            args.token,
            aws_region=args.aws_region,
            state_github_token=args.state_token,
            state_repo_name=args.state_repo,
            draft_prs=args.draft_prs,
        )
        logger.info("Reconciliation drained %d queued failure(s).", count)
        sys.exit(0)

    # Analyze mode requires a run ID
    if args.run_id is None:
        parser.error("--run-id is required for analyze mode")

    result = run_pipeline(
        args.repo,
        args.run_id,
        args.config,
        args.token,
        aws_region=args.aws_region,
        state_github_token=args.state_token,
        state_repo_name=args.state_repo,
        allow_pr_creation=not args.queue_only,
    )
    if not result.reports:
        logger.info("No failures processed.")
        sys.exit(0)

    logger.info("Pipeline complete. %d failure(s) processed.", len(result.reports))


if __name__ == "__main__":
    main()

"""Pipeline orchestrator — CLI entry point for the CI Failure Bot."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from github import Auth, Github

from scripts.bedrock_client import BedrockClient, BedrockError
from scripts.bedrock_retriever import BedrockRetriever
from scripts.config import BotConfig, ProjectContext, load_config, load_config_text
from scripts.failure_detector import FailureDetector
from scripts.failure_store import FailureStore
from scripts.fix_generator import FixGenerator
from scripts.log_parser import LogParserRouter
from scripts.log_retriever import LogRetriever
from scripts.models import (
    FailedJob,
    FailureReport,
    FailureHistorySummary,
    RootCauseReport,
    ValidationResult,
    WorkflowRun,
    failure_report_from_dict,
    root_cause_report_from_dict,
)
from scripts.parsers.build_error_parser import BuildErrorParser
from scripts.parsers.gtest_parser import GTestParser
from scripts.parsers.sentinel_cluster_parser import SentinelClusterParser
from scripts.parsers.tcl_parser import TclTestParser
from scripts.pr_manager import PRManager
from scripts.rate_limiter import RateLimiter
from scripts.root_cause_analyzer import RootCauseAnalyzer
from scripts.summary import ApprovalCandidate, ApprovalSummary, PRSummaryComment, WorkflowSummary
from scripts.validation_runner import ValidationRunner

logger = logging.getLogger(__name__)


def _build_parser_router() -> LogParserRouter:
    """Create a parser router with all supported parsers registered."""
    router = LogParserRouter()
    router.register(GTestParser())
    router.register(TclTestParser())
    router.register(SentinelClusterParser())
    router.register(BuildErrorParser())
    return router
@dataclass
class PipelineResult:
    """Result of a full pipeline run with per-job visibility."""

    reports: list[FailureReport]
    job_outcomes: list[dict[str, str]]

    @staticmethod
    def empty() -> "PipelineResult":
        return PipelineResult(reports=[], job_outcomes=[])




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


def _process_failure(
    job: FailedJob,
    workflow_run: WorkflowRun,
    failure_source: str,
    log_retriever: LogRetriever,
    parser_router: LogParserRouter,
    failure_store: FailureStore,
) -> FailureReport | None:
    """Process a single failed job through Detect → Parse stages.

    Returns a FailureReport, or None if the failure should be skipped.
    """
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

    # Check deduplication for each parsed failure
    if parsed_failures:
        first = parsed_failures[0]
        fp = failure_store.compute_fingerprint(
            first.failure_identifier, first.error_message, first.file_path
        )
        existing = failure_store.get_entry(fp)
        if existing and existing.status == "queued":
            logger.info(
                "Skipping already queued failure: identifier=%s, fingerprint=%s",
                first.failure_identifier, fp[:12],
            )
            return None
        if failure_store.has_open_pr(fp):
            logger.info(
                "Skipping duplicate failure: identifier=%s, fingerprint=%s",
                first.failure_identifier, fp[:12],
            )
            return None
        # Record as processing
        failure_store.record(
            fp, first.failure_identifier, first.error_message,
            first.file_path, test_name=first.test_name,
        )
    elif is_unparseable:
        fp = failure_store.compute_fingerprint(job.name, raw_excerpt or "", "")
        existing = failure_store.get_entry(fp)
        if existing and existing.status == "queued":
            logger.info(
                "Skipping already queued unparseable failure: job=%s, fingerprint=%s",
                job.name, fp[:12],
            )
            return None
        if failure_store.has_open_pr(fp):
            logger.info(
                "Skipping duplicate unparseable failure: job=%s, fingerprint=%s",
                job.name, fp[:12],
            )
            return None
        failure_store.record(
            fp,
            job.name,
            raw_excerpt or "",
            "",
        )

    return report


def _analyze_and_fix(
    report: FailureReport,
    root_cause_analyzer: RootCauseAnalyzer,
    fix_generator: FixGenerator,
    project: "ProjectContext",
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
        diff = fix_generator.generate(root_cause, source_files)
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
    metrics: dict[str, float | int] | None = None,
) -> str | None:
    """Run validation with retry-driven fix regeneration.

    Returns the validated diff on success, or ``None`` if validation fails.
    """
    max_validation_attempts = config.max_retries_validation + 1
    current_diff = diff

    for attempt in range(max_validation_attempts):
        # Validate the fix
        validation_start = time.perf_counter()
        try:
            result: ValidationResult = validation_runner.validate(current_diff, report)
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
                new_diff = fix_generator.generate(
                    root_cause, source_files,
                    validation_error=result.output,
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

    config = _load_runtime_config(
        gh, repo_name, config_path, ref=workflow_run.head_sha,
    )
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
    log_retriever = LogRetriever(gh)
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
        )
    root_cause_analyzer = RootCauseAnalyzer(bedrock_client, gh)
    root_cause_analyzer.with_retriever(retriever, config.retrieval)
    fix_generator = FixGenerator(
        bedrock_client, config,
        github_client=gh, repo_full_name=repo_name,
    )
    fix_generator.with_retriever(retriever, config.retrieval)

    # Build validation runner and PR manager (allow injection for testing)
    if validation_runner is None:
        clone_url = f"https://github.com/{repo_name}.git"
        validation_runner = ValidationRunner(config, repo_clone_url=clone_url)
    if pr_manager is None:
        pr_manager = PRManager(gh, repo_name, failure_store)

    # Load existing failure store
    failure_store.load()

    # Trust classification
    failure_source = FailureDetector.classify_trust(workflow_run, repo_name)
    if failure_source == "untrusted-fork":
        logger.info("Untrusted fork failure for run %d, skipping privileged stages.", run_id)
        return PipelineResult.empty()

    # Detect failed jobs
    detect_start = time.perf_counter()
    try:
        failed_jobs = detector.detect(workflow_run)
    except Exception as exc:
        logger.error("Failure detection failed for run %d: %s", run_id, exc)
        return PipelineResult.empty()
    detect_duration = time.perf_counter() - detect_start

    if not failed_jobs:
        logger.info("No actionable failures in run %d.", run_id)
        return PipelineResult.empty()

    # Workflow summary collector
    summary = WorkflowSummary(mode="analyze")
    approval_summary = ApprovalSummary()

    # Enforce max_failures_per_run with alphabetical ordering
    failed_jobs.sort(key=lambda j: j.name)
    if len(failed_jobs) > config.max_failures_per_run:
        skipped = failed_jobs[config.max_failures_per_run:]
        for j in skipped:
            logger.warning(
                "Skipping job %s: skipped-rate-limit (max %d per run exceeded).",
                j.name, config.max_failures_per_run,
            )
            summary.add_result(j.name, "", "skipped-rate-limit")
        failed_jobs = failed_jobs[: config.max_failures_per_run]

    # Process each failure through Detect → Parse → Analyze → Fix → Validate → PR
    reports: list[FailureReport] = []
    for job in failed_jobs:
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

            parse_start = time.perf_counter()
            report = _process_failure(
                job, workflow_run, failure_source,
                log_retriever, parser_router, failure_store,
            )
            parse_duration = time.perf_counter() - parse_start
            if not report:
                summary.add_result(job.name, "", "skipped")
                continue

            reports.append(report)
            fingerprint = _get_report_fingerprint(report, failure_store)
            if fingerprint:
                failure_store.record_failure_observation(
                    report,
                    fingerprint=fingerprint,
                    max_entries=max(1, config.max_history_entries_per_test),
                )

            failure_id = report.parsed_failures[0].failure_identifier if report.parsed_failures else ""

            # Analyze → Fix
            root_cause, diff = _analyze_and_fix(
                report, root_cause_analyzer, fix_generator, config.project, metrics,
            )

            if not diff or not root_cause:
                outcome = "analysis-failed" if not root_cause else "no-fix-generated"
                summary.add_result(job.name, failure_id, outcome)
                continue

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
                metrics,
            )
            if validated_diff is None:
                summary.add_result(job.name, failure_id, "validation-failed")
                continue

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
                    rate_limiter.queue_failure(fingerprint)
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
                summary.add_result(job.name, failure_id, "queued-manual-approval")
            elif rate_limiter.can_create_pr():
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
                    rate_limiter.queue_failure(fingerprint)
                    logger.warning(
                        "Skipping PR creation for job %s: "
                        "daily-rate-limit, fingerprint=%s queued.",
                        job.name, fingerprint[:12],
                    )
                summary.add_result(job.name, failure_id, "queued-rate-limit")
        except Exception as exc:
            logger.error("Error processing job %s: %s", job.name, exc)
            summary.add_result(job.name, "", "error", error=str(exc))
            continue

    # Save failure store and rate limiter state
    failure_store.save()
    rate_limiter.save()

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
    """Compute the fingerprint for a report."""
    if not report.parsed_failures:
        return failure_store.compute_fingerprint(
            report.job_name, report.raw_log_excerpt or "", "",
        )
    first = report.parsed_failures[0]
    return failure_store.compute_fingerprint(
        first.failure_identifier, first.error_message, first.file_path
    )


def run_reconciliation(
    repo_name: str,
    config_path: str,
    github_token: str,
    aws_region: str | None = None,
    state_github_token: str | None = None,
    state_repo_name: str | None = None,
    rate_limiter: RateLimiter | None = None,
) -> int:
    """Drain queued failures when rate limits have reset.

    Called during scheduled reconciliation runs. Processes queued
    failures up to the daily PR limit.

    Returns the number of queued failures successfully processed.
    """
    gh = Github(auth=Auth.Token(github_token))
    state_gh = Github(auth=Auth.Token(state_github_token or github_token))
    state_repo = state_repo_name or repo_name
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

    queued = rate_limiter.get_queued_failures()
    if not queued:
        logger.info("No queued failures to drain.")
        return 0

    logger.info("Reconciliation: %d queued failure(s) to process.", len(queued))

    # Build components needed for the pipeline
    failure_store = FailureStore(
        gh,
        repo_name,
        state_github_client=state_gh,
        state_repo_full_name=state_repo,
    )
    failure_store.load()
    failure_store.reconcile_pr_states()
    pr_manager = PRManager(gh, repo_name, failure_store)

    # Workflow summary collector
    summary = WorkflowSummary(mode="reconcile")

    processed = 0
    for fingerprint in list(queued):
        # Check if we can still create PRs
        if not rate_limiter.can_create_pr():
            logger.info(
                "Rate limit reached during reconciliation drain, stopping. "
                "%d failure(s) remain queued.", len(queued) - processed,
            )
            break

        # Look up the failure in the store to get context
        entry = failure_store.get_entry(fingerprint)
        if entry is None:
            logger.warning(
                "Queued fingerprint %s not found in failure store, dequeuing.",
                fingerprint[:12],
            )
            rate_limiter.dequeue_failure(fingerprint)
            summary.add_result("", fingerprint[:12], "dequeued-not-found")
            processed += 1
            continue

        # Skip if already has an open PR
        if failure_store.has_open_pr(fingerprint):
            logger.info(
                "Queued fingerprint %s already has an open PR, dequeuing.",
                fingerprint[:12],
            )
            rate_limiter.dequeue_failure(fingerprint)
            summary.add_result("", entry.failure_identifier, "dequeued-has-open-pr")
            processed += 1
            continue

        if not entry.queued_pr_payload:
            logger.warning(
                "Queued fingerprint %s has no persisted PR payload, dequeuing.",
                fingerprint[:12],
            )
            failure_store.clear_queued_pr(fingerprint)
            rate_limiter.dequeue_failure(fingerprint)
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
            pr_url = pr_manager.create_pr(patch, report, root_cause, target_branch)
            rate_limiter.record_pr_created()
            failure_store.clear_queued_pr(fingerprint)
            rate_limiter.dequeue_failure(fingerprint)
            summary.add_result(
                report.job_name, entry.failure_identifier, "pr-created",
            )
            logger.info(
                "Created queued PR for fingerprint %s: %s",
                fingerprint[:12], pr_url,
            )
        except (RuntimeError, ValueError) as exc:
            failure_store.clear_queued_pr(fingerprint)
            rate_limiter.dequeue_failure(fingerprint)
            summary.add_result(
                report.job_name, entry.failure_identifier, "pr-creation-failed",
                error=str(exc),
            )
        processed += 1

    # Save state
    failure_store.save()
    rate_limiter.save()

    # Emit workflow summary
    summary.write()

    logger.info("Reconciliation complete: %d failure(s) processed.", processed)
    return processed


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="CI Failure Bot Pipeline")
    parser.add_argument("--repo", required=True, help="Repository full name (owner/repo)")
    parser.add_argument("--run-id", type=int, default=None, help="Workflow run ID (required for analyze mode)")
    parser.add_argument("--config", default=".github/ci-failure-bot.yml", help="Config file path")
    parser.add_argument("--token", required=True, help="GitHub token")
    parser.add_argument("--state-token", default=None, help="GitHub token for bot-state storage")
    parser.add_argument("--state-repo", default=None, help="Repository full name used for bot-state persistence")
    parser.add_argument("--aws-region", default=None, help="AWS region for Bedrock client")
    parser.add_argument("--mode", default="analyze", choices=["analyze", "reconcile"],
                        help="Pipeline mode: 'analyze' for normal failure processing, 'reconcile' for draining queued failures")
    parser.add_argument(
        "--queue-only",
        action="store_true",
        help="Analyze and validate fixes, but queue them for approval instead of opening PRs.",
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

"""Data models for the CI Failure Bot and PR reviewer pipelines."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass


@dataclass
class WorkflowRun:
    """Represents a GitHub Actions workflow run."""
    id: int
    name: str
    event: str  # "push", "pull_request", etc.
    head_sha: str
    head_branch: str
    head_repository: str  # e.g., "valkey-io/valkey"
    is_fork: bool
    conclusion: str  # "failure", "success", etc.
    workflow_file: str  # e.g., "ci.yml"


@dataclass
class FailedJob:
    """Represents a single failed job within a workflow run."""
    id: int
    name: str
    conclusion: str
    step_name: str | None
    matrix_params: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedFailure:
    """Structured failure information extracted from a log."""
    failure_identifier: str  # test name or stable build-scoped identifier
    test_name: str | None
    file_path: str
    error_message: str
    assertion_details: str | None
    line_number: int | None
    stack_trace: str | None
    parser_type: str  # "gtest", "tcl", "build", "sentinel", "cluster", "module"


@dataclass
class FailureReport:
    """Complete report for a single job failure."""
    workflow_name: str
    job_name: str
    matrix_params: dict[str, str]
    commit_sha: str
    failure_source: str  # "trusted" or "untrusted-fork"
    parsed_failures: list[ParsedFailure] = field(default_factory=list)
    raw_log_excerpt: str | None = None  # last 200 lines if unparseable
    is_unparseable: bool = False
    workflow_file: str = ""
    repo_full_name: str = ""
    workflow_run_id: int | None = None
    target_branch: str = ""


@dataclass
class RootCauseReport:
    """Result of Bedrock-powered root cause analysis."""
    description: str
    files_to_change: list[str]
    confidence: str  # "high", "medium", "low"
    rationale: str
    is_flaky: bool
    flakiness_indicators: list[str] | None = None
    failure_streak: int = 0
    total_failure_observations: int = 0
    last_known_good_sha: str | None = None
    first_bad_sha: str | None = None


@dataclass
class FailureObservation:
    """One pass/fail observation for a stable failure identity."""

    outcome: str  # "pass" or "fail"
    observed_at: str
    commit_sha: str
    workflow_run_id: int | None
    workflow_name: str
    workflow_file: str
    job_name: str
    matrix_params: dict[str, str]
    failure_identifier: str
    test_name: str | None = None
    error_signature: str = ""
    file_path: str = ""
    fingerprint: str | None = None


@dataclass
class FailureHistoryEntry:
    """Observation timeline for a stable workflow/job/test identity."""

    key: str
    workflow_file: str
    job_name: str
    matrix_params: dict[str, str]
    failure_identifier: str
    test_name: str | None
    observations: list[FailureObservation] = field(default_factory=list)


@dataclass
class FailureHistorySummary:
    """Derived summary for recent pass/fail observations."""

    key: str
    total_observations: int
    failure_count: int
    pass_count: int
    consecutive_failures: int
    last_outcome: str
    latest_failure_sha: str | None
    last_known_good_sha: str | None
    first_bad_sha: str | None


@dataclass
class ValidationResult:
    """Result of running validation against a proposed fix."""
    passed: bool
    output: str  # build/test output on failure


@dataclass
class FailureStoreEntry:
    """A single entry in the failure deduplication store."""
    fingerprint: str
    failure_identifier: str
    test_name: str | None
    error_signature: str
    file_path: str
    pr_url: str | None
    status: str  # "open", "merged", "abandoned", "processing"
    created_at: str
    updated_at: str
    queued_pr_payload: dict | None = None


@dataclass
class GithubEvent:
    """Normalized GitHub event payload used by the PR reviewer."""

    event_name: str
    repo: str
    actor: str
    pr_number: int | None
    comment_id: int | None
    body: str | None
    is_review_comment: bool = False
    comment_path: str | None = None
    comment_line: int | None = None
    in_reply_to_id: int | None = None


@dataclass
class ChangedFile:
    """A changed file in a pull request."""

    path: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    contents: str | None
    is_binary: bool


@dataclass
class PullRequestContext:
    """Context fetched for a pull request review."""

    repo: str
    number: int
    title: str
    body: str
    base_sha: str
    head_sha: str
    author: str
    files: list[ChangedFile]


@dataclass
class SummaryResult:
    """LLM-generated pull request summary."""

    walkthrough: str
    file_groups_markdown: str
    release_notes: str | None


@dataclass
class ReviewFinding:
    """A defect-oriented review finding on a PR."""

    path: str
    line: int | None
    body: str
    severity: str


@dataclass
class ReviewThread:
    """Conversation context for review-thread or PR-comment chat."""

    comment_id: int
    path: str | None
    line: int | None
    conversation: list[str]
    reply_to_bot: bool = False


@dataclass
class DiffScope:
    """Subset of PR files under detailed review."""

    base_sha: str
    head_sha: str
    files: list[ChangedFile]
    incremental: bool


@dataclass
class ReviewState:
    """Persisted reviewer state for incremental review."""

    repo: str
    pr_number: int
    last_reviewed_head_sha: str | None
    summary_comment_id: int | None
    review_comment_ids: list[int]
    updated_at: str


@dataclass
class FuzzerSignal:
    """One normal or anomalous signal extracted from a fuzzer run."""

    title: str
    severity: str
    evidence: str


@dataclass
class FuzzerRunContext:
    """Normalized input context for a single analyzed fuzzer workflow run."""

    repo: str
    workflow_file: str
    run_id: int
    run_url: str
    conclusion: str
    head_sha: str
    scenario_id: str | None = None
    seed: str | None = None
    artifact_names: list[str] = field(default_factory=list)
    manifest: dict[str, object] | None = None
    results: dict[str, object] | None = None
    scenario_yaml: str | None = None
    structured_logs: dict[str, dict[str, object]] = field(default_factory=dict)
    node_logs: dict[str, str] = field(default_factory=dict)
    raw_job_log: str | None = None
    raw_log_fallback_used: bool = False


@dataclass
class FuzzerRunAnalysis:
    """Structured summary of a fuzzer workflow run."""

    repo: str
    workflow_file: str
    run_id: int
    run_url: str
    conclusion: str
    head_sha: str
    scenario_id: str | None
    seed: str | None
    overall_status: str  # "normal", "warning", or "anomalous"
    summary: str
    anomalies: list[FuzzerSignal] = field(default_factory=list)
    normal_signals: list[str] = field(default_factory=list)
    reproduction_hint: str | None = None
    raw_log_fallback_used: bool = False


def failure_report_to_dict(report: FailureReport) -> dict:
    """Serialize a failure report for persistence."""
    return asdict(report)


def failure_report_from_dict(data: dict) -> FailureReport:
    """Deserialize a persisted failure report."""
    parsed_failures = [
        ParsedFailure(**raw_failure)
        for raw_failure in data.get("parsed_failures", [])
    ]
    return FailureReport(
        workflow_name=str(data.get("workflow_name", "")),
        job_name=str(data.get("job_name", "")),
        matrix_params=dict(data.get("matrix_params", {})),
        commit_sha=str(data.get("commit_sha", "")),
        failure_source=str(data.get("failure_source", "")),
        parsed_failures=parsed_failures,
        raw_log_excerpt=data.get("raw_log_excerpt"),
        is_unparseable=bool(data.get("is_unparseable", False)),
        workflow_file=str(data.get("workflow_file", "")),
        repo_full_name=str(data.get("repo_full_name", "")),
        workflow_run_id=data.get("workflow_run_id"),
        target_branch=str(data.get("target_branch", "")),
    )


def root_cause_report_to_dict(report: RootCauseReport) -> dict:
    """Serialize a root cause report for persistence."""
    return asdict(report)


def root_cause_report_from_dict(data: dict) -> RootCauseReport:
    """Deserialize a persisted root cause report."""
    return RootCauseReport(
        description=str(data.get("description", "")),
        files_to_change=list(data.get("files_to_change", [])),
        confidence=str(data.get("confidence", "low")),
        rationale=str(data.get("rationale", "")),
        is_flaky=bool(data.get("is_flaky", False)),
        flakiness_indicators=data.get("flakiness_indicators"),
        failure_streak=int(data.get("failure_streak", 0)),
        total_failure_observations=int(data.get("total_failure_observations", 0)),
        last_known_good_sha=data.get("last_known_good_sha"),
        first_bad_sha=data.get("first_bad_sha"),
    )


def review_state_to_dict(state: ReviewState) -> dict:
    """Serialize a review state for persistence."""
    return asdict(state)


def review_state_from_dict(data: dict) -> ReviewState:
    """Deserialize a persisted review state."""
    return ReviewState(
        repo=str(data.get("repo", "")),
        pr_number=int(data.get("pr_number", 0)),
        last_reviewed_head_sha=data.get("last_reviewed_head_sha"),
        summary_comment_id=data.get("summary_comment_id"),
        review_comment_ids=list(data.get("review_comment_ids", [])),
        updated_at=str(data.get("updated_at", "")),
    )


def fuzzer_run_analysis_to_dict(analysis: FuzzerRunAnalysis) -> dict:
    """Serialize a fuzzer run analysis."""
    if not is_dataclass(analysis):
        return {
            "repo": getattr(analysis, "repo", ""),
            "workflow_file": getattr(analysis, "workflow_file", ""),
            "run_id": getattr(analysis, "run_id", 0),
            "run_url": getattr(analysis, "run_url", ""),
            "conclusion": getattr(analysis, "conclusion", ""),
            "head_sha": getattr(analysis, "head_sha", ""),
            "scenario_id": getattr(analysis, "scenario_id", None),
            "seed": getattr(analysis, "seed", None),
            "overall_status": getattr(analysis, "overall_status", "normal"),
            "summary": getattr(analysis, "summary", ""),
            "anomalies": list(getattr(analysis, "anomalies", [])),
            "normal_signals": list(getattr(analysis, "normal_signals", [])),
            "reproduction_hint": getattr(analysis, "reproduction_hint", None),
            "raw_log_fallback_used": bool(
                getattr(analysis, "raw_log_fallback_used", False)
            ),
        }
    return asdict(analysis)

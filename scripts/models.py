"""Data models for the CI Failure Bot pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


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
    )

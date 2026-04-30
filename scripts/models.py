"""Data models for the CI Failure Agent and PR reviewer pipelines."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field, is_dataclass

__all__ = [
    "WorkflowRun",
    "FailedJob",
    "ParsedFailure",
    "FailureReport",
    "RootCauseReport",
    "FailureObservation",
    "FailureHistoryEntry",
    "FailureHistorySummary",
    "ValidationResult",
    "FlakyCampaignAttempt",
    "FlakyCampaignState",
    "FailureStoreEntry",
    "GithubEvent",
    "ChangedFile",
    "ExistingReviewComment",
    "PullRequestCommit",
    "PullRequestContext",
    "SummaryResult",
    "ReviewFinding",
    "ReviewThread",
    "DiffScope",
    "ReviewState",
    "FuzzerSignal",
    "FuzzerRunContext",
    "FuzzerRunAnalysis",
    "failure_report_to_dict",
    "failure_report_from_dict",
    "root_cause_report_to_dict",
    "root_cause_report_from_dict",
    "flaky_campaign_state_to_dict",
    "flaky_campaign_state_from_dict",
    "review_state_to_dict",
    "review_state_from_dict",
    "fuzzer_run_analysis_to_dict",
    "RejectionReason",
    "LogExcerpt",
    "InspectedFile",
    "CommitInfo",
    "EvidencePack",
    "RootCauseHypothesis",
    "RejectedHypothesis",
    "CriticVerdict",
    "RootCauseResult",
    "FixCandidate",
    "ValidatedCandidate",
    "RejectedCandidate",
    "TournamentResult",
    "RubricCheck",
    "RubricVerdict",
]


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
    incident_key: str | None = None


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
    strategy: str = "local"  # "local" or "ci-rerun"
    passed_runs: int = 0
    attempted_runs: int = 0


@dataclass
class FlakyCampaignAttempt:
    """One persistent experiment attempt for a flaky failure campaign."""

    attempt_number: int
    created_at: str
    patch: str
    summary: str
    strategy: str
    validation_output: str
    passed: bool
    passed_runs: int = 0
    attempted_runs: int = 0


@dataclass
class FlakyCampaignState:
    """Persistent state for long-running flaky-failure remediation."""

    fingerprint: str
    history_key: str
    failure_identifier: str
    workflow_file: str
    job_name: str
    matrix_params: dict[str, str]
    repo_full_name: str
    branch: str
    status: str  # "active", "validated", "queued", "pr-created", "abandoned"
    created_at: str
    updated_at: str
    root_cause: dict | None = None
    current_patch: str | None = None
    best_validation_output: str = ""
    last_validation_output: str = ""
    last_strategy: str = ""
    consecutive_full_passes: int = 0
    total_attempts: int = 0
    attempts: list[FlakyCampaignAttempt] = field(default_factory=list)
    failed_hypotheses: list[str] = field(default_factory=list)
    queued_pr_payload: dict | None = None
    proof_status: str = ""
    proof_summary: str = ""
    proof_url: str = ""
    proof_required_runs: int = 0
    proof_passed_runs: int = 0
    proof_attempted_runs: int = 0
    proof_started_at: str = ""
    proof_updated_at: str = ""
    landing_status: str = ""
    landing_summary: str = ""
    landing_url: str = ""
    landing_repo: str = ""
    landing_updated_at: str = ""


@dataclass
class FailureStoreEntry:
    """A single entry in the failure deduplication store."""
    fingerprint: str
    failure_identifier: str
    test_name: str | None
    incident_key: str
    error_signature: str
    file_path: str
    pr_url: str | None
    status: str  # "open", "merged", "abandoned", "processing"
    created_at: str
    updated_at: str
    queued_pr_payload: dict | None = None
    campaign_status: str | None = None
    incident_observations: list[FailureObservation] = field(default_factory=list)
    evidence_pack: dict | None = None
    rejection_reason: str | None = None


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
class ExistingReviewComment:
    """An existing review comment already present on the pull request."""

    path: str
    line: int | None
    author: str
    body: str
    in_reply_to_id: int | None = None


@dataclass
class PullRequestCommit:
    """A commit in a pull request."""

    sha: str
    message: str


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
    review_comments: list[ExistingReviewComment] = field(default_factory=list)
    commits: list[PullRequestCommit] = field(default_factory=list)
    base_ref: str = ""
    head_ref: str = ""
    labels: list[str] = field(default_factory=list)


@dataclass
class SummaryResult:
    """LLM-generated pull request summary."""

    walkthrough: str
    file_groups_markdown: str
    release_notes: str | None
    short_summary: str = ""


@dataclass
class ReviewFinding:
    """A defect-oriented review finding on a PR."""

    path: str
    line: int | None
    body: str
    severity: str
    title: str = ""
    confidence: str = "medium"
    trigger: str = ""
    impact: str = ""
    supporting_paths: list[str] = field(default_factory=list)
    verification_notes: str = ""


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
    root_cause_category: str | None = None
    raw_log_fallback_used: bool = False
    triage_verdict: str = "needs-human-triage"
    suggested_labels: list[str] = field(default_factory=list)


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


def flaky_campaign_state_to_dict(state: FlakyCampaignState) -> dict:
    """Serialize a flaky campaign state for persistence."""
    return asdict(state)


def flaky_campaign_state_from_dict(data: dict) -> FlakyCampaignState:
    """Deserialize a persisted flaky campaign state."""
    attempts = [
        FlakyCampaignAttempt(**raw_attempt)
        for raw_attempt in data.get("attempts", [])
        if isinstance(raw_attempt, dict)
    ]
    return FlakyCampaignState(
        fingerprint=str(data.get("fingerprint", "")),
        history_key=str(data.get("history_key", "")),
        failure_identifier=str(data.get("failure_identifier", "")),
        workflow_file=str(data.get("workflow_file", "")),
        job_name=str(data.get("job_name", "")),
        matrix_params=dict(data.get("matrix_params", {})),
        repo_full_name=str(data.get("repo_full_name", "")),
        branch=str(data.get("branch", "")),
        status=str(data.get("status", "active")),
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
        root_cause=data.get("root_cause") if isinstance(data.get("root_cause"), dict) else None,
        current_patch=data.get("current_patch"),
        best_validation_output=str(data.get("best_validation_output", "")),
        last_validation_output=str(data.get("last_validation_output", "")),
        last_strategy=str(data.get("last_strategy", "")),
        consecutive_full_passes=int(data.get("consecutive_full_passes", 0)),
        total_attempts=int(data.get("total_attempts", 0)),
        attempts=attempts,
        failed_hypotheses=[
            str(item)
            for item in data.get("failed_hypotheses", [])
            if isinstance(item, str)
        ],
        queued_pr_payload=data.get("queued_pr_payload")
        if isinstance(data.get("queued_pr_payload"), dict)
        else None,
        proof_status=str(data.get("proof_status", "")),
        proof_summary=str(data.get("proof_summary", "")),
        proof_url=str(data.get("proof_url", "")),
        proof_required_runs=int(data.get("proof_required_runs", 0)),
        proof_passed_runs=int(data.get("proof_passed_runs", 0)),
        proof_attempted_runs=int(data.get("proof_attempted_runs", 0)),
        proof_started_at=str(data.get("proof_started_at", "")),
        proof_updated_at=str(data.get("proof_updated_at", "")),
        landing_status=str(data.get("landing_status", "")),
        landing_summary=str(data.get("landing_summary", "")),
        landing_url=str(data.get("landing_url", "")),
        landing_repo=str(data.get("landing_repo", "")),
        landing_updated_at=str(data.get("landing_updated_at", "")),
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
            "root_cause_category": getattr(analysis, "root_cause_category", None),
            "raw_log_fallback_used": bool(
                getattr(analysis, "raw_log_fallback_used", False)
            ),
            "triage_verdict": getattr(
                analysis, "triage_verdict", "needs-human-triage"
            ),
            "suggested_labels": list(getattr(analysis, "suggested_labels", [])),
        }
    return asdict(analysis)


# ---------------------------------------------------------------------------
# Evidence-First AI Pipeline stage contracts
# ---------------------------------------------------------------------------


class RejectionReason(enum.Enum):
    """Why a failure was routed to needs-human-follow-up."""

    THIN_EVIDENCE = "thin_evidence"
    LOW_CONFIDENCE_ROOT_CAUSE = "low_confidence_root_cause"
    CRITIC_REJECTED = "critic_rejected"
    TOURNAMENT_EMPTY = "tournament_empty"
    VALIDATION_FAILED = "validation_failed"
    RUBRIC_FAILED = "rubric_failed"


@dataclass
class LogExcerpt:
    """A log excerpt around a failure point."""

    source: str  # e.g. "job-log", "test-output"
    content: str
    line_start: int | None = None
    line_end: int | None = None

    def validate(self) -> None:
        if not self.content:
            raise ValueError("LogExcerpt.content must not be empty")


@dataclass
class InspectedFile:
    """A source or test file inspected during evidence gathering."""

    path: str
    reason: str  # why this file was inspected
    excerpt: str | None = None

    def validate(self) -> None:
        if not self.path:
            raise ValueError("InspectedFile.path must not be empty")


@dataclass
class CommitInfo:
    """Recent commit context."""

    sha: str
    message: str
    author: str
    files_changed: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.sha:
            raise ValueError("CommitInfo.sha must not be empty")


@dataclass
class EvidencePack:
    """Canonical evidence object built before any AI stage runs."""

    failure_id: str
    run_id: int | None
    job_ids: list[str]
    workflow: str
    parsed_failures: list[ParsedFailure]
    log_excerpts: list[LogExcerpt]
    source_files_inspected: list[InspectedFile]
    test_files_inspected: list[InspectedFile]
    valkey_guidance_used: list[str]
    recent_commits: list[CommitInfo]
    linked_urls: list[str]
    unknowns: list[str]
    built_at: str

    def validate(self) -> None:
        if not self.failure_id:
            raise ValueError("EvidencePack.failure_id must not be empty")
        if not self.workflow:
            raise ValueError("EvidencePack.workflow must not be empty")
        for le in self.log_excerpts:
            le.validate()
        for sf in self.source_files_inspected:
            sf.validate()
        for tf in self.test_files_inspected:
            tf.validate()
        for ci in self.recent_commits:
            ci.validate()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> EvidencePack:
        parsed_failures = [
            ParsedFailure(**pf) for pf in data.get("parsed_failures", [])
        ]
        log_excerpts = [
            LogExcerpt(**le) for le in data.get("log_excerpts", [])
        ]
        source_files = [
            InspectedFile(**sf) for sf in data.get("source_files_inspected", [])
        ]
        test_files = [
            InspectedFile(**tf) for tf in data.get("test_files_inspected", [])
        ]
        commits = [
            CommitInfo(**ci) for ci in data.get("recent_commits", [])
        ]
        return cls(
            failure_id=str(data.get("failure_id", "")),
            run_id=data.get("run_id"),
            job_ids=list(data.get("job_ids", [])),
            workflow=str(data.get("workflow", "")),
            parsed_failures=parsed_failures,
            log_excerpts=log_excerpts,
            source_files_inspected=source_files,
            test_files_inspected=test_files,
            valkey_guidance_used=list(data.get("valkey_guidance_used", [])),
            recent_commits=commits,
            linked_urls=list(data.get("linked_urls", [])),
            unknowns=list(data.get("unknowns", [])),
            built_at=str(data.get("built_at", "")),
        )


@dataclass
class RootCauseHypothesis:
    """A single root-cause hypothesis with evidence references."""

    summary: str
    causal_chain: list[str]
    evidence_refs: list[str]
    confidence: str  # "low", "medium", "high"
    disconfirmed_alternatives: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.summary:
            raise ValueError("RootCauseHypothesis.summary must not be empty")
        if self.confidence not in ("low", "medium", "high"):
            raise ValueError(f"Invalid confidence: {self.confidence}")
        if not self.causal_chain:
            raise ValueError("RootCauseHypothesis.causal_chain must not be empty")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RootCauseHypothesis:
        return cls(
            summary=str(data.get("summary", "")),
            causal_chain=list(data.get("causal_chain", [])),
            evidence_refs=list(data.get("evidence_refs", [])),
            confidence=str(data.get("confidence", "low")),
            disconfirmed_alternatives=list(data.get("disconfirmed_alternatives", [])),
        )


@dataclass
class RejectedHypothesis:
    """A hypothesis that was rejected by the critic."""

    hypothesis: RootCauseHypothesis
    reason: str

    def to_dict(self) -> dict:
        return {"hypothesis": self.hypothesis.to_dict(), "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict) -> RejectedHypothesis:
        return cls(
            hypothesis=RootCauseHypothesis.from_dict(data.get("hypothesis", {})),
            reason=str(data.get("reason", "")),
        )


@dataclass
class CriticVerdict:
    """The critic's overall judgment."""

    deterministic_checks_passed: int
    deterministic_checks_failed: int
    model_critic_called: bool
    model_critic_rationale: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> CriticVerdict:
        return cls(
            deterministic_checks_passed=int(data.get("deterministic_checks_passed", 0)),
            deterministic_checks_failed=int(data.get("deterministic_checks_failed", 0)),
            model_critic_called=bool(data.get("model_critic_called", False)),
            model_critic_rationale=str(data.get("model_critic_rationale", "")),
        )


@dataclass
class RootCauseResult:
    """Output of the root-cause analyst + critic pipeline."""

    accepted: RootCauseHypothesis | None
    rejected: list[RejectedHypothesis]
    critic_verdict: CriticVerdict
    rejection_reason: RejectionReason | None = None

    def validate(self) -> None:
        if self.accepted is not None:
            self.accepted.validate()

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted.to_dict() if self.accepted else None,
            "rejected": [r.to_dict() for r in self.rejected],
            "critic_verdict": self.critic_verdict.to_dict(),
            "rejection_reason": self.rejection_reason.value if self.rejection_reason else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RootCauseResult:
        accepted_data = data.get("accepted")
        accepted = RootCauseHypothesis.from_dict(accepted_data) if accepted_data else None
        rejected = [RejectedHypothesis.from_dict(r) for r in data.get("rejected", [])]
        critic_verdict = CriticVerdict.from_dict(data.get("critic_verdict", {}))
        rr = data.get("rejection_reason")
        rejection_reason = RejectionReason(rr) if rr else None
        return cls(
            accepted=accepted,
            rejected=rejected,
            critic_verdict=critic_verdict,
            rejection_reason=rejection_reason,
        )


@dataclass
class FixCandidate:
    """A single fix candidate from the tournament."""

    candidate_id: str
    prompt_variant: str  # "minimal", "root_cause_deep", "defensive_guard"
    patch: str
    rationale: str
    evidence_refs: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.candidate_id:
            raise ValueError("FixCandidate.candidate_id must not be empty")
        if self.prompt_variant not in ("minimal", "root_cause_deep", "defensive_guard"):
            raise ValueError(f"Invalid prompt_variant: {self.prompt_variant}")
        if not self.patch:
            raise ValueError("FixCandidate.patch must not be empty")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> FixCandidate:
        return cls(
            candidate_id=str(data.get("candidate_id", "")),
            prompt_variant=str(data.get("prompt_variant", "minimal")),
            patch=str(data.get("patch", "")),
            rationale=str(data.get("rationale", "")),
            evidence_refs=list(data.get("evidence_refs", [])),
        )


@dataclass
class ValidatedCandidate:
    """A fix candidate with its validation result."""

    candidate: FixCandidate
    validation_result: ValidationResult

    def to_dict(self) -> dict:
        return {
            "candidate": self.candidate.to_dict(),
            "validation_result": asdict(self.validation_result),
        }

    @classmethod
    def from_dict(cls, data: dict) -> ValidatedCandidate:
        candidate = FixCandidate.from_dict(data.get("candidate", {}))
        vr_data = data.get("validation_result", {})
        validation_result = ValidationResult(
            passed=bool(vr_data.get("passed", False)),
            output=str(vr_data.get("output", "")),
            strategy=str(vr_data.get("strategy", "local")),
            passed_runs=int(vr_data.get("passed_runs", 0)),
            attempted_runs=int(vr_data.get("attempted_runs", 0)),
        )
        return cls(candidate=candidate, validation_result=validation_result)


@dataclass
class RejectedCandidate:
    """A fix candidate that was rejected during the tournament."""

    candidate: FixCandidate
    reason: str
    validation_output: str = ""

    def to_dict(self) -> dict:
        return {
            "candidate": self.candidate.to_dict(),
            "reason": self.reason,
            "validation_output": self.validation_output,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RejectedCandidate:
        return cls(
            candidate=FixCandidate.from_dict(data.get("candidate", {})),
            reason=str(data.get("reason", "")),
            validation_output=str(data.get("validation_output", "")),
        )


@dataclass
class TournamentResult:
    """Output of the fix tournament."""

    winning: ValidatedCandidate | None
    rejected: list[RejectedCandidate]
    reason_if_empty: str | None = None

    def validate(self) -> None:
        if self.winning is not None:
            self.winning.candidate.validate()

    def to_dict(self) -> dict:
        return {
            "winning": self.winning.to_dict() if self.winning else None,
            "rejected": [r.to_dict() for r in self.rejected],
            "reason_if_empty": self.reason_if_empty,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TournamentResult:
        winning_data = data.get("winning")
        winning = ValidatedCandidate.from_dict(winning_data) if winning_data else None
        rejected = [RejectedCandidate.from_dict(r) for r in data.get("rejected", [])]
        return cls(
            winning=winning,
            rejected=rejected,
            reason_if_empty=data.get("reason_if_empty"),
        )


@dataclass
class RubricCheck:
    """Result of a single rubric check."""

    name: str
    kind: str  # "deterministic" or "model"
    passed: bool
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RubricCheck:
        return cls(
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "deterministic")),
            passed=bool(data.get("passed", False)),
            detail=str(data.get("detail", "")),
        )


@dataclass
class RubricVerdict:
    """Aggregate result of all rubric checks."""

    checks: list[RubricCheck]
    overall_passed: bool
    blocking_checks: list[str]

    def validate(self) -> None:
        if not self.checks:
            raise ValueError("RubricVerdict.checks must not be empty")

    def to_dict(self) -> dict:
        return {
            "checks": [c.to_dict() for c in self.checks],
            "overall_passed": self.overall_passed,
            "blocking_checks": self.blocking_checks,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RubricVerdict:
        checks = [RubricCheck.from_dict(c) for c in data.get("checks", [])]
        return cls(
            checks=checks,
            overall_passed=bool(data.get("overall_passed", False)),
            blocking_checks=list(data.get("blocking_checks", [])),
        )

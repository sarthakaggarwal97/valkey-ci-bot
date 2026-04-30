"""Report-only acceptance harness for validating Valkey readiness."""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import boto3
import yaml  # type: ignore[import-untyped]
from botocore.config import Config as BotocoreConfig
from github import Auth, Github

from scripts.bedrock_client import BedrockClient, PromptClient
from scripts.bedrock_retriever import BedrockRetriever
from scripts.code_reviewer import CodeReviewer, ReviewCoverage
from scripts.commit_signoff import (
    CommitSigner,
    load_signer_from_env,
    require_dco_signoff_from_env,
)
from scripts.models import DiffScope, PullRequestContext, ReviewFinding, SummaryResult
from scripts.pr_context_fetcher import PRContextFetcher
from scripts.pr_review_main import (
    _filtered_context,
    _load_runtime_reviewer_config,
    _select_review_files,
)
from scripts.pr_summarizer import PRSummarizer
from scripts.valkey_repo_context import (
    augment_reviewer_config_for_valkey,
    load_valkey_repo_context,
)

logger = logging.getLogger(__name__)

_SECURITY_PATTERNS = (
    re.compile(r"\bsecurity\b", re.IGNORECASE),
    re.compile(r"\bvulnerability\b", re.IGNORECASE),
    re.compile(r"\bcve-\d{4}-\d+\b", re.IGNORECASE),
)


@dataclass
class ReviewExpectations:
    """Expected policy signals for one PR acceptance case."""

    missing_dco: bool | None = None
    needs_core_team: bool | None = None
    needs_docs: bool | None = None
    security_sensitive: bool | None = None


@dataclass
class ReviewCase:
    """One PR review case to evaluate."""

    name: str
    pr_number: int
    expectations: ReviewExpectations = field(default_factory=ReviewExpectations)


@dataclass
class CICase:
    """Manual CI replay case definition."""

    name: str
    workflow_run_id: int
    config_path: str = ".github/valkey-daily-bot.yml"
    repo: str = ""
    notes: str = ""


@dataclass
class BackportCase:
    """Manual backport replay case definition."""

    name: str
    source_pr_number: int
    target_branch: str
    config_path: str = ".github/backport-agent.yml"
    repo: str = ""
    notes: str = ""


@dataclass
class WorkflowCase:
    """Repo-local workflow contract case definition."""

    name: str
    workflow_path: str
    required_strings: list[str] = field(default_factory=list)
    forbidden_strings: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class AcceptanceManifest:
    """Top-level acceptance manifest."""

    target_repo: str = "valkey-io/valkey"
    execution_repo: str = ""
    reviewer_config_path: str = ".github/pr-review-bot.yml"
    review_cases: list[ReviewCase] = field(default_factory=list)
    ci_cases: list[CICase] = field(default_factory=list)
    backport_cases: list[BackportCase] = field(default_factory=list)
    workflow_cases: list[WorkflowCase] = field(default_factory=list)


@dataclass
class AcceptanceScorecard:
    """High-level replay/eval scorecard for rollout readiness."""

    review_cases: int
    review_passed: int
    review_failed: int
    workflow_cases: int
    workflow_passed: int
    workflow_failed: int
    ci_replay_cases: int
    backport_replay_cases: int

    @property
    def readiness(self) -> str:
        if self.review_failed == 0 and self.workflow_failed == 0:
            return "pass"
        return "needs-follow-up"


@dataclass
class ReviewPolicySignals:
    """Deterministic policy checks derived from PR metadata."""

    missing_dco_commits: list[str]
    needs_core_team: bool
    needs_docs: bool
    security_sensitive: bool
    governance_changed: bool
    changed_files: list[str]


@dataclass
class ExpectationCheck:
    """One expectation comparison result."""

    label: str
    expected: bool
    actual: bool

    @property
    def passed(self) -> bool:
        return self.expected == self.actual


@dataclass
class ReviewCaseResult:
    """Rendered result for one review case."""

    name: str
    pr_number: int
    policy: ReviewPolicySignals
    expectation_checks: list[ExpectationCheck] = field(default_factory=list)
    summary: SummaryResult | None = None
    findings: list[ReviewFinding] = field(default_factory=list)
    coverage: ReviewCoverage | None = None

    @property
    def model_followups(self) -> list[str]:
        """Return model-execution issues that should block acceptance."""
        if self.summary is None:
            return []

        followups: list[str] = []
        if not self.summary.walkthrough.strip():
            followups.append("summary-empty")
        if self.coverage is None:
            followups.append("review-coverage-missing")
        elif not self.coverage.complete:
            followups.append("review-coverage-incomplete")
        return followups

    @property
    def passed(self) -> bool:
        return (
            all(check.passed for check in self.expectation_checks)
            and not self.model_followups
        )


@dataclass
class WorkflowCaseCheck:
    """One workflow-contract assertion result."""

    label: str
    passed: bool
    detail: str


@dataclass
class WorkflowCaseResult:
    """Rendered result for one workflow contract case."""

    name: str
    workflow_path: str
    checks: list[WorkflowCaseCheck] = field(default_factory=list)
    notes: str = ""

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


def _build_scorecard(
    manifest: AcceptanceManifest,
    results: list[ReviewCaseResult],
    workflow_results: list[WorkflowCaseResult] | None = None,
) -> AcceptanceScorecard:
    """Build a rollout scorecard from acceptance results and replay cases."""
    workflow_results = workflow_results or []
    review_passed = sum(1 for result in results if result.passed)
    review_failed = len(results) - review_passed
    workflow_passed = sum(1 for result in workflow_results if result.passed)
    workflow_failed = len(workflow_results) - workflow_passed
    return AcceptanceScorecard(
        review_cases=len(results),
        review_passed=review_passed,
        review_failed=review_failed,
        workflow_cases=len(workflow_results),
        workflow_passed=workflow_passed,
        workflow_failed=workflow_failed,
        ci_replay_cases=len(manifest.ci_cases),
        backport_replay_cases=len(manifest.backport_cases),
    )


def _coerce_bool_or_none(value: Any) -> bool | None:
    """Return bool for valid YAML booleans, else None."""
    return value if isinstance(value, bool) else None


def _coerce_str(value: Any, default: str = "") -> str:
    """Return a string value or the provided default."""
    return value if isinstance(value, str) else default


def _coerce_int(value: Any, default: int = 0) -> int:
    """Return an integer value or the provided default."""
    if isinstance(value, bool):
        return default
    return value if isinstance(value, int) else default


def _coerce_str_list(value: Any) -> list[str]:
    """Return a list of strings from a YAML sequence."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _load_manifest(path: str | Path) -> AcceptanceManifest:
    """Load and validate the acceptance manifest."""
    raw = yaml.safe_load(Path(path).read_text())
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("Acceptance manifest must be a YAML mapping.")

    review_cases: list[ReviewCase] = []
    for item in raw.get("review_cases", []):
        if not isinstance(item, dict):
            raise ValueError("Each review case must be a mapping.")
        expectations_raw = item.get("expectations", {})
        if expectations_raw is None:
            expectations_raw = {}
        if not isinstance(expectations_raw, dict):
            raise ValueError("Review expectations must be a mapping.")
        review_cases.append(
            ReviewCase(
                name=_coerce_str(item.get("name"), f"pr-{_coerce_int(item.get('pr_number'))}"),
                pr_number=_coerce_int(item.get("pr_number")),
                expectations=ReviewExpectations(
                    missing_dco=_coerce_bool_or_none(expectations_raw.get("missing_dco")),
                    needs_core_team=_coerce_bool_or_none(expectations_raw.get("needs_core_team")),
                    needs_docs=_coerce_bool_or_none(expectations_raw.get("needs_docs")),
                    security_sensitive=_coerce_bool_or_none(
                        expectations_raw.get("security_sensitive")
                    ),
                ),
            )
        )

    ci_cases: list[CICase] = []
    for item in raw.get("ci_cases", []):
        if not isinstance(item, dict):
            raise ValueError("Each CI case must be a mapping.")
        ci_cases.append(
            CICase(
                name=_coerce_str(item.get("name"), f"run-{_coerce_int(item.get('workflow_run_id'))}"),
                workflow_run_id=_coerce_int(item.get("workflow_run_id")),
                config_path=_coerce_str(
                    item.get("config_path"),
                    ".github/valkey-daily-bot.yml",
                ),
                repo=_coerce_str(item.get("repo")),
                notes=_coerce_str(item.get("notes")),
            )
        )

    backport_cases: list[BackportCase] = []
    for item in raw.get("backport_cases", []):
        if not isinstance(item, dict):
            raise ValueError("Each backport case must be a mapping.")
        backport_cases.append(
            BackportCase(
                name=_coerce_str(
                    item.get("name"),
                    f"backport-{_coerce_int(item.get('source_pr_number'))}",
                ),
                source_pr_number=_coerce_int(item.get("source_pr_number")),
                target_branch=_coerce_str(item.get("target_branch")),
                config_path=_coerce_str(
                    item.get("config_path"),
                    ".github/backport-agent.yml",
                ),
                repo=_coerce_str(item.get("repo")),
                notes=_coerce_str(item.get("notes")),
            )
        )

    workflow_cases: list[WorkflowCase] = []
    for item in raw.get("workflow_cases", []):
        if not isinstance(item, dict):
            raise ValueError("Each workflow case must be a mapping.")
        workflow_cases.append(
            WorkflowCase(
                name=_coerce_str(item.get("name"), _coerce_str(item.get("workflow_path"))),
                workflow_path=_coerce_str(item.get("workflow_path")),
                required_strings=_coerce_str_list(item.get("required_strings")),
                forbidden_strings=_coerce_str_list(item.get("forbidden_strings")),
                notes=_coerce_str(item.get("notes")),
            )
        )

    manifest = AcceptanceManifest(
        target_repo=_coerce_str(raw.get("target_repo"), "valkey-io/valkey"),
        execution_repo=_coerce_str(raw.get("execution_repo")),
        reviewer_config_path=_coerce_str(
            raw.get("reviewer_config_path"),
            ".github/pr-review-bot.yml",
        ),
        review_cases=review_cases,
        ci_cases=ci_cases,
        backport_cases=backport_cases,
        workflow_cases=workflow_cases,
    )
    _validate_manifest(manifest)
    return manifest


def _validate_manifest(manifest: AcceptanceManifest) -> None:
    """Raise when the manifest is malformed."""
    if "/" not in manifest.target_repo:
        raise ValueError("target_repo must use owner/repo format.")
    if manifest.execution_repo and "/" not in manifest.execution_repo:
        raise ValueError("execution_repo must use owner/repo format when set.")
    for review_case in manifest.review_cases:
        if review_case.pr_number <= 0:
            raise ValueError(
                f"Review case {review_case.name!r} is missing a positive pr_number."
            )
    for ci_case in manifest.ci_cases:
        if ci_case.workflow_run_id <= 0:
            raise ValueError(
                f"CI case {ci_case.name!r} is missing a positive workflow_run_id."
            )
    for backport_case in manifest.backport_cases:
        if backport_case.source_pr_number <= 0:
            raise ValueError(
                "Backport case "
                f"{backport_case.name!r} is missing a positive source_pr_number."
            )
        if not backport_case.target_branch:
            raise ValueError(
                f"Backport case {backport_case.name!r} is missing target_branch."
            )
    for workflow_case in manifest.workflow_cases:
        if not workflow_case.workflow_path:
            raise ValueError(
                f"Workflow case {workflow_case.name!r} is missing workflow_path."
            )


def _has_signed_off_by(message: str) -> bool:
    """Return whether a commit message includes a DCO trailer."""
    return bool(re.search(r"^Signed-off-by:\s+.+<.+>$", message, flags=re.MULTILINE))


def _needs_core_team(paths: list[str]) -> bool:
    """Return whether the change likely needs @core-team review."""
    for path in paths:
        basename = Path(path).name
        if path == "GOVERNANCE.md":
            return True
        if basename in {"replication.c", "rdb.c", "aof.c"}:
            return True
        if basename.startswith("cluster") and basename.endswith(".c"):
            return True
    return False


def _needs_docs(paths: list[str]) -> bool:
    """Return whether the change likely needs valkey-doc follow-up."""
    for path in paths:
        if path == "valkey.conf":
            return True
        if path.startswith("src/commands/"):
            return True
    return False


def _security_sensitive(title: str, body: str, paths: list[str]) -> bool:
    """Return whether the PR appears to cover a security-sensitive fix."""
    joined = "\n".join([title, body, *paths])
    return any(pattern.search(joined) for pattern in _SECURITY_PATTERNS)


def _collect_policy_signals(gh: Github, repo_name: str, pr_number: int) -> ReviewPolicySignals:
    """Collect deterministic policy signals for one PR."""
    repo = gh.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    commits = list(pr.get_commits())
    files = list(pr.get_files())
    changed_paths = [raw_file.filename for raw_file in files]
    missing_dco = [
        commit.sha
        for commit in commits
        if not _has_signed_off_by(getattr(commit.commit, "message", "") or "")
    ]
    return ReviewPolicySignals(
        missing_dco_commits=missing_dco,
        needs_core_team=_needs_core_team(changed_paths),
        needs_docs=_needs_docs(changed_paths),
        security_sensitive=_security_sensitive(pr.title or "", pr.body or "", changed_paths),
        governance_changed="GOVERNANCE.md" in changed_paths,
        changed_files=changed_paths,
    )


def _expectation_checks(
    expectations: ReviewExpectations,
    policy: ReviewPolicySignals,
) -> list[ExpectationCheck]:
    """Compare configured expectations against deterministic policy signals."""
    checks: list[ExpectationCheck] = []
    mapping = {
        "missing_dco": bool(policy.missing_dco_commits),
        "needs_core_team": policy.needs_core_team,
        "needs_docs": policy.needs_docs,
        "security_sensitive": policy.security_sensitive,
    }
    for label, actual in mapping.items():
        expected = getattr(expectations, label)
        if expected is None:
            continue
        checks.append(ExpectationCheck(label=label, expected=expected, actual=actual))
    return checks


def _build_review_runtime(
    *,
    aws_region: str,
    config,
) -> tuple[PromptClient, BedrockRetriever | None]:
    """Create the Bedrock runtime clients for report-only review execution."""
    timeout_seconds = max(60, int(config.bedrock_timeout_ms / 1000))
    client_config = BotocoreConfig(read_timeout=timeout_seconds, connect_timeout=60)
    bedrock = BedrockClient(
        config=config,
        client=boto3.client(
            "bedrock-runtime",
            region_name=aws_region or None,
            config=client_config,
        ),
    )
    retriever = None
    if config.retrieval.enabled and any(
        [
            config.retrieval.code_knowledge_base_id,
            config.retrieval.docs_knowledge_base_id,
        ]
    ):
        retriever = BedrockRetriever(
            boto3.client(
                "bedrock-agent-runtime",
                region_name=aws_region or None,
                config=client_config,
            ),
        )
    return bedrock, retriever


def _run_review_case(
    gh: Github,
    repo_name: str,
    case: ReviewCase,
    reviewer_config_path: str,
    *,
    aws_region: str,
    run_models: bool,
) -> ReviewCaseResult:
    """Evaluate one review case in report-only mode."""
    policy = _collect_policy_signals(gh, repo_name, case.pr_number)
    checks = _expectation_checks(case.expectations, policy)
    result = ReviewCaseResult(
        name=case.name,
        pr_number=case.pr_number,
        policy=policy,
        expectation_checks=checks,
    )
    if not run_models:
        return result

    config = _load_runtime_reviewer_config(gh, repo_name, reviewer_config_path)
    fetcher = PRContextFetcher(gh, github_retries=config.github_retries)
    context = fetcher.fetch(repo_name, case.pr_number)
    valkey_context = load_valkey_repo_context(gh, repo_name, ref=context.base_sha)
    config = augment_reviewer_config_for_valkey(config, context, valkey_context)
    selected_paths = set(_select_review_files(context, config))
    review_context: PullRequestContext = _filtered_context(
        fetcher.hydrate_contents(context, selected_paths),
        selected_paths,
    )
    bedrock_client, retriever = _build_review_runtime(
        aws_region=aws_region,
        config=config,
    )
    result.summary = PRSummarizer(
        bedrock_client,
        retriever=retriever,
        retrieval_config=config.retrieval,
    ).summarize(review_context, config)
    reviewer = CodeReviewer(
        bedrock_client,
        retriever=retriever,
        retrieval_config=config.retrieval,
        github_client=gh,
    )
    diff_scope = DiffScope(
        base_sha=review_context.base_sha,
        head_sha=review_context.head_sha,
        files=review_context.files,
        incremental=False,
    )
    triaged_files = reviewer.triage_files(diff_scope.files, review_context, config)
    if not triaged_files:
        result.coverage = ReviewCoverage(
            requested_lgtm=True,
            skipped_files=[
                (changed_file.path, "approved by triage")
                for changed_file in diff_scope.files
            ],
        )
        return result

    triaged_scope = DiffScope(
        base_sha=diff_scope.base_sha,
        head_sha=diff_scope.head_sha,
        files=triaged_files,
        incremental=diff_scope.incremental,
    )
    result.findings = reviewer.review(
        review_context,
        triaged_scope,
        config,
        short_summary=result.summary.short_summary if result.summary else "",
    )
    get_coverage = getattr(reviewer, "get_last_review_coverage", None)
    if callable(get_coverage):
        coverage = get_coverage()
        if isinstance(coverage, ReviewCoverage):
            result.coverage = coverage
    return result


def _run_workflow_case(case: WorkflowCase) -> WorkflowCaseResult:
    """Evaluate one repo-local workflow contract case."""
    path = Path(case.workflow_path)
    checks: list[WorkflowCaseCheck] = []
    if not path.exists():
        checks.append(
            WorkflowCaseCheck(
                label="file-exists",
                passed=False,
                detail=f"{case.workflow_path} is missing.",
            )
        )
        return WorkflowCaseResult(
            name=case.name,
            workflow_path=case.workflow_path,
            checks=checks,
            notes=case.notes,
        )

    text = path.read_text(encoding="utf-8")
    try:
        yaml.safe_load(text)
        checks.append(
            WorkflowCaseCheck(
                label="yaml-parse",
                passed=True,
                detail="workflow parses as YAML",
            )
        )
    except yaml.YAMLError as exc:  # type: ignore[attr-defined]
        checks.append(
            WorkflowCaseCheck(
                label="yaml-parse",
                passed=False,
                detail=f"workflow YAML is invalid: {exc}",
            )
        )

    for fragment in case.required_strings:
        checks.append(
            WorkflowCaseCheck(
                label=f"contains:{fragment}",
                passed=fragment in text,
                detail=(
                    f"required fragment present: {fragment}"
                    if fragment in text
                    else f"missing required fragment: {fragment}"
                ),
            )
        )
    for fragment in case.forbidden_strings:
        checks.append(
            WorkflowCaseCheck(
                label=f"omits:{fragment}",
                passed=fragment not in text,
                detail=(
                    f"forbidden fragment absent: {fragment}"
                    if fragment not in text
                    else f"found forbidden fragment: {fragment}"
                ),
            )
        )
    return WorkflowCaseResult(
        name=case.name,
        workflow_path=case.workflow_path,
        checks=checks,
        notes=case.notes,
    )


def _render_review_case(result: ReviewCaseResult) -> str:
    """Render one review case as markdown."""
    lines = [f"### {result.name} (PR #{result.pr_number})"]
    lines.append(
        f"- Acceptance verdict: {'pass' if result.passed else 'needs follow-up'}"
    )
    lines.append(
        "- Deterministic signals: "
        f"missing_dco={bool(result.policy.missing_dco_commits)}, "
        f"needs_core_team={result.policy.needs_core_team}, "
        f"needs_docs={result.policy.needs_docs}, "
        f"security_sensitive={result.policy.security_sensitive}"
    )
    if result.policy.missing_dco_commits:
        lines.append(
            "- Commits missing DCO: "
            + ", ".join(f"`{sha[:12]}`" for sha in result.policy.missing_dco_commits)
        )
    if result.expectation_checks:
        for check in result.expectation_checks:
            status = "pass" if check.passed else "mismatch"
            lines.append(
                f"- Expectation `{check.label}`: {status} "
                f"(expected `{check.expected}`, actual `{check.actual}`)"
            )
    if result.model_followups:
        lines.append(
            "- Model follow-up: "
            + ", ".join(f"`{item}`" for item in result.model_followups)
        )
    if result.summary is not None:
        lines.append(f"- Summary: {result.summary.walkthrough or '(empty)'}")
    if result.coverage is not None:
        lines.append(
            f"- Coverage: checked {len(result.coverage.checked_files)} file(s), "
            f"unaccounted {len(result.coverage.unaccounted_files)}"
        )
    if result.findings:
        lines.append(f"- Findings: {len(result.findings)}")
        for finding in result.findings[:3]:
            lines.append(
                f"  - `{finding.path}`:{finding.line or 1} "
                f"[{finding.severity}] {finding.body.splitlines()[0]}"
            )
    elif result.summary is not None:
        lines.append("- Findings: none")
    return "\n".join(lines)


def _render_ci_command(case: CICase, repo_name: str, signer: CommitSigner) -> str:
    """Render the exact CI replay command for one case."""
    command = [
        f"CI_BOT_COMMIT_NAME='{signer.name}'" if signer.configured else "CI_BOT_COMMIT_NAME='<set-me>'",
        (
            f"CI_BOT_COMMIT_EMAIL='{signer.email}'"
            if signer.configured
            else "CI_BOT_COMMIT_EMAIL='<set-me>'"
        ),
        (
            "CI_BOT_REQUIRE_DCO_SIGNOFF='true'"
            if require_dco_signoff_from_env()
            else "CI_BOT_REQUIRE_DCO_SIGNOFF='false'"
        ),
        "python -m scripts.main",
        f"--repo {repo_name}",
        f"--run-id {case.workflow_run_id}",
        f"--config {case.config_path}",
        "--token $GITHUB_TOKEN",
        "--state-token $GITHUB_TOKEN",
        "--state-repo $STATE_REPO",
        "--aws-region ${AWS_REGION:-us-east-1}",
        "--queue-only",
    ]
    return " ".join(command)


def _render_backport_command(
    case: BackportCase,
    repo_name: str,
    signer: CommitSigner,
) -> str:
    """Render the exact backport replay command for one case."""
    command = [
        f"CI_BOT_COMMIT_NAME='{signer.name}'" if signer.configured else "CI_BOT_COMMIT_NAME='<set-me>'",
        (
            f"CI_BOT_COMMIT_EMAIL='{signer.email}'"
            if signer.configured
            else "CI_BOT_COMMIT_EMAIL='<set-me>'"
        ),
        (
            "CI_BOT_REQUIRE_DCO_SIGNOFF='true'"
            if require_dco_signoff_from_env()
            else "CI_BOT_REQUIRE_DCO_SIGNOFF='false'"
        ),
        "python -m scripts.backport_main",
        f"--repo {repo_name}",
        f"--pr-number {case.source_pr_number}",
        f"--target-branch {case.target_branch}",
        f"--config {case.config_path}",
        "--token $GITHUB_TOKEN",
        "--aws-region ${AWS_REGION:-us-east-1}",
    ]
    return " ".join(command)


def _render_report(
    manifest: AcceptanceManifest,
    results: list[ReviewCaseResult],
    workflow_results: list[WorkflowCaseResult] | None = None,
) -> str:
    """Render the full markdown report."""
    signer = load_signer_from_env()
    workflow_results = workflow_results or []
    scorecard = _build_scorecard(manifest, results, workflow_results)
    lines = [
        "# Valkey Acceptance Report",
        "",
        f"- Target repo: `{manifest.target_repo}`",
        f"- Execution repo: `{manifest.execution_repo or manifest.target_repo}`",
        f"- Reviewer config: `{manifest.reviewer_config_path}`",
        (
            "- Commit signer: "
            f"`{signer.name} <{signer.email}>`"
            if signer.configured
            else "- Commit signer: not configured"
        ),
        (
            f"- Require DCO signoff: `{require_dco_signoff_from_env()}`"
        ),
        "",
        "## Readiness Scorecard",
        "",
        f"- Verdict: `{scorecard.readiness}`",
        f"- Review cases: `{scorecard.review_passed}/{scorecard.review_cases}` passed",
        (
            "- Workflow cases: "
            f"`{scorecard.workflow_passed}/{scorecard.workflow_cases}` passed"
        ),
        f"- CI replay cases queued for manual execution: `{scorecard.ci_replay_cases}`",
        (
            "- Backport replay cases queued for manual execution: "
            f"`{scorecard.backport_replay_cases}`"
        ),
        "",
        "## Review Cases",
        "",
    ]
    if results:
        for result in results:
            lines.append(_render_review_case(result))
            lines.append("")
    else:
        lines.append("No automated review cases configured.")
        lines.append("")

    lines.append("## Workflow Cases")
    lines.append("")
    if workflow_results:
        for workflow_result in workflow_results:
            lines.append(f"### {workflow_result.name}")
            lines.append(
                f"- Workflow: `{workflow_result.workflow_path}`"
            )
            lines.append(
                f"- Acceptance verdict: {'pass' if workflow_result.passed else 'needs follow-up'}"
            )
            if workflow_result.notes:
                lines.append(f"- Notes: {workflow_result.notes}")
            for check in workflow_result.checks:
                status = "pass" if check.passed else "mismatch"
                lines.append(f"- {check.label}: {status} ({check.detail})")
            lines.append("")
    else:
        lines.append("No workflow contract cases configured.")
        lines.append("")

    lines.append("## Manual CI Replays")
    lines.append("")
    if manifest.ci_cases:
        for ci_case in manifest.ci_cases:
            repo_name = ci_case.repo or manifest.execution_repo or manifest.target_repo
            lines.append(f"### {ci_case.name}")
            lines.append(f"- Repo: `{repo_name}`")
            lines.append(f"- Run ID: `{ci_case.workflow_run_id}`")
            lines.append(
                f"- Command: `{_render_ci_command(ci_case, repo_name, signer)}`"
            )
            if ci_case.notes:
                lines.append(f"- Notes: {ci_case.notes}")
            lines.append("")
    else:
        lines.append("No manual CI cases configured.")
        lines.append("")

    lines.append("## Manual Backport Replays")
    lines.append("")
    if manifest.backport_cases:
        for backport_case in manifest.backport_cases:
            repo_name = (
                backport_case.repo
                or manifest.execution_repo
                or manifest.target_repo
            )
            lines.append(f"### {backport_case.name}")
            lines.append(f"- Repo: `{repo_name}`")
            lines.append(f"- Source PR: `#{backport_case.source_pr_number}`")
            lines.append(f"- Target branch: `{backport_case.target_branch}`")
            lines.append(
                "- Command: "
                f"`{_render_backport_command(backport_case, repo_name, signer)}`"
            )
            if backport_case.notes:
                lines.append(f"- Notes: {backport_case.notes}")
            lines.append("")
    else:
        lines.append("No manual backport cases configured.")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="Acceptance manifest YAML path.")
    parser.add_argument("--token", default="", help="GitHub token for API-backed checks.")
    parser.add_argument(
        "--aws-region",
        default="us-east-1",
        help="AWS region for Bedrock calls when --run-models is set.",
    )
    parser.add_argument(
        "--run-models",
        action="store_true",
        help="Execute Bedrock summary/review passes in addition to policy checks.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional markdown report output path. Defaults to stdout.",
    )
    parser.add_argument(
        "--json-output",
        default="",
        help="Optional structured JSON output path.",
    )
    parser.add_argument(
        "--fail-on-followup",
        action="store_true",
        help="Exit non-zero when the readiness scorecard needs follow-up.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    manifest = _load_manifest(args.manifest)
    results: list[ReviewCaseResult] = []
    workflow_results: list[WorkflowCaseResult] = []

    if manifest.review_cases:
        if not args.token:
            raise SystemExit("--token is required when review_cases are configured.")
        gh = Github(auth=Auth.Token(args.token))
        for case in manifest.review_cases:
            logger.info("Evaluating review case %s (PR #%d).", case.name, case.pr_number)
            results.append(
                _run_review_case(
                    gh,
                    manifest.target_repo,
                    case,
                    manifest.reviewer_config_path,
                    aws_region=args.aws_region,
                    run_models=args.run_models,
                )
            )

    for workflow_case in manifest.workflow_cases:
        logger.info(
            "Evaluating workflow case %s (%s).",
            workflow_case.name,
            workflow_case.workflow_path,
        )
        workflow_results.append(_run_workflow_case(workflow_case))

    scorecard = _build_scorecard(manifest, results, workflow_results)
    report = _render_report(manifest, results, workflow_results)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
    else:
        print(report)

    if args.json_output:
        payload = {
            "manifest": asdict(manifest),
            "scorecard": {
                **asdict(scorecard),
                "readiness": scorecard.readiness,
            },
            "results": [
                {
                    "name": result.name,
                    "pr_number": result.pr_number,
                    "passed": result.passed,
                    "policy": asdict(result.policy),
                    "expectation_checks": [asdict(check) for check in result.expectation_checks],
                    "model_followups": result.model_followups,
                    "summary": asdict(result.summary) if result.summary else None,
                    "findings": [asdict(finding) for finding in result.findings],
                    "coverage": asdict(result.coverage) if result.coverage else None,
                }
                for result in results
            ],
            "workflow_results": [
                {
                    "name": result.name,
                    "workflow_path": result.workflow_path,
                    "passed": result.passed,
                    "notes": result.notes,
                    "checks": [asdict(check) for check in result.checks],
                }
                for result in workflow_results
            ],
        }
        Path(args.json_output).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.fail_on_followup and scorecard.readiness != "pass":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

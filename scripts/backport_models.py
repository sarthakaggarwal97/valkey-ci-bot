"""Data models for the Backport Agent pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ConflictedFile:
    """A file with merge conflict markers after cherry-pick."""

    path: str
    content_with_markers: str
    target_branch_content: str
    source_branch_content: str


@dataclass
class ResolutionResult:
    """Outcome of LLM conflict resolution for a single file."""

    path: str
    resolved_content: str | None  # None = resolution failed
    resolution_summary: str
    tokens_used: int
    attempts: int


@dataclass
class CherryPickResult:
    """Outcome of the cherry-pick operation."""

    success: bool  # True if no conflicts
    conflicting_files: list[ConflictedFile] = field(default_factory=list)
    applied_commits: list[str] = field(default_factory=list)


@dataclass
class BackportPRContext:
    """Context about the source PR needed throughout the pipeline."""

    source_pr_number: int
    source_pr_title: str
    source_pr_body: str
    source_pr_url: str
    source_pr_diff: str
    target_branch: str
    commits: list[str]
    repo_full_name: str


@dataclass
class BackportResult:
    """Final outcome of a backport run."""

    outcome: str  # "success", "conflicts-unresolved", "duplicate", "rate-limited", "branch-missing", "pr-not-merged", "error"
    backport_pr_url: str | None = None
    commits_cherry_picked: int = 0
    files_conflicted: int = 0
    files_resolved: int = 0
    files_unresolved: int = 0
    total_tokens_used: int = 0
    error_message: str | None = None


@dataclass
class BackportConfig:
    """Configuration for the backport agent, loaded from consumer repo YAML."""

    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    max_conflict_retries: int = 2
    max_conflicting_files: int = 20
    max_prs_per_day: int = 10
    per_backport_token_budget: int = 100_000
    backport_label: str = "backport"
    llm_conflict_label: str = "llm-resolved-conflicts"

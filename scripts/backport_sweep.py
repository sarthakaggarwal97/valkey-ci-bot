"""Weekly backport sweep for GitHub Project-tracked Valkey release branches."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from github import Auth, Github

from scripts.backport_config import load_backport_config_from_repo
from scripts.backport_main import (
    _apply_resolutions,
    _BedrockConfigAdapter,
    _clone_repo,
    _resolve_commit_signer,
    _run_git,
    emit_job_summary,
)
from scripts.backport_models import BackportConfig, BackportPRContext, ResolutionResult
from scripts.backport_pr_creator import BackportPRCreator
from scripts.bedrock_client import BedrockClient
from scripts.cherry_pick import CherryPickExecutor
from scripts.config import BotConfig
from scripts.conflict_resolver import ConflictResolver
from scripts.github_client import retry_github_call
from scripts.publish_guard import check_publish_allowed
from scripts.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

_DEFAULT_BRANCH_FIELDS = (
    "Backport Branch",
    "Target Branch",
    "Release Branch",
    "Branch",
    "Version",
    "Release",
    "Folder",
)
_DEFAULT_RELEASE_BRANCH_PATTERN = r"\d+\.\d+"
_DEFAULT_STATUS_FIELD = "Status"
_DEFAULT_STATUS_VALUE = "To be backported"
_DEFAULT_TEST_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class ProjectBackportCandidate:
    """A merged PR from the project board that targets one release branch."""

    source_pr_number: int
    source_pr_title: str
    source_pr_url: str
    target_branch: str
    merge_commit_sha: str | None = None
    commit_shas: list[str] = field(default_factory=list)


@dataclass
class BackportTestResult:
    """Result of running configured backport validation commands."""

    status: str
    output: str = ""


@dataclass
class CandidateBackportResult:
    """Sweep outcome for one source PR on one target branch."""

    candidate: ProjectBackportCandidate
    outcome: str
    commits_cherry_picked: list[str] = field(default_factory=list)
    conflict_files: list[str] = field(default_factory=list)
    resolution_results: list[ResolutionResult] = field(default_factory=list)
    test_result: BackportTestResult = field(default_factory=lambda: BackportTestResult("not-run"))
    existing_url: str | None = None
    error_message: str | None = None


@dataclass
class BranchSweepResult:
    """Sweep outcome for one release branch."""

    target_branch: str
    candidates_found: int
    pr_url: str | None = None
    outcome: str = "skipped"
    results: list[CandidateBackportResult] = field(default_factory=list)
    error_message: str | None = None

    @property
    def applied_count(self) -> int:
        return sum(1 for result in self.results if result.outcome == "applied")


@dataclass
class SweepSummary:
    """Top-level weekly sweep result."""

    release_branches: list[str]
    branch_results: list[BranchSweepResult]


class GitHubGraphQLClient:
    """Small GraphQL client for GitHub Project v2 reads."""

    def __init__(self, token: str) -> None:
        self._token = token

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode()
        request = urllib.request.Request(
            "https://api.github.com/graphql",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github+json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub GraphQL request failed: {exc.code} {details}") from exc

        data = json.loads(body)
        errors = data.get("errors")
        if errors:
            messages = "; ".join(str(error.get("message", error)) for error in errors)
            raise RuntimeError(f"GitHub GraphQL returned errors: {messages}")
        return data.get("data", {})


class ProjectBackportDiscovery:
    """Discover merged PRs in the configured Project v2 backport column."""

    def __init__(
        self,
        graphql_client: GitHubGraphQLClient,
        *,
        project_owner: str,
        project_number: int,
        project_owner_type: str = "organization",
        status_field: str = _DEFAULT_STATUS_FIELD,
        status_value: str = _DEFAULT_STATUS_VALUE,
        branch_fields: list[str] | None = None,
    ) -> None:
        self._graphql = graphql_client
        self._project_owner = project_owner
        self._project_number = project_number
        self._project_owner_type = project_owner_type
        self._status_field = status_field
        self._status_value = status_value
        self._branch_fields = branch_fields or list(_DEFAULT_BRANCH_FIELDS)

    def discover(self, release_branches: list[str]) -> dict[str, list[ProjectBackportCandidate]]:
        """Return project candidates grouped by target release branch."""
        by_branch: dict[str, list[ProjectBackportCandidate]] = {
            branch: [] for branch in release_branches
        }
        for item in self._iter_project_items():
            candidate = self._candidate_from_item(item, release_branches)
            if candidate is None:
                continue
            by_branch.setdefault(candidate.target_branch, []).append(candidate)
        return by_branch

    def _iter_project_items(self) -> list[dict[str, Any]]:
        owner_field = "user" if self._project_owner_type == "user" else "organization"
        query = _project_items_query(owner_field)
        cursor = None
        items: list[dict[str, Any]] = []
        while True:
            data = self._graphql.execute(
                query,
                {
                    "owner": self._project_owner,
                    "number": self._project_number,
                    "cursor": cursor,
                },
            )
            owner = data.get(owner_field) or {}
            project = owner.get("projectV2")
            if not project:
                raise RuntimeError(
                    f"Project {self._project_owner}/{self._project_number} was not found."
                )
            page = project.get("items") or {}
            items.extend(page.get("nodes") or [])
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return items
            cursor = page_info.get("endCursor")

    def _candidate_from_item(
        self,
        item: dict[str, Any],
        release_branches: list[str],
    ) -> ProjectBackportCandidate | None:
        content = item.get("content") or {}
        if content.get("__typename") != "PullRequest":
            return None
        if not bool(content.get("merged")):
            return None

        fields = _extract_field_values(item)
        if not _field_has_value(fields, self._status_field, self._status_value):
            return None

        target_branch = _matching_release_branch(
            fields,
            self._branch_fields,
            release_branches,
        )
        if target_branch is None:
            return None

        commits = [
            node.get("commit", {}).get("oid", "")
            for node in (content.get("commits", {}).get("nodes") or [])
        ]
        commits = [sha for sha in commits if sha]
        merge_commit = content.get("mergeCommit") or {}
        return ProjectBackportCandidate(
            source_pr_number=int(content["number"]),
            source_pr_title=str(content.get("title") or ""),
            source_pr_url=str(content.get("url") or ""),
            target_branch=target_branch,
            merge_commit_sha=merge_commit.get("oid"),
            commit_shas=commits,
        )


def discover_release_branches(repo: object, release_branch_pattern: str) -> list[str]:
    """Return Valkey release branches matching *release_branch_pattern*."""
    pattern = re.compile(release_branch_pattern)
    branches = [
        branch.name
        for branch in retry_github_call(
            lambda: list(repo.get_branches()),
            retries=3,
            description="list repository branches",
        )
        if pattern.fullmatch(branch.name or "")
    ]
    return sorted(branches, key=_release_branch_sort_key, reverse=True)


def run_backport_sweep(
    *,
    repo_full_name: str,
    config: BackportConfig,
    github_token: str,
    aws_region: str,
    project_owner: str,
    project_number: int,
    project_owner_type: str = "organization",
    status_field: str = _DEFAULT_STATUS_FIELD,
    status_value: str = _DEFAULT_STATUS_VALUE,
    branch_fields: list[str] | None = None,
    release_branch_pattern: str = _DEFAULT_RELEASE_BRANCH_PATTERN,
    test_commands: list[str] | None = None,
    test_timeout_seconds: int = _DEFAULT_TEST_TIMEOUT_SECONDS,
    push_repo: str | None = None,
) -> SweepSummary:
    """Run the weekly project-driven backport sweep."""
    gh = Github(auth=Auth.Token(github_token))
    repo = retry_github_call(
        lambda: gh.get_repo(repo_full_name),
        retries=3,
        description=f"get repo {repo_full_name}",
    )
    release_branches = discover_release_branches(repo, release_branch_pattern)
    discovery = ProjectBackportDiscovery(
        GitHubGraphQLClient(github_token),
        project_owner=project_owner,
        project_number=project_number,
        project_owner_type=project_owner_type,
        status_field=status_field,
        status_value=status_value,
        branch_fields=branch_fields,
    )
    candidates_by_branch = discovery.discover(release_branches)

    bot_config = BotConfig(max_prs_per_day=config.max_prs_per_day)
    rate_limiter = RateLimiter(
        bot_config,
        None,
        "",
        state_github_client=gh,
        state_repo_full_name=repo_full_name,
    )
    rate_limiter.load()
    pr_target_repo = push_repo or repo_full_name
    pr_creator = BackportPRCreator(
        gh,
        pr_target_repo,
        backport_label=config.backport_label,
        llm_conflict_label=config.llm_conflict_label,
    )

    branch_results: list[BranchSweepResult] = []
    try:
        signer, require_dco_signoff = _resolve_commit_signer()
        for target_branch in release_branches:
            candidates = candidates_by_branch.get(target_branch, [])
            if not candidates:
                branch_results.append(
                    BranchSweepResult(
                        target_branch=target_branch,
                        candidates_found=0,
                        outcome="no-candidates",
                    )
                )
                continue
            branch_results.append(
                _process_release_branch(
                    gh=gh,
                    repo=repo,
                    repo_full_name=repo_full_name,
                    pr_target_repo=pr_target_repo,
                    config=config,
                    github_token=github_token,
                    aws_region=aws_region,
                    target_branch=target_branch,
                    candidates=candidates,
                    pr_creator=pr_creator,
                    rate_limiter=rate_limiter,
                    signer=signer,
                    require_dco_signoff=require_dco_signoff,
                    test_commands=test_commands or [],
                    test_timeout_seconds=test_timeout_seconds,
                    push_repo=push_repo,
                )
            )
    finally:
        rate_limiter.save()

    summary = SweepSummary(release_branches=release_branches, branch_results=branch_results)
    emit_job_summary(build_job_summary(summary))
    return summary


def build_job_summary(summary: SweepSummary) -> str:
    """Render a compact GitHub Actions summary for the sweep."""
    lines = ["## Weekly Backport Sweep", ""]
    for branch_result in summary.branch_results:
        detail = (
            f"- `{branch_result.target_branch}`: {branch_result.outcome}; "
            f"{branch_result.applied_count}/{branch_result.candidates_found} applied"
        )
        if branch_result.pr_url:
            detail += f"; PR: {branch_result.pr_url}"
        if branch_result.error_message:
            detail += f"; {branch_result.error_message}"
        lines.append(detail)
    return "\n".join(lines)


def _process_release_branch(
    *,
    gh: Github,
    repo: object,
    repo_full_name: str,
    pr_target_repo: str,
    config: BackportConfig,
    github_token: str,
    aws_region: str,
    target_branch: str,
    candidates: list[ProjectBackportCandidate],
    pr_creator: BackportPRCreator,
    rate_limiter: RateLimiter,
    signer: object,
    require_dco_signoff: bool,
    test_commands: list[str],
    test_timeout_seconds: int,
    push_repo: str | None,
) -> BranchSweepResult:
    result = BranchSweepResult(
        target_branch=target_branch,
        candidates_found=len(candidates),
    )
    eligible: list[ProjectBackportCandidate] = []
    for candidate in candidates:
        existing_url = pr_creator.find_existing_backport(
            candidate.source_pr_number,
            target_branch,
            candidate.source_pr_url,
        )
        if existing_url:
            result.results.append(
                CandidateBackportResult(
                    candidate=candidate,
                    outcome="duplicate",
                    existing_url=existing_url,
                )
            )
            continue
        eligible.append(candidate)

    if not eligible:
        result.outcome = "all-duplicates"
        return result

    branch_name = _weekly_branch_name(target_branch)
    with tempfile.TemporaryDirectory(prefix="valkey-backport-sweep-") as tmp_dir:
        git_env = _clone_repo(
            repo_full_name,
            github_token,
            tmp_dir,
            target_branch,
            signer=signer,
        )
        _run_git(tmp_dir, "checkout", "-b", branch_name)
        executor = CherryPickExecutor(tmp_dir)

        for candidate in eligible:
            result.results.append(
                _apply_candidate(
                    gh=gh,
                    repo=repo,
                    repo_full_name=repo_full_name,
                    config=config,
                    aws_region=aws_region,
                    tmp_dir=tmp_dir,
                    executor=executor,
                    branch_name=branch_name,
                    candidate=candidate,
                    signer=signer,
                    require_dco_signoff=require_dco_signoff,
                    test_commands=test_commands,
                    test_timeout_seconds=test_timeout_seconds,
                )
            )

        if result.applied_count == 0:
            result.outcome = "nothing-applied"
            return result

        if not rate_limiter.reserve_pr_creation():
            result.outcome = "rate-limited"
            result.error_message = "Daily backport PR rate limit reached."
            return result

        _push_branch(tmp_dir, branch_name, git_env, push_repo, repo_full_name)

    result.pr_url = _create_weekly_pr(
        gh,
        pr_target_repo,
        branch_name,
        target_branch,
        result.results,
        backport_label=config.backport_label,
        llm_conflict_label=config.llm_conflict_label,
    )
    rate_limiter.record_pr_created()
    result.outcome = "success"
    return result


def _apply_candidate(
    *,
    gh: Github,
    repo: object,
    repo_full_name: str,
    config: BackportConfig,
    aws_region: str,
    tmp_dir: str,
    executor: CherryPickExecutor,
    branch_name: str,
    candidate: ProjectBackportCandidate,
    signer: object,
    require_dco_signoff: bool,
    test_commands: list[str],
    test_timeout_seconds: int,
) -> CandidateBackportResult:
    source_pr = retry_github_call(
        lambda: repo.get_pull(candidate.source_pr_number),
        retries=3,
        description=f"get PR #{candidate.source_pr_number}",
    )
    if not bool(getattr(source_pr, "merged", False)):
        return CandidateBackportResult(candidate=candidate, outcome="pr-not-merged")

    pr_context = _build_pr_context(repo_full_name, source_pr, candidate.target_branch)
    merge_commit_sha = getattr(source_pr, "merge_commit_sha", None) or candidate.merge_commit_sha
    commit_shas = pr_context.commits or candidate.commit_shas

    try:
        cherry_result = executor.execute(branch_name, merge_commit_sha, commit_shas)
    except Exception as exc:
        _abort_cherry_pick(tmp_dir)
        return CandidateBackportResult(
            candidate=candidate,
            outcome="error",
            error_message=f"Cherry-pick failed: {exc}",
        )

    resolution_results: list[ResolutionResult] = []
    if not cherry_result.success and cherry_result.conflicting_files:
        resolution_results = _resolve_candidate_conflicts(
            gh,
            repo_full_name,
            config,
            aws_region,
            merge_commit_sha or "",
            cherry_result.conflicting_files,
            pr_context,
        )
        if any(resolution.resolved_content is None for resolution in resolution_results):
            _abort_cherry_pick(tmp_dir)
            return CandidateBackportResult(
                candidate=candidate,
                outcome="conflicts-unresolved",
                commits_cherry_picked=cherry_result.applied_commits,
                conflict_files=[conflict.path for conflict in cherry_result.conflicting_files],
                resolution_results=resolution_results,
                error_message="At least one conflict could not be resolved cleanly.",
            )
        _apply_resolutions(
            tmp_dir,
            resolution_results,
            signer=signer,
            require_dco_signoff=require_dco_signoff,
        )

    test_result = _run_test_commands(
        tmp_dir,
        test_commands,
        timeout_seconds=test_timeout_seconds,
    )
    return CandidateBackportResult(
        candidate=candidate,
        outcome="applied",
        commits_cherry_picked=cherry_result.applied_commits,
        conflict_files=[conflict.path for conflict in cherry_result.conflicting_files],
        resolution_results=resolution_results,
        test_result=test_result,
    )


def _resolve_candidate_conflicts(
    gh: Github,
    repo_full_name: str,
    config: BackportConfig,
    aws_region: str,
    head_sha: str,
    conflicting_files: list[object],
    pr_context: BackportPRContext,
) -> list[ResolutionResult]:
    bedrock_adapter = _BedrockConfigAdapter(_backport_config=config)
    bedrock_client = BedrockClient(
        bedrock_adapter,
        client=boto3.client("bedrock-runtime", region_name=aws_region),
    )
    resolver = ConflictResolver(
        bedrock_client,
        config,
        github_client=gh,
        repo_full_name=repo_full_name,
        head_sha=head_sha,
    )
    return resolver.resolve_conflicts(
        conflicting_files,
        pr_context,
        token_budget=config.per_backport_token_budget,
    )


def _build_pr_context(repo_full_name: str, source_pr: object, target_branch: str) -> BackportPRContext:
    commits = [
        c.sha
        for c in retry_github_call(
            lambda: list(source_pr.get_commits()),
            retries=3,
            description=f"get commits for PR #{source_pr.number}",
        )
    ]
    diff_parts: list[str] = []
    try:
        pr_files = retry_github_call(
            lambda: list(source_pr.get_files()),
            retries=3,
            description=f"get files for PR #{source_pr.number}",
        )
        for changed_file in pr_files:
            if changed_file.patch:
                diff_parts.append(
                    f"--- a/{changed_file.filename}\n"
                    f"+++ b/{changed_file.filename}\n"
                    f"{changed_file.patch}"
                )
    except Exception as exc:
        logger.warning("Could not fetch PR diff for #%s: %s", source_pr.number, exc)

    return BackportPRContext(
        source_pr_number=int(source_pr.number),
        source_pr_title=source_pr.title or "",
        source_pr_body=source_pr.body or "",
        source_pr_url=source_pr.html_url,
        source_pr_diff="\n".join(diff_parts),
        target_branch=target_branch,
        commits=commits,
        repo_full_name=repo_full_name,
    )


def _run_test_commands(
    repo_dir: str,
    commands: list[str],
    *,
    timeout_seconds: int,
) -> BackportTestResult:
    if not commands:
        return BackportTestResult(status="skipped", output="No test commands configured.")

    output_parts: list[str] = []
    for command in commands:
        logger.info("Running backport test command: %s", command)
        try:
            # Commands come from the repository-owned workflow/config inputs.
            completed = subprocess.run(
                command,
                shell=True,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            output_parts.append(f"$ {command}\nTIMEOUT after {timeout_seconds}s")
            return BackportTestResult(status="failed", output="\n".join(output_parts))
        except OSError as exc:
            output_parts.append(f"$ {command}\nERROR: {exc}")
            return BackportTestResult(status="failed", output="\n".join(output_parts))

        combined = completed.stdout + completed.stderr
        output_parts.append(f"$ {command}\n{combined}")
        if completed.returncode != 0:
            return BackportTestResult(status="failed", output="\n".join(output_parts))

    return BackportTestResult(status="passed", output="\n".join(output_parts))


def _abort_cherry_pick(repo_dir: str) -> None:
    subprocess.run(
        ["git", "cherry-pick", "--abort"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )


def _push_branch(
    repo_dir: str,
    branch_name: str,
    git_env: dict[str, str],
    push_repo: str | None,
    repo_full_name: str,
) -> None:
    if push_repo and push_repo != repo_full_name:
        fork_url = f"https://x-access-token@github.com/{push_repo}.git"
        _run_git(repo_dir, "remote", "add", "fork", fork_url, env=git_env)
        _run_git(repo_dir, "push", "fork", branch_name, env=git_env)
    else:
        _run_git(repo_dir, "push", "origin", branch_name, env=git_env)


def _create_weekly_pr(
    gh: Github,
    repo_full_name: str,
    branch_name: str,
    target_branch: str,
    results: list[CandidateBackportResult],
    *,
    backport_label: str,
    llm_conflict_label: str,
) -> str:
    repo = retry_github_call(
        lambda: gh.get_repo(repo_full_name),
        retries=3,
        description=f"get repo {repo_full_name}",
    )
    title = f"[Backport {target_branch}] Weekly backport sweep"
    body = build_weekly_pr_body(target_branch, results)
    check_publish_allowed(
        target_repo=repo_full_name,
        action="create_pull",
        context=f"weekly backport {branch_name}->{target_branch}",
    )
    pr = retry_github_call(
        lambda: repo.create_pull(
            title=title,
            body=body,
            head=branch_name,
            base=target_branch,
        ),
        retries=3,
        description="create weekly backport PR",
    )

    labels = [backport_label]
    if any(result.resolution_results for result in results):
        labels.append(llm_conflict_label)
    retry_github_call(
        lambda: pr.add_to_labels(*labels),
        retries=3,
        description="apply weekly backport PR labels",
    )
    return pr.html_url


def build_weekly_pr_body(
    target_branch: str,
    results: list[CandidateBackportResult],
) -> str:
    """Build a PR body for a weekly release-branch backport batch."""
    applied = [result for result in results if result.outcome == "applied"]
    skipped = [result for result in results if result.outcome != "applied"]
    sections = [
        "## Backport Summary",
        (
            f"Cherry-picked {len(applied)} merged PR(s) onto `{target_branch}` "
            "from the weekly backport sweep."
        ),
        "### Cherry-Picked PRs",
        _render_applied_table(applied),
    ]
    if skipped:
        sections.extend(["### Skipped PRs", _render_skipped_table(skipped)])

    commands = sorted(
        {
            result.test_result.status
            for result in applied
            if result.test_result.status
        }
    )
    if commands:
        sections.extend(["### Test Status", ", ".join(commands)])
    return "\n\n".join(sections)


def _render_applied_table(results: list[CandidateBackportResult]) -> str:
    if not results:
        return "No PRs were cherry-picked."
    lines = [
        "| Source PR | Title | Cherry-picked commits | Conflicts | Tests |",
        "|---|---|---|---|---|",
    ]
    for result in results:
        candidate = result.candidate
        commits = "<br>".join(f"`{sha}`" for sha in result.commits_cherry_picked)
        conflicts = (
            "<br>".join(f"`{path}`" for path in result.conflict_files)
            if result.conflict_files else "none"
        )
        lines.append(
            "| "
            f"[#{candidate.source_pr_number}]({candidate.source_pr_url}) | "
            f"{_escape_table_cell(candidate.source_pr_title)} | "
            f"{commits or 'none'} | "
            f"{conflicts} | "
            f"{result.test_result.status} |"
        )
    return "\n".join(lines)


def _render_skipped_table(results: list[CandidateBackportResult]) -> str:
    lines = [
        "| Source PR | Reason | Detail |",
        "|---|---|---|",
    ]
    for result in results:
        candidate = result.candidate
        detail = result.existing_url or result.error_message or ""
        lines.append(
            "| "
            f"[#{candidate.source_pr_number}]({candidate.source_pr_url}) | "
            f"{result.outcome} | "
            f"{_escape_table_cell(detail)} |"
        )
    return "\n".join(lines)


def _project_items_query(owner_field: str) -> str:
    return f"""
query($owner: String!, $number: Int!, $cursor: String) {{
  {owner_field}(login: $owner) {{
    projectV2(number: $number) {{
      items(first: 100, after: $cursor) {{
        pageInfo {{
          hasNextPage
          endCursor
        }}
        nodes {{
          content {{
            __typename
            ... on PullRequest {{
              number
              title
              url
              merged
              mergeCommit {{
                oid
              }}
              commits(first: 100) {{
                nodes {{
                  commit {{
                    oid
                  }}
                }}
              }}
            }}
          }}
          fieldValues(first: 50) {{
            nodes {{
              __typename
              ... on ProjectV2ItemFieldTextValue {{
                text
                field {{ ... on ProjectV2FieldCommon {{ name }} }}
              }}
              ... on ProjectV2ItemFieldNumberValue {{
                number
                field {{ ... on ProjectV2FieldCommon {{ name }} }}
              }}
              ... on ProjectV2ItemFieldDateValue {{
                date
                field {{ ... on ProjectV2FieldCommon {{ name }} }}
              }}
              ... on ProjectV2ItemFieldSingleSelectValue {{
                name
                field {{ ... on ProjectV2FieldCommon {{ name }} }}
              }}
              ... on ProjectV2ItemFieldIterationValue {{
                title
                field {{ ... on ProjectV2FieldCommon {{ name }} }}
              }}
              ... on ProjectV2ItemFieldLabelValue {{
                labels(first: 20) {{
                  nodes {{ name }}
                }}
                field {{ ... on ProjectV2FieldCommon {{ name }} }}
              }}
              ... on ProjectV2ItemFieldMilestoneValue {{
                milestone {{ title }}
                field {{ ... on ProjectV2FieldCommon {{ name }} }}
              }}
              ... on ProjectV2ItemFieldRepositoryValue {{
                repository {{ name nameWithOwner }}
                field {{ ... on ProjectV2FieldCommon {{ name }} }}
              }}
              ... on ProjectV2ItemFieldUserValue {{
                users(first: 20) {{
                  nodes {{ login }}
                }}
                field {{ ... on ProjectV2FieldCommon {{ name }} }}
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


def _extract_field_values(item: dict[str, Any]) -> dict[str, list[str]]:
    values_by_field: dict[str, list[str]] = defaultdict(list)
    field_values = item.get("fieldValues") or {}
    for value in field_values.get("nodes") or []:
        field = value.get("field") or {}
        field_name = field.get("name")
        if not field_name:
            continue
        values_by_field[_normalize(field_name)].extend(_field_value_strings(value))
    return dict(values_by_field)


def _field_value_strings(value: dict[str, Any]) -> list[str]:
    typename = value.get("__typename")
    if typename == "ProjectV2ItemFieldTextValue":
        return [str(value.get("text") or "")]
    if typename == "ProjectV2ItemFieldNumberValue":
        number = value.get("number")
        return [] if number is None else [str(number)]
    if typename == "ProjectV2ItemFieldDateValue":
        return [str(value.get("date") or "")]
    if typename == "ProjectV2ItemFieldSingleSelectValue":
        return [str(value.get("name") or "")]
    if typename == "ProjectV2ItemFieldIterationValue":
        return [str(value.get("title") or "")]
    if typename == "ProjectV2ItemFieldLabelValue":
        labels = value.get("labels") or {}
        return [str(node.get("name") or "") for node in labels.get("nodes") or []]
    if typename == "ProjectV2ItemFieldMilestoneValue":
        milestone = value.get("milestone") or {}
        return [str(milestone.get("title") or "")]
    if typename == "ProjectV2ItemFieldRepositoryValue":
        repository = value.get("repository") or {}
        return [
            str(repository.get("nameWithOwner") or ""),
            str(repository.get("name") or ""),
        ]
    if typename == "ProjectV2ItemFieldUserValue":
        users = value.get("users") or {}
        return [str(node.get("login") or "") for node in users.get("nodes") or []]
    return []


def _field_has_value(fields: dict[str, list[str]], field_name: str, expected: str) -> bool:
    expected_norm = _normalize(expected)
    return any(_normalize(value) == expected_norm for value in fields.get(_normalize(field_name), []))


def _matching_release_branch(
    fields: dict[str, list[str]],
    branch_fields: list[str],
    release_branches: list[str],
) -> str | None:
    for field_name in branch_fields:
        values = fields.get(_normalize(field_name), [])
        for branch in release_branches:
            if _values_match_branch(values, branch):
                return branch
    return None


def _values_match_branch(values: list[str], branch: str) -> bool:
    branch_norm = _normalize(branch)
    for value in values:
        value_norm = _normalize(value)
        if value_norm == branch_norm or value_norm == f"backport {branch_norm}":
            return True
    return False


def _weekly_branch_name(target_branch: str) -> str:
    run_id = os.environ.get("GITHUB_RUN_ID")
    suffix = run_id or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    safe_branch = re.sub(r"[^A-Za-z0-9._-]+", "-", target_branch).strip("-")
    return f"backport/weekly-{safe_branch}-{suffix}"


def _release_branch_sort_key(name: str) -> tuple[int, ...]:
    parts = []
    for part in name.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(-1)
    return tuple(parts)


def _escape_table_cell(value: object) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return text.replace("|", "\\|").replace("\n", "<br>")


def _normalize(value: object) -> str:
    return str(value or "").strip().lower()


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the weekly Valkey backport sweep.")
    parser.add_argument("--repo", default="valkey-io/valkey", help="Repository full name.")
    parser.add_argument("--project-owner", default="valkey-io", help="Project owner login.")
    parser.add_argument("--project-owner-type", choices=["organization", "user"], default="organization")
    parser.add_argument("--project-number", type=int, required=True, help="GitHub Project v2 number.")
    parser.add_argument("--status-field", default=_DEFAULT_STATUS_FIELD)
    parser.add_argument("--status-value", default=_DEFAULT_STATUS_VALUE)
    parser.add_argument(
        "--branch-fields",
        default=",".join(_DEFAULT_BRANCH_FIELDS),
        help="Comma-separated project field names that can contain the target release branch.",
    )
    parser.add_argument("--release-branch-pattern", default=_DEFAULT_RELEASE_BRANCH_PATTERN)
    parser.add_argument("--config", default=".github/backport-agent.yml")
    parser.add_argument("--token", default="")
    parser.add_argument("--aws-region", default="us-east-1")
    parser.add_argument("--test-command", action="append", default=[])
    parser.add_argument("--test-timeout-seconds", type=int, default=_DEFAULT_TEST_TIMEOUT_SECONDS)
    parser.add_argument("--push-repo", default="")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    github_token = (
        args.token
        or os.environ.get("BACKPORT_GITHUB_TOKEN", "")
        or os.environ.get("GITHUB_TOKEN", "")
    )
    if not github_token:
        parser.error(
            "GitHub token is required via --token, BACKPORT_GITHUB_TOKEN, or GITHUB_TOKEN."
        )

    gh = Github(auth=Auth.Token(github_token))
    config = load_backport_config_from_repo(gh, args.repo, args.config)
    summary = run_backport_sweep(
        repo_full_name=args.repo,
        config=config,
        github_token=github_token,
        aws_region=args.aws_region,
        project_owner=args.project_owner,
        project_number=args.project_number,
        project_owner_type=args.project_owner_type,
        status_field=args.status_field,
        status_value=args.status_value,
        branch_fields=_split_csv(args.branch_fields),
        release_branch_pattern=args.release_branch_pattern,
        test_commands=args.test_command,
        test_timeout_seconds=args.test_timeout_seconds,
        push_repo=args.push_repo or None,
    )
    logger.info("Backport sweep complete for %d release branches.", len(summary.release_branches))


if __name__ == "__main__":
    main()

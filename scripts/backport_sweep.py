"""Weekly backport sweep for GitHub Project-tracked Valkey release branches.

Discovers merged PRs marked "To be backported" on a GitHub Projects v2
board, groups them by target release branch, cherry-picks them onto the
branch, resolves conflicts with Claude Code, and opens/updates one PR
per release branch on the configured push repo.

Key design: one open PR per release branch at any time. New candidates
are cherry-picked onto the existing branch; the PR auto-updates.
"""

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
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github import Auth, Github

from scripts.backport_main import (
    _resolve_commit_signer,
    _run_git,
    emit_job_summary,
)
from scripts.backport_models import BackportPRContext
from scripts.cherry_pick import CherryPickExecutor
from scripts.claude_conflict_resolver import resolve_conflicts_with_claude
from scripts.github_client import retry_github_call
from scripts.publish_guard import check_publish_allowed

logger = logging.getLogger(__name__)

_DEFAULT_BRANCH_FIELDS = (
    "Backport Branch", "Target Branch", "Release Branch",
    "Branch", "Version", "Release", "Folder",
)
# Only sweep these release branches, even if other N.N branches exist in the repo
_SUPPORTED_RELEASE_BRANCHES = ("7.2", "8.0", "8.1", "9.0", "9.1")
_DEFAULT_RELEASE_BRANCH_PATTERN = r"\d+\.\d+"
_DEFAULT_STATUS_FIELD = "Status"
_DEFAULT_STATUS_VALUE = "To be backported"
_BRANCH_PREFIX = "agent/backport/weekly"


# ── Data classes ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProjectBackportCandidate:
    source_pr_number: int
    source_pr_title: str
    source_pr_url: str
    target_branch: str
    merge_commit_sha: str | None = None
    commit_shas: list[str] = field(default_factory=list)


@dataclass
class CandidateResult:
    source_pr_number: int
    source_pr_title: str
    outcome: str  # applied, skipped-existing, skipped-conflict, skipped-test, error
    detail: str = ""


@dataclass
class BranchSweepResult:
    target_branch: str
    candidates_found: int = 0
    results: list[CandidateResult] = field(default_factory=list)
    pr_url: str = ""


# ── GraphQL client ────────────────────────────────────────────────────

class GitHubGraphQLClient:
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
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GraphQL failed: {exc.code} {details}") from exc
        data = json.loads(body)
        if data.get("errors"):
            msgs = "; ".join(str(e.get("message", e)) for e in data["errors"])
            raise RuntimeError(f"GraphQL errors: {msgs}")
        return data.get("data", {})


# ── Projects v2 discovery ─────────────────────────────────────────────

class ProjectBackportDiscovery:
    def __init__(self, gql: GitHubGraphQLClient, *, project_owner: str,
                 project_number: int, project_owner_type: str = "organization",
                 status_field: str = _DEFAULT_STATUS_FIELD,
                 status_value: str = _DEFAULT_STATUS_VALUE,
                 branch_fields: list[str] | None = None,
                 implicit_target_branch: str | None = None) -> None:
        self._gql = gql
        self._owner = project_owner
        self._number = project_number
        self._owner_type = project_owner_type
        self._status_field = status_field
        self._status_value = status_value
        self._branch_fields = branch_fields or list(_DEFAULT_BRANCH_FIELDS)
        # If set, every candidate on this project goes to this branch
        # (used for per-release-version project boards like valkey-io/projects/14 → 8.1)
        self._implicit_target = implicit_target_branch

    def discover(self, release_branches: list[str]) -> dict[str, list[ProjectBackportCandidate]]:
        by_branch: dict[str, list[ProjectBackportCandidate]] = {b: [] for b in release_branches}
        for item in self._iter_items():
            c = self._candidate_from_item(item, release_branches)
            if c:
                by_branch.setdefault(c.target_branch, []).append(c)
        return by_branch

    def _iter_items(self) -> list[dict[str, Any]]:
        owner_field = "user" if self._owner_type == "user" else "organization"
        query = _project_items_query(owner_field)
        cursor = None
        items: list[dict[str, Any]] = []
        while True:
            data = self._gql.execute(query, {"owner": self._owner, "number": self._number, "cursor": cursor})
            project = (data.get(owner_field) or {}).get("projectV2")
            if not project:
                raise RuntimeError(f"Project {self._owner}/{self._number} not found")
            page = project.get("items") or {}
            items.extend(page.get("nodes") or [])
            pi = page.get("pageInfo") or {}
            if not pi.get("hasNextPage"):
                return items
            cursor = pi.get("endCursor")

    def _candidate_from_item(self, item: dict[str, Any], branches: list[str]) -> ProjectBackportCandidate | None:
        content = item.get("content") or {}
        if content.get("__typename") != "PullRequest" or not content.get("merged"):
            return None
        fields = _extract_field_values(item)
        if not _field_has_value(fields, self._status_field, self._status_value):
            return None
        # Determine target branch: either from project (implicit) or from a field
        if self._implicit_target:
            target = self._implicit_target
        else:
            target = _matching_release_branch(fields, self._branch_fields, branches)
            if not target:
                return None
        commits = [n.get("commit", {}).get("oid", "") for n in (content.get("commits", {}).get("nodes") or [])]
        merge_sha = (content.get("mergeCommit") or {}).get("oid")
        return ProjectBackportCandidate(
            source_pr_number=int(content["number"]),
            source_pr_title=str(content.get("title") or ""),
            source_pr_url=str(content.get("url") or ""),
            target_branch=target,
            merge_commit_sha=merge_sha,
            commit_shas=[s for s in commits if s],
        )


def discover_release_branches(repo: object, pattern: str) -> list[str]:
    regex = re.compile(pattern)
    branches = [b.name for b in retry_github_call(lambda: list(repo.get_branches()), retries=3, description="list branches")]
    matched = sorted([b for b in branches if regex.fullmatch(b) and b in _SUPPORTED_RELEASE_BRANCHES], key=_release_branch_sort_key)
    logger.info("Discovered release branches: %s", matched)
    return matched


# ── Sweep orchestrator ────────────────────────────────────────────────

def run_backport_sweep(
    *,
    repo_full_name: str,
    github_token: str,
    project_owner: str,
    project_number: int,
    project_owner_type: str = "organization",
    status_field: str = _DEFAULT_STATUS_FIELD,
    status_value: str = _DEFAULT_STATUS_VALUE,
    branch_fields: list[str] | None = None,
    push_repo: str | None = None,
    only_branch: str | None = None,
    test_commands: list[str] | None = None,
    discover_only: bool = False,
    implicit_target_branch: str | None = None,
    max_candidates: int = 0,
) -> list[BranchSweepResult]:
    gh = Github(auth=Auth.Token(github_token))
    repo = retry_github_call(lambda: gh.get_repo(repo_full_name), retries=3, description=f"get {repo_full_name}")
    release_branches = discover_release_branches(repo, _DEFAULT_RELEASE_BRANCH_PATTERN)
    if only_branch:
        release_branches = [b for b in release_branches if b == only_branch]
    if implicit_target_branch and implicit_target_branch not in release_branches:
        # User-specified target takes precedence even if not in pattern match
        release_branches = [implicit_target_branch]

    discovery = ProjectBackportDiscovery(
        GitHubGraphQLClient(github_token),
        project_owner=project_owner, project_number=project_number,
        project_owner_type=project_owner_type, status_field=status_field,
        status_value=status_value, branch_fields=branch_fields,
        implicit_target_branch=implicit_target_branch,
    )
    candidates_by_branch = discovery.discover(release_branches)

    results: list[BranchSweepResult] = []
    for branch in release_branches:
        candidates = candidates_by_branch.get(branch, [])
        if max_candidates > 0 and len(candidates) > max_candidates:
            logger.info("Branch %s: limiting from %d to %d candidates", branch, len(candidates), max_candidates)
            candidates = candidates[:max_candidates]
        logger.info("Branch %s: %d candidate(s)", branch, len(candidates))
        if discover_only:
            for c in candidates:
                logger.info("  PR #%d: %s (%s)", c.source_pr_number, c.source_pr_title, c.merge_commit_sha or "no merge sha")
            results.append(BranchSweepResult(target_branch=branch, candidates_found=len(candidates)))
            continue
        if not candidates:
            results.append(BranchSweepResult(target_branch=branch))
            continue
        results.append(_process_branch(
            gh=gh, repo=repo, repo_full_name=repo_full_name,
            github_token=github_token, target_branch=branch,
            candidates=candidates, push_repo=push_repo or repo_full_name,
            test_commands=test_commands or [],
        ))

    summary = _build_summary(results)
    emit_job_summary(summary)
    return results


def _process_branch(
    *, gh: object, repo: object, repo_full_name: str, github_token: str,
    target_branch: str, candidates: list[ProjectBackportCandidate],
    push_repo: str, test_commands: list[str],
) -> BranchSweepResult:
    result = BranchSweepResult(target_branch=target_branch, candidates_found=len(candidates))
    tmpdir = tempfile.mkdtemp(prefix=f"backport-{target_branch}-")

    try:
        # Clone
        clone_url = f"https://x-access-token:{github_token}@github.com/{repo_full_name}.git"
        _run_git(tmpdir, "clone", "--branch", target_branch, clone_url, tmpdir)
        _run_git(tmpdir, "config", "user.name", "valkey-ci-agent")
        _run_git(tmpdir, "config", "user.email", "ci-agent@valkey.io")

        # Check for existing backport branch on push_repo
        backport_branch = f"{_BRANCH_PREFIX}/{target_branch}"
        existing_pr = _find_existing_pr(gh, push_repo, backport_branch)

        if existing_pr:
            logger.info("Found existing PR #%d for %s, fetching branch...", existing_pr.number, target_branch)
            push_url = f"https://x-access-token:{github_token}@github.com/{push_repo}.git"
            _run_git(tmpdir, "remote", "add", "push_target", push_url)
            _run_git(tmpdir, "fetch", "push_target", backport_branch)
            _run_git(tmpdir, "checkout", f"push_target/{backport_branch}")
            _run_git(tmpdir, "checkout", "-B", backport_branch)
        else:
            _run_git(tmpdir, "checkout", "-b", backport_branch)
            push_url = f"https://x-access-token:{github_token}@github.com/{push_repo}.git"
            _run_git(tmpdir, "remote", "add", "push_target", push_url)

        # Find already-applied PRs
        already_applied = _list_already_applied(tmpdir, target_branch, backport_branch)
        logger.info("Already applied on %s: %s", backport_branch, already_applied)

        cherry_picker = CherryPickExecutor()
        signer, _ = _resolve_commit_signer()

        for candidate in candidates:
            if str(candidate.source_pr_number) in already_applied:
                result.results.append(CandidateResult(
                    source_pr_number=candidate.source_pr_number,
                    source_pr_title=candidate.source_pr_title,
                    outcome="skipped-existing",
                    detail="already on backport branch",
                ))
                continue

            cr = _apply_candidate(tmpdir, candidate, cherry_picker, signer, repo_full_name, github_token)
            result.results.append(cr)

        # Push if we applied anything
        applied = [r for r in result.results if r.outcome == "applied"]
        if applied:
            check_publish_allowed(target_repo=push_repo, action="git_push", context=backport_branch)
            _run_git(tmpdir, "push", "push_target", backport_branch)
            logger.info("Pushed %d commit(s) to %s/%s", len(applied), push_repo, backport_branch)

            # Upsert PR
            pr_url = _upsert_pr(gh, push_repo, target_branch, backport_branch, result, existing_pr)
            result.pr_url = pr_url

    except Exception as exc:
        logger.error("Error processing branch %s: %s", target_branch, exc)
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    return result


def _apply_candidate(
    repo_dir: str, candidate: ProjectBackportCandidate,
    cherry_picker: CherryPickExecutor, signer: object,
    repo_full_name: str, github_token: str,
) -> CandidateResult:
    sha = candidate.merge_commit_sha
    if not sha:
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "error", "no merge SHA")

    try:
        # Fetch the merge commit
        _run_git(repo_dir, "fetch", "origin", sha)
        cp_result = cherry_picker.cherry_pick(repo_dir, sha)
    except Exception as exc:
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "error", str(exc))

    if cp_result.success:
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "applied")

    # Conflicts — resolve with Claude Code
    if not cp_result.conflicting_files:
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "error", "cherry-pick failed without conflict info")

    pr_context = BackportPRContext(
        source_pr_number=candidate.source_pr_number,
        source_pr_title=candidate.source_pr_title,
        source_pr_body="",
        source_pr_url=candidate.source_pr_url,
        source_pr_diff="",
        target_branch=candidate.target_branch,
        commits=candidate.commit_shas,
        repo_full_name=repo_full_name,
    )

    resolutions = resolve_conflicts_with_claude(repo_dir, cp_result.conflicting_files, pr_context)
    unresolved = [r for r in resolutions if r.resolved_content is None]
    if unresolved:
        # Abort cherry-pick
        subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo_dir, capture_output=True)
        paths = ", ".join(r.path for r in unresolved)
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "skipped-conflict", f"unresolved: {paths}")

    # Apply resolutions and commit
    for r in resolutions:
        if r.resolved_content is not None:
            Path(os.path.join(repo_dir, r.path)).write_text(r.resolved_content)
    _run_git(repo_dir, "add", "-A")
    _run_git(repo_dir, "commit", "--no-edit")

    return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "applied", "conflicts resolved by Claude Code")


# ── PR management ─────────────────────────────────────────────────────

def _find_existing_pr(gh: object, push_repo: str, branch: str) -> object | None:
    try:
        repo = retry_github_call(lambda: gh.get_repo(push_repo), retries=2, description=f"get {push_repo}")
        pulls = retry_github_call(lambda: list(repo.get_pulls(state="open", head=f"{push_repo.split('/')[0]}:{branch}")), retries=2, description="list PRs")
        return pulls[0] if pulls else None
    except Exception:
        return None


def _upsert_pr(gh: object, push_repo: str, target_branch: str, head_branch: str,
               result: BranchSweepResult, existing_pr: object | None) -> str:
    repo = retry_github_call(lambda: gh.get_repo(push_repo), retries=2, description=f"get {push_repo}")
    body = _build_pr_body(result)
    title = f"[backport] Weekly backport sweep for {target_branch}"

    if existing_pr:
        check_publish_allowed(target_repo=push_repo, action="edit_pull", context=f"PR #{existing_pr.number}")
        retry_github_call(lambda: existing_pr.edit(title=title, body=body), retries=2, description="update PR")
        logger.info("Updated PR #%d on %s", existing_pr.number, push_repo)
        return existing_pr.html_url

    check_publish_allowed(target_repo=push_repo, action="create_pull", context=head_branch)
    owner = push_repo.split("/")[0]
    pr = retry_github_call(
        lambda: repo.create_pull(title=title, body=body, head=f"{owner}:{head_branch}", base=target_branch, draft=True),
        retries=2, description="create PR",
    )
    logger.info("Created PR #%d on %s", pr.number, push_repo)
    return pr.html_url


def _list_already_applied(repo_dir: str, base_branch: str, backport_branch: str) -> set[str]:
    """Extract source PR numbers from commit messages on the backport branch."""
    try:
        result = subprocess.run(
            ["git", "log", f"origin/{base_branch}..{backport_branch}", "--format=%s"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        pr_nums: set[str] = set()
        for line in result.stdout.strip().splitlines():
            m = re.search(r"\(#(\d+)\)", line)
            if m:
                pr_nums.add(m.group(1))
        return pr_nums
    except Exception:
        return set()


# ── Summary / PR body ─────────────────────────────────────────────────

def _build_pr_body(result: BranchSweepResult) -> str:
    lines = [
        f"# Weekly backport sweep for {result.target_branch}",
        "",
        "Automated cherry-picks from PRs marked \"To be backported\".",
        "",
    ]
    applied = [r for r in result.results if r.outcome == "applied"]
    skipped = [r for r in result.results if r.outcome != "applied"]

    if applied:
        lines.extend(["## Applied", "", "| Source PR | Title | Detail |", "|---|---|---|"])
        for r in applied:
            lines.append(f"| #{r.source_pr_number} | {_esc(r.source_pr_title)} | {_esc(r.detail)} |")
        lines.append("")

    if skipped:
        lines.extend(["## Skipped", "", "| Source PR | Title | Reason |", "|---|---|---|"])
        for r in skipped:
            lines.append(f"| #{r.source_pr_number} | {_esc(r.source_pr_title)} | {r.outcome}: {_esc(r.detail)} |")
        lines.append("")

    lines.extend(["---", "*Generated by valkey-ci-agent using Claude Code.*"])
    return "\n".join(lines)


def _build_summary(results: list[BranchSweepResult]) -> str:
    lines = ["## Weekly Backport Sweep", ""]
    for r in results:
        applied = sum(1 for c in r.results if c.outcome == "applied")
        lines.append(f"- `{r.target_branch}`: {applied}/{r.candidates_found} applied" + (f" — [PR]({r.pr_url})" if r.pr_url else ""))
    return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────

def _normalize(value: object) -> str:
    return str(value or "").strip().lower()


def _esc(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _release_branch_sort_key(name: str) -> tuple[int, ...]:
    parts = []
    for p in name.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(-1)
    return tuple(parts)


def _project_items_query(owner_field: str) -> str:
    return f"""
query($owner: String!, $number: Int!, $cursor: String) {{
  {owner_field}(login: $owner) {{
    projectV2(number: $number) {{
      items(first: 100, after: $cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          content {{
            __typename
            ... on PullRequest {{
              number title url merged
              mergeCommit {{ oid }}
              commits(first: 100) {{ nodes {{ commit {{ oid }} }} }}
            }}
          }}
          fieldValues(first: 50) {{
            nodes {{
              __typename
              ... on ProjectV2ItemFieldTextValue {{ text field {{ ... on ProjectV2FieldCommon {{ name }} }} }}
              ... on ProjectV2ItemFieldSingleSelectValue {{ name field {{ ... on ProjectV2FieldCommon {{ name }} }} }}
              ... on ProjectV2ItemFieldNumberValue {{ number field {{ ... on ProjectV2FieldCommon {{ name }} }} }}
              ... on ProjectV2ItemFieldIterationValue {{ title field {{ ... on ProjectV2FieldCommon {{ name }} }} }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


def _extract_field_values(item: dict[str, Any]) -> dict[str, list[str]]:
    vals: dict[str, list[str]] = defaultdict(list)
    for v in (item.get("fieldValues") or {}).get("nodes") or []:
        name = (v.get("field") or {}).get("name")
        if not name:
            continue
        vals[_normalize(name)].extend(_field_value_strings(v))
    return dict(vals)


def _field_value_strings(v: dict[str, Any]) -> list[str]:
    t = v.get("__typename")
    if t == "ProjectV2ItemFieldTextValue":
        return [str(v.get("text") or "")]
    if t == "ProjectV2ItemFieldSingleSelectValue":
        return [str(v.get("name") or "")]
    if t == "ProjectV2ItemFieldNumberValue":
        n = v.get("number")
        return [] if n is None else [str(n)]
    if t == "ProjectV2ItemFieldIterationValue":
        return [str(v.get("title") or "")]
    return []


def _field_has_value(fields: dict[str, list[str]], field_name: str, expected: str) -> bool:
    return any(_normalize(v) == _normalize(expected) for v in fields.get(_normalize(field_name), []))


def _matching_release_branch(fields: dict[str, list[str]], branch_fields: list[str], branches: list[str]) -> str | None:
    for fn in branch_fields:
        vals = fields.get(_normalize(fn), [])
        for b in branches:
            if any(_normalize(v) == _normalize(b) or _normalize(v) == f"backport {_normalize(b)}" for v in vals):
                return b
    return None


# ── CLI ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--target-token", required=True)
    parser.add_argument("--project-owner", required=True)
    parser.add_argument("--project-number", required=True, type=int)
    parser.add_argument("--project-owner-type", default="organization")
    parser.add_argument("--push-repo", default="")
    parser.add_argument("--status-field", default=_DEFAULT_STATUS_FIELD)
    parser.add_argument("--status-value", default=_DEFAULT_STATUS_VALUE)
    parser.add_argument("--branch-fields", default=",".join(_DEFAULT_BRANCH_FIELDS))
    parser.add_argument("--test-commands", default="")
    parser.add_argument("--only-branch", default="")
    parser.add_argument("--implicit-target-branch", default="",
                        help="When the project implies the branch (e.g., project 14 → 8.1), set this to override the field-based lookup")
    parser.add_argument("--max-candidates", type=int, default=0,
                        help="Cap the number of candidates per branch (0 = unlimited)")
    parser.add_argument("--aws-region", default="us-east-1")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    results = run_backport_sweep(
        repo_full_name=args.repo,
        github_token=args.target_token,
        project_owner=args.project_owner,
        project_number=args.project_number,
        project_owner_type=args.project_owner_type,
        status_field=args.status_field,
        status_value=args.status_value,
        branch_fields=[f.strip() for f in args.branch_fields.split(",") if f.strip()] or None,
        push_repo=args.push_repo or None,
        only_branch=args.only_branch or None,
        test_commands=[c.strip() for c in args.test_commands.split("\n") if c.strip()] or None,
        discover_only=args.discover_only or args.dry_run,
        implicit_target_branch=args.implicit_target_branch or None,
        max_candidates=args.max_candidates,
    )

    print(json.dumps([{"branch": r.target_branch, "found": r.candidates_found, "applied": sum(1 for c in r.results if c.outcome == "applied"), "pr": r.pr_url} for r in results], indent=2))


if __name__ == "__main__":
    main()

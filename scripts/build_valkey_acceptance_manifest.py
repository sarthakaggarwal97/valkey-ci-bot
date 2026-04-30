"""Generate a live Valkey acceptance manifest from current repository state."""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]
from github import Auth, Github

from scripts.pr_context_fetcher import PRContextFetcher
from scripts.review_policy import collect_review_policy_note

if TYPE_CHECKING:
    from github.Repository import Repository


_REPLAY_WORKFLOWS: tuple[tuple[str, str], ...] = (
    ("daily.yml", "latest daily failure replay"),
    ("ci.yml", "latest CI failure replay"),
    ("external.yml", "latest external failure replay"),
    ("weekly.yml", "latest weekly failure replay"),
    ("benchmark-on-label.yml", "latest benchmark failure replay"),
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="valkey-io/valkey")
    parser.add_argument("--token", default="")
    parser.add_argument("--output", default="examples/valkey-acceptance.yml")
    parser.add_argument("--execution-repo", default="your-user/valkey")
    parser.add_argument("--workflow-seed", default="examples/ci-agent-acceptance.yml")
    parser.add_argument("--max-review-cases", type=int, default=5)
    parser.add_argument("--search-limit", type=int, default=12)
    return parser


def _github_client(token: str) -> Github:
    return Github(auth=Auth.Token(token)) if token else Github()


def _iter_recent_pulls(repo: "Repository", state: str, limit: int) -> list[Any]:
    pulls = repo.get_pulls(state=state, sort="updated", direction="desc")
    results: list[Any] = []
    for pr in pulls:
        results.append(pr)
        if len(results) >= limit:
            break
    return results


def _review_case_name(category: str, title: str) -> str:
    headline = title.strip().replace("\n", " ")
    if len(headline) > 72:
        headline = headline[:69].rstrip() + "..."
    return f"{category}: {headline}" if headline else category


def _build_review_case(pr_context, note, category: str) -> dict[str, Any]:
    return {
        "name": _review_case_name(category, pr_context.title),
        "pr_number": pr_context.number,
        "expectations": {
            "missing_dco": bool(note.missing_dco_commits),
            "needs_core_team": note.needs_core_team,
            "needs_docs": note.needs_docs,
            "security_sensitive": note.security_sensitive,
        },
    }


def _select_review_cases(
    fetcher: PRContextFetcher,
    repo_name: str,
    pulls: list[Any],
    *,
    max_cases: int,
) -> list[dict[str, Any]]:
    candidates: list[tuple[Any, Any]] = []
    seen_prs: set[int] = set()
    for pull in pulls:
        pr_number = int(getattr(pull, "number", 0) or 0)
        if pr_number <= 0 or pr_number in seen_prs:
            continue
        seen_prs.add(pr_number)
        context = fetcher.fetch(repo_name, pr_number)
        note = collect_review_policy_note(context)
        candidates.append((context, note))

    selectors = [
        ("missing dco", lambda note: bool(note.missing_dco_commits)),
        ("needs docs", lambda note: note.needs_docs),
        ("core-team review", lambda note: note.needs_core_team),
        ("extra tests", lambda note: note.needs_extra_tests),
        (
            "clean change",
            lambda note: not any([
                note.missing_dco_commits,
                note.needs_docs,
                note.needs_core_team,
                note.security_sensitive,
            ]),
        ),
    ]

    selected: list[dict[str, Any]] = []
    used_prs: set[int] = set()
    for category, predicate in selectors:
        match = next(
            (
                (context, note)
                for context, note in candidates
                if context.number not in used_prs and predicate(note)
            ),
            None,
        )
        if match is None:
            continue
        context, note = match
        selected.append(_build_review_case(context, note, category))
        used_prs.add(context.number)
        if len(selected) >= max_cases:
            return selected

    for context, note in candidates:
        if context.number in used_prs:
            continue
        selected.append(_build_review_case(context, note, "recent replay"))
        used_prs.add(context.number)
        if len(selected) >= max_cases:
            break
    return selected


def _latest_failed_workflow_run(repo: "Repository", workflow_file: str) -> int | None:
    try:
        workflow = repo.get_workflow(workflow_file)
    except Exception:
        return None
    runs = workflow.get_runs(status="completed")
    for run in runs:
        if getattr(run, "conclusion", "") == "failure":
            return int(getattr(run, "id", 0) or 0) or None
    return None


def _select_ci_cases(repo: "Repository") -> list[dict[str, Any]]:
    seen_run_ids: set[int] = set()
    cases: list[dict[str, Any]] = []
    for workflow_file, name in _REPLAY_WORKFLOWS:
        run_id = _latest_failed_workflow_run(repo, workflow_file)
        if run_id is None or run_id in seen_run_ids:
            continue
        seen_run_ids.add(run_id)
        cases.append(
            {
                "name": name,
                "workflow_run_id": run_id,
                "config_path": ".github/valkey-daily-bot.yml",
                "notes": (
                    f"Generated from the latest failed `{workflow_file}` run. "
                    "Use queue-only mode first and confirm the patch scope stays narrow."
                ),
            }
        )
    return cases


def _latest_release_branch(repo: "Repository") -> str | None:
    release_branches: list[tuple[tuple[int, int], str]] = []
    for branch in repo.get_branches():
        name = str(getattr(branch, "name", "") or "")
        match = re.fullmatch(r"(\d+)\.(\d+)", name)
        if match:
            release_branches.append(((int(match.group(1)), int(match.group(2))), name))
    if not release_branches:
        return None
    release_branches.sort(reverse=True)
    return release_branches[0][1]


def _select_backport_case(repo: "Repository", limit: int) -> dict[str, Any] | None:
    target_branch = _latest_release_branch(repo)
    if not target_branch:
        return None
    for pr in _iter_recent_pulls(repo, "closed", limit):
        if not getattr(pr, "merged_at", None):
            continue
        base = getattr(getattr(pr, "base", None), "ref", "") or ""
        if base != getattr(repo, "default_branch", "unstable"):
            continue
        return {
            "name": f"recent merged replay: {pr.title}",
            "source_pr_number": int(pr.number),
            "target_branch": target_branch,
            "config_path": ".github/backport-agent.yml",
            "notes": (
                "Generated from the latest merged PR on the default branch. "
                "Use this to sanity-check cherry-pick viability on the current release line."
            ),
        }
    return None


def _seed_workflow_cases(path: str | Path) -> list[dict[str, Any]]:
    seed_path = Path(path)
    if not seed_path.exists():
        return []
    raw = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return []
    cases = raw.get("workflow_cases", [])
    return [case for case in cases if isinstance(case, dict)]


def build_manifest(
    gh: Github,
    repo_name: str,
    *,
    execution_repo: str,
    workflow_seed_path: str,
    max_review_cases: int,
    search_limit: int,
) -> dict[str, Any]:
    repo = gh.get_repo(repo_name)
    fetcher = PRContextFetcher(gh)
    open_pulls = _iter_recent_pulls(repo, "open", search_limit)
    closed_pulls = _iter_recent_pulls(repo, "closed", search_limit)
    review_cases = _select_review_cases(
        fetcher,
        repo_name,
        open_pulls,
        max_cases=max_review_cases,
    )
    if len(review_cases) < max_review_cases:
        seen = {int(case["pr_number"]) for case in review_cases}
        for case in _select_review_cases(
            fetcher,
            repo_name,
            closed_pulls,
            max_cases=max_review_cases,
        ):
            if int(case["pr_number"]) in seen:
                continue
            review_cases.append(case)
            seen.add(int(case["pr_number"]))
            if len(review_cases) >= max_review_cases:
                break

    ci_cases = _select_ci_cases(repo)

    backport_cases: list[dict[str, Any]] = []
    backport_case = _select_backport_case(repo, search_limit)
    if backport_case is not None:
        backport_cases.append(backport_case)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_repo": repo_name,
        "execution_repo": execution_repo,
        "reviewer_config_path": ".github/pr-review-bot.yml",
        "review_cases": review_cases,
        "ci_cases": ci_cases,
        "backport_cases": backport_cases,
        "workflow_cases": _seed_workflow_cases(workflow_seed_path),
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    manifest = build_manifest(
        _github_client(args.token),
        args.repo,
        execution_repo=args.execution_repo,
        workflow_seed_path=args.workflow_seed,
        max_review_cases=max(1, args.max_review_cases),
        search_limit=max(5, args.search_limit),
    )
    output_path = Path(args.output)
    output_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

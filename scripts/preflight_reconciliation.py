"""Preflight checks before draining queued fixes into a target repository."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github import Auth, Github

from scripts.failure_store import FailureStore
from scripts.models import failure_report_from_dict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationPreflightResult:
    """Summary of queued-branch compatibility for reconciliation."""

    queued_failure_count: int
    target_branches: list[str]
    missing_branches: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "queued_failure_count": self.queued_failure_count,
            "target_branches": self.target_branches,
            "missing_branches": self.missing_branches,
        }


def _resolve_target_branch(payload: dict[str, object]) -> str:
    """Return the target branch encoded in a queued PR payload."""
    failure_report_payload = payload.get("failure_report")
    if not isinstance(failure_report_payload, dict):
        failure_report_payload = {}
    failure_report = failure_report_from_dict(failure_report_payload)
    target_branch = payload.get("target_branch") or failure_report.target_branch or "unstable"
    return str(target_branch)


def run_preflight(
    repo_name: str,
    github_token: str,
    *,
    state_github_token: str | None = None,
    state_repo_name: str | None = None,
) -> ReconciliationPreflightResult:
    """Ensure every queued fix targets a branch present in the target repo."""
    gh = Github(auth=Auth.Token(github_token))
    state_gh = Github(auth=Auth.Token(state_github_token or github_token))
    state_repo = state_repo_name or repo_name

    failure_store = FailureStore(
        gh,
        repo_name,
        state_github_client=state_gh,
        state_repo_full_name=state_repo,
    )
    failure_store.load()

    queued = failure_store.list_queued_failures()
    target_branches: set[str] = set()
    for fingerprint in queued:
        entry = failure_store.get_entry(fingerprint)
        if entry is None or not entry.queued_pr_payload:
            logger.warning(
                "Skipping queued fingerprint %s during preflight: missing payload.",
                fingerprint[:12],
            )
            continue
        target_branches.add(_resolve_target_branch(entry.queued_pr_payload))

    repo = gh.get_repo(repo_name)
    missing_branches: list[str] = []
    for branch in sorted(target_branches):
        try:
            repo.get_git_ref(f"heads/{branch}")
        except Exception:
            missing_branches.append(branch)

    return ReconciliationPreflightResult(
        queued_failure_count=len(queued),
        target_branches=sorted(target_branches),
        missing_branches=missing_branches,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Repository full name (owner/repo)")
    parser.add_argument("--token", required=True, help="GitHub token for the target repo")
    parser.add_argument("--state-token", default=None, help="GitHub token for agent-state persistence")
    parser.add_argument("--state-repo", default=None, help="Repository full name used for agent-state persistence")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    result = run_preflight(
        args.repo,
        args.token,
        state_github_token=args.state_token,
        state_repo_name=args.state_repo,
    )
    print(json.dumps(result.to_dict(), indent=2))
    if result.missing_branches:
        logger.error(
            "Reconciliation target %s is missing queued base branches: %s",
            args.repo,
            ", ".join(result.missing_branches),
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the live reviewer on a PR fixture and score results.

Usage:
    python -m scripts.eval.review_runner --fixture eval/fixtures/review-3591.json

Calls claude_reviewer.review_pr directly (no posting) and scores the findings
against the fixture's maintainer_comments ground truth.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts.claude_reviewer import review_pr
from scripts.eval.flow_scorer import score_review_flow
from scripts.git_auth import GitAuth, github_https_url
from scripts.models import DiffScope
from scripts.pr_context_fetcher import PRContextFetcher

logger = logging.getLogger(__name__)


def _fetch_pr_diff(repo: str, pr_number: int, token: str) -> DiffScope:
    """Fetch the PR's changed files via GitHub API and build a DiffScope."""
    from github import Auth, Github
    gh = Github(auth=Auth.Token(token))
    fetcher = PRContextFetcher(gh, github_retries=2)
    pr_context = fetcher.fetch(repo, pr_number)
    pr_context = fetcher.hydrate_contents(pr_context, set(f.path for f in pr_context.files))
    return pr_context


def _clone_pr(
    repo: str, base_sha: str, head_sha: str, pr_number: int, token: str,
) -> str:
    """Clone the target repo and check out the PR head. Returns the tmpdir path."""
    tmpdir = tempfile.mkdtemp(prefix="eval-review-")
    git_auth = GitAuth(token, prefix="eval-review-auth-")
    git_auth.__enter__()
    try:
        env = git_auth.env()
        clone_url = github_https_url(repo)
        subprocess.run(
            ["git", "clone", "--filter=blob:none", clone_url, tmpdir],
            env=env, check=True, capture_output=True, timeout=180,
        )
        # Fetch the PR head
        subprocess.run(
            ["git", "fetch", "origin", f"pull/{pr_number}/head:pr-head"],
            cwd=tmpdir, env=env, check=True, capture_output=True, timeout=90,
        )
        subprocess.run(
            ["git", "checkout", "pr-head"],
            cwd=tmpdir, env=env, check=True, capture_output=True, timeout=60,
        )
    finally:
        git_auth.cleanup()
    return tmpdir


def run_fixture(fixture_path: Path, token: str) -> dict[str, Any]:
    """Run the reviewer on a fixture, score the results, return a dict."""
    fixture = json.loads(fixture_path.read_text())
    repo = fixture["repo"]
    pr_number = fixture["pr_number"]

    logger.info("Fetching PR context for %s#%d", repo, pr_number)
    pr_context = _fetch_pr_diff(repo, pr_number, token)

    diff_scope = DiffScope(
        base_sha=pr_context.base_sha,
        head_sha=pr_context.head_sha,
        files=pr_context.files,
        incremental=False,
    )

    logger.info("Cloning repo and checking out PR #%d", pr_number)
    repo_dir = _clone_pr(
        repo, pr_context.base_sha, pr_context.head_sha, pr_number, token,
    )

    try:
        logger.info("Running Claude reviewer on %d files...", len(diff_scope.files))
        findings = review_pr(pr_context, diff_scope, repo_dir=repo_dir)
    finally:
        import shutil
        shutil.rmtree(repo_dir, ignore_errors=True)

    agent_findings = [
        {"path": f.path, "line": f.line or 1, "body": f.body, "severity": f.severity}
        for f in findings
    ]

    maintainer_comments = fixture["ground_truth"]["maintainer_comments"]
    score = score_review_flow(
        fixture["name"], agent_findings, maintainer_comments, line_tolerance=10,
    )

    return {
        "fixture": fixture["name"],
        "pr_number": pr_number,
        "description": fixture["description"],
        "agent_findings": agent_findings,
        "ground_truth": maintainer_comments,
        "score": asdict(score),
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    args = parser.parse_args()

    if not args.token:
        logger.error("No GitHub token provided. Set GITHUB_TOKEN or pass --token.")
        return 1

    result = run_fixture(args.fixture, args.token)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2))
        logger.info("Wrote result to %s", args.output)
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

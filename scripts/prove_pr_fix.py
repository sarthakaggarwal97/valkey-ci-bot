"""Run GitHub-native proof validation for one bot-authored draft PR."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github import Auth, Github

from scripts.config import load_config
from scripts.event_ledger import EventLedger
from scripts.failure_store import FailureStore
from scripts.models import FailureReport, ValidationResult, failure_report_from_dict
from scripts.validation_runner import ValidationRunner

logger = logging.getLogger(__name__)
_COMMENT_MARKER_PREFIX = "<!-- ci-agent-proof:"


def _build_clone_url(repo_full_name: str, token: str) -> str:
    """Return an HTTPS clone URL with embedded token auth."""
    safe_token = urllib_parse.quote(token, safe="")
    return f"https://x-access-token:{safe_token}@github.com/{repo_full_name}.git"


def _proof_run_url() -> str:
    """Return the current GitHub Actions run URL when available."""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""


def _proof_marker(fingerprint: str) -> str:
    return f"{_COMMENT_MARKER_PREFIX}{fingerprint} -->"


def _summarize_validation(result: ValidationResult, required_runs: int) -> str:
    """Return a one-line summary for persistence and comments."""
    if result.passed:
        return (
            f"Proof passed across {result.passed_runs}/{required_runs} "
            "GitHub-native validation runs."
        )
    return (
        f"Proof failed after {result.passed_runs}/{required_runs} "
        "GitHub-native validation runs."
    )


def _render_comment(
    *,
    fingerprint: str,
    result: ValidationResult,
    required_runs: int,
    proof_run_url: str,
    marked_ready: bool,
    was_draft: bool,
) -> str:
    """Render a stable PR comment for the proof campaign."""
    status = "passed" if result.passed else "failed"
    lines = [
        _proof_marker(fingerprint),
        "## CI Agent Proof Campaign",
        "",
        f"- Status: **{status}**",
        f"- Consecutive proof runs: **{result.passed_runs}/{required_runs}**",
        f"- Validation strategy: `{result.strategy}`",
    ]
    if proof_run_url:
        lines.append(f"- Proof workflow: [run]({proof_run_url})")
    if result.passed and marked_ready:
        lines.append("- PR state: marked ready for review automatically.")
    elif result.passed and was_draft:
        lines.append("- PR state: proof passed, but draft promotion needs follow-up.")
    elif result.passed:
        lines.append("- PR state: already ready for review.")
    else:
        lines.append("- PR state: left in draft for human follow-up.")
    output = result.output.strip()
    if output:
        lines.extend(
            [
                "",
                "<details><summary>Validation output</summary>",
                "",
                "```text",
                output[:12000],
                "```",
                "</details>",
            ]
        )
    return "\n".join(lines).strip()


def _upsert_proof_comment(repo, pr_number: int, fingerprint: str, body: str) -> str:
    """Create or update the persistent proof comment on a pull request."""
    issue = repo.get_issue(number=pr_number)
    marker = _proof_marker(fingerprint)
    for comment in issue.get_comments():
        existing = (comment.body or "").strip()
        if marker in existing:
            comment.edit(body)
            return str(comment.html_url or "")
    created = issue.create_comment(body)
    return str(created.html_url or "")


def _github_post(url: str, token: str) -> tuple[int, str]:
    """POST to the GitHub REST API and return status/body."""
    request = urllib_request.Request(
        url,
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "valkey-ci-agent",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return exc.code, detail


def _mark_ready_for_review(repo_full_name: str, pr_number: int, token: str) -> bool:
    """Mark a draft pull request ready for review when GitHub accepts it."""
    status, _ = _github_post(
        (
            f"https://api.github.com/repos/{repo_full_name}/pulls/"
            f"{pr_number}/ready_for_review"
        ),
        token,
    )
    return 200 <= status < 300


def _prepare_report(raw_report: FailureReport, pr) -> FailureReport:
    """Rebind a stored failure report to the current PR head SHA."""
    return FailureReport(
        workflow_name=raw_report.workflow_name,
        job_name=raw_report.job_name,
        matrix_params=dict(raw_report.matrix_params),
        commit_sha=str(pr.head.sha),
        failure_source="trusted",
        parsed_failures=list(raw_report.parsed_failures),
        raw_log_excerpt=raw_report.raw_log_excerpt,
        is_unparseable=raw_report.is_unparseable,
        workflow_file=raw_report.workflow_file,
        repo_full_name=str(pr.head.repo.full_name),
        workflow_run_id=raw_report.workflow_run_id,
        target_branch=str(pr.base.ref or raw_report.target_branch),
    )


def run_proof_campaign(args: argparse.Namespace) -> dict[str, object]:
    """Execute one proof campaign and persist its outcome."""
    config = load_config(args.config)
    target_gh = Github(auth=Auth.Token(args.token))
    state_gh = Github(auth=Auth.Token(args.state_token or args.token))
    target_repo = target_gh.get_repo(args.repo)
    pr = target_repo.get_pull(args.pr_number)
    raw_report = failure_report_from_dict(json.loads(args.failure_report_json))
    report = _prepare_report(raw_report, pr)
    proof_run_url = _proof_run_url()

    failure_store = FailureStore(
        target_gh,
        args.repo,
        state_github_client=state_gh,
        state_repo_full_name=args.state_repo,
    )
    failure_store.load()
    event_ledger = EventLedger(
        target_gh,
        args.repo,
        state_github_client=state_gh,
        state_repo_full_name=args.state_repo,
    )

    failure_store.update_proof_campaign(
        args.fingerprint,
        status="running",
        proof_url=proof_run_url or pr.html_url or "",
        required_runs=max(1, args.repeat_count),
    )
    event_ledger.record(
        "proof.started",
        args.fingerprint,
        job_name=report.job_name,
        failure_identifier=(
            report.parsed_failures[0].failure_identifier
            if report.parsed_failures
            else report.job_name
        ),
        pr_url=str(pr.html_url),
        pr_number=pr.number,
        proof_runs=max(1, args.repeat_count),
        workflow_file=report.workflow_file,
    )
    failure_store.save()
    event_ledger.save()

    runner = ValidationRunner(
        config,
        repo_clone_url=_build_clone_url(args.repo, args.token),
        github_client=target_gh,
        repo_full_name=args.repo,
    )
    result = runner.validate("", report, repeat_count=max(1, args.repeat_count))
    summary = _summarize_validation(result, max(1, args.repeat_count))

    marked_ready = False
    was_draft = bool(getattr(pr, "draft", False))
    if result.passed and was_draft:
        marked_ready = _mark_ready_for_review(args.repo, args.pr_number, args.token)
        if marked_ready:
            event_ledger.record(
                "pr.ready_for_review",
                args.fingerprint,
                pr_url=str(pr.html_url),
                pr_number=pr.number,
                source="proof-campaign",
            )

    comment_url = _upsert_proof_comment(
        target_repo,
        args.pr_number,
        args.fingerprint,
        _render_comment(
            fingerprint=args.fingerprint,
            result=result,
            required_runs=max(1, args.repeat_count),
            proof_run_url=proof_run_url,
            marked_ready=marked_ready,
            was_draft=was_draft,
        ),
    )

    failure_store.update_proof_campaign(
        args.fingerprint,
        status="passed" if result.passed else "failed",
        summary=summary,
        proof_url=proof_run_url or pr.html_url or "",
        required_runs=max(1, args.repeat_count),
        passed_runs=result.passed_runs,
        attempted_runs=result.attempted_runs,
    )
    event_ledger.record(
        "proof.passed" if result.passed else "proof.failed",
        args.fingerprint,
        job_name=report.job_name,
        failure_identifier=(
            report.parsed_failures[0].failure_identifier
            if report.parsed_failures
            else report.job_name
        ),
        pr_url=str(pr.html_url),
        pr_number=pr.number,
        proof_runs=max(1, args.repeat_count),
        passed_runs=result.passed_runs,
        attempted_runs=result.attempted_runs,
        comment_url=comment_url,
        ready_for_review=marked_ready,
    )
    failure_store.save()
    event_ledger.save()

    return {
        "fingerprint": args.fingerprint,
        "repo": args.repo,
        "pr_number": args.pr_number,
        "pr_url": str(pr.html_url),
        "proof_status": "passed" if result.passed else "failed",
        "proof_summary": summary,
        "proof_runs": max(1, args.repeat_count),
        "passed_runs": result.passed_runs,
        "attempted_runs": result.attempted_runs,
        "ready_for_review": marked_ready,
        "comment_url": comment_url,
        "proof_run_url": proof_run_url,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a GitHub-native proof campaign.")
    parser.add_argument("--repo", required=True, help="Target repository full name.")
    parser.add_argument("--pr-number", required=True, type=int, help="Pull request number.")
    parser.add_argument("--fingerprint", required=True, help="Failure fingerprint.")
    parser.add_argument("--failure-report-json", required=True, help="Serialized FailureReport.")
    parser.add_argument("--config", required=True, help="Bot config path.")
    parser.add_argument("--token", required=True, help="GitHub token for the target repo.")
    parser.add_argument("--state-token", default=None, help="GitHub token for state writes.")
    parser.add_argument("--state-repo", required=True, help="Repository full name for bot state.")
    parser.add_argument("--repeat-count", required=True, type=int, help="Required proof runs.")
    parser.add_argument(
        "--output",
        default="proof-result.json",
        help="Path to write the JSON proof result.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    result = run_proof_campaign(args)
    output_path = Path(args.output)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

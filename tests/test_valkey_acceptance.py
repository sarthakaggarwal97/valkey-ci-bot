"""Tests for the Valkey acceptance harness helpers."""

from __future__ import annotations

from pathlib import Path

from scripts.code_reviewer import ReviewCoverage
from scripts.commit_signoff import CommitSigner
from scripts.models import SummaryResult
from scripts.valkey_acceptance import (
    AcceptanceManifest,
    BackportCase,
    CICase,
    ReviewCaseResult,
    ReviewPolicySignals,
    WorkflowCase,
    WorkflowCaseCheck,
    WorkflowCaseResult,
    _build_scorecard,
    _has_signed_off_by,
    _load_manifest,
    _needs_core_team,
    _needs_docs,
    _render_backport_command,
    _render_ci_command,
    _run_workflow_case,
    _security_sensitive,
)


def test_has_signed_off_by_detects_dco_trailer() -> None:
    assert _has_signed_off_by("fix: test\n\nSigned-off-by: Val Key <valkey@example.com>")
    assert not _has_signed_off_by("fix: test")


def test_needs_core_team_matches_valkey_critical_paths() -> None:
    assert _needs_core_team(["src/cluster_slot_stats.c"]) is True
    assert _needs_core_team(["src/replication.c"]) is True
    assert _needs_core_team(["GOVERNANCE.md"]) is True
    assert _needs_core_team(["src/server.c"]) is False


def test_needs_docs_matches_command_and_config_paths() -> None:
    assert _needs_docs(["src/commands/get.json"]) is True
    assert _needs_docs(["valkey.conf"]) is True
    assert _needs_docs(["src/server.c"]) is False


def test_security_sensitive_detects_keywords() -> None:
    assert _security_sensitive("Fix security issue", "", ["src/server.c"]) is True
    assert _security_sensitive("Bug fix", "Addresses CVE-2026-1234", []) is True
    assert _security_sensitive("Bug fix", "", ["src/server.c"]) is False


def test_load_manifest_parses_review_ci_and_backport_cases(tmp_path: Path) -> None:
    manifest_path = tmp_path / "acceptance.yml"
    manifest_path.write_text(
        """
target_repo: valkey-io/valkey
execution_repo: your-user/valkey
review_cases:
  - name: dco-check
    pr_number: 123
    expectations:
      missing_dco: false
ci_cases:
  - name: daily-replay
    workflow_run_id: 456
backport_cases:
  - name: release-8-1
    source_pr_number: 789
    target_branch: "8.1"
workflow_cases:
  - name: review-pr-workflow
    workflow_path: .github/workflows/review-pr.yml
    required_strings:
      - "python -m scripts.pr_review_main"
""",
        encoding="utf-8",
    )

    manifest = _load_manifest(manifest_path)

    assert manifest.target_repo == "valkey-io/valkey"
    assert manifest.execution_repo == "your-user/valkey"
    assert manifest.review_cases[0].pr_number == 123
    assert manifest.ci_cases[0].workflow_run_id == 456
    assert manifest.backport_cases[0].target_branch == "8.1"
    assert manifest.workflow_cases[0].workflow_path == ".github/workflows/review-pr.yml"


def test_render_ci_command_includes_queue_only_and_identity() -> None:
    command = _render_ci_command(
        CICase(name="daily", workflow_run_id=123),
        "your-user/valkey",
        CommitSigner(name="Val Key", email="valkey@example.com"),
    )

    assert "--queue-only" in command
    assert "CI_BOT_COMMIT_NAME='Val Key'" in command
    assert "--run-id 123" in command


def test_render_backport_command_includes_branch_and_identity() -> None:
    command = _render_backport_command(
        BackportCase(name="bp", source_pr_number=123, target_branch="8.1"),
        "your-user/valkey",
        CommitSigner(name="Val Key", email="valkey@example.com"),
    )

    assert "--pr-number 123" in command
    assert "--target-branch 8.1" in command
    assert "CI_BOT_COMMIT_EMAIL='valkey@example.com'" in command


def test_review_case_result_blocks_on_incomplete_model_coverage() -> None:
    result = ReviewCaseResult(
        name="model-case",
        pr_number=123,
        policy=ReviewPolicySignals(
            missing_dco_commits=[],
            needs_core_team=False,
            needs_docs=False,
            security_sensitive=False,
            governance_changed=False,
            changed_files=["src/server.c"],
        ),
        summary=SummaryResult(
            walkthrough="Adds a guarded server path.",
            file_groups_markdown="",
            release_notes=None,
            short_summary="",
        ),
        coverage=ReviewCoverage(
            requested_lgtm=True,
            unaccounted_files=["src/server.c"],
        ),
    )

    assert result.passed is False
    assert result.model_followups == ["review-coverage-incomplete"]


def test_acceptance_scorecard_counts_review_and_replay_cases() -> None:
    passing = ReviewCaseResult(
        name="policy-case",
        pr_number=1,
        policy=ReviewPolicySignals(
            missing_dco_commits=[],
            needs_core_team=False,
            needs_docs=False,
            security_sensitive=False,
            governance_changed=False,
            changed_files=["src/server.c"],
        ),
    )
    failing = ReviewCaseResult(
        name="model-case",
        pr_number=2,
        policy=ReviewPolicySignals(
            missing_dco_commits=[],
            needs_core_team=False,
            needs_docs=False,
            security_sensitive=False,
            governance_changed=False,
            changed_files=["src/server.c"],
        ),
        summary=SummaryResult(
            walkthrough="Adds a guarded server path.",
            file_groups_markdown="",
            release_notes=None,
            short_summary="",
        ),
        coverage=ReviewCoverage(
            requested_lgtm=True,
            unaccounted_files=["src/server.c"],
        ),
    )
    manifest = AcceptanceManifest(
        ci_cases=[CICase(name="daily", workflow_run_id=1)],
        backport_cases=[
            BackportCase(name="bp", source_pr_number=2, target_branch="8.1")
        ],
        workflow_cases=[WorkflowCase(name="review", workflow_path=".github/workflows/review-pr.yml")],
    )
    workflow_results = [
        WorkflowCaseResult(
            name="review",
            workflow_path=".github/workflows/review-pr.yml",
            checks=[
                WorkflowCaseCheck(
                    label="contains:python -m scripts.pr_review_main",
                    passed=True,
                    detail="required fragment present",
                )
            ],
        )
    ]

    scorecard = _build_scorecard(manifest, [passing, failing], workflow_results)

    assert scorecard.review_cases == 2
    assert scorecard.review_passed == 1
    assert scorecard.review_failed == 1
    assert scorecard.workflow_cases == 1
    assert scorecard.workflow_passed == 1
    assert scorecard.workflow_failed == 0
    assert scorecard.ci_replay_cases == 1
    assert scorecard.backport_replay_cases == 1
    assert scorecard.readiness == "needs-follow-up"


def test_run_workflow_case_checks_required_and_forbidden_fragments(tmp_path: Path) -> None:
    workflow_path = tmp_path / "review-pr.yml"
    workflow_path.write_text(
        "name: Review\njobs:\n  review:\n    steps:\n      - run: python -m scripts.pr_review_main\n",
        encoding="utf-8",
    )

    result = _run_workflow_case(
        WorkflowCase(
            name="review",
            workflow_path=str(workflow_path),
            required_strings=["python -m scripts.pr_review_main"],
            forbidden_strings=["build-only validation"],
        )
    )

    assert result.passed is True
    assert any(check.label == "yaml-parse" and check.passed for check in result.checks)

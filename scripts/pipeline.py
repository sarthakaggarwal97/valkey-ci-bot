"""CI-failure pipeline orchestrator — wires evidence-first stages.

Replaces the monolithic main.py with a staged, evidence-first flow.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from scripts.models import (
    EvidencePack,
    FailureStoreEntry,
    RejectionReason,
    RootCauseResult,
    RubricVerdict,
    TournamentResult,
)
from scripts.stages.evidence import build_for_ci_failure
from scripts.stages.fix_tournament import run_tournament
from scripts.stages.root_cause import analyze as analyze_root_cause
from scripts.stages.rubric import RubricGate

logger = logging.getLogger(__name__)


@dataclass
class StageLog:
    """Structured log record for one stage execution."""

    stage: str
    failure_id: str
    duration_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    outcome: str = ""  # "accepted", "rejected", "error"
    rejection_reason: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "stage": self.stage,
            "failure_id": self.failure_id,
            "duration_ms": self.duration_ms,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "outcome": self.outcome,
            "rejection_reason": self.rejection_reason,
        })


@dataclass
class PipelineOutcome:
    """Result of processing one failure through the pipeline."""

    failure_id: str
    evidence: EvidencePack | None = None
    root_cause: RootCauseResult | None = None
    tournament: TournamentResult | None = None
    rubric: RubricVerdict | None = None
    pr_url: str | None = None
    final_status: str = ""  # "pr-created", "needs-human", "error"
    rejection_reason: RejectionReason | None = None
    stage_logs: list[StageLog] | None = None


def _timed(stage_name: str, failure_id: str):
    """Context manager that yields a StageLog and fills duration on exit."""
    class _Timer:
        def __init__(self):
            self.log = StageLog(stage=stage_name, failure_id=failure_id, duration_ms=0)
            self._start = 0.0
        def __enter__(self):
            self._start = time.monotonic()
            return self.log
        def __exit__(self, *args):
            self.log.duration_ms = int((time.monotonic() - self._start) * 1000)
    return _Timer()


def _persist_outcome(
    failure_store: Any,
    failure_id: str,
    outcome: "PipelineOutcome",
) -> None:
    """Update the failure store entry with pipeline-run state.

    No-op when ``failure_store`` is None or the entry does not exist yet.
    Uses ``pipeline_adapter.update_failure_store_entry`` to avoid
    overwriting fields the pipeline did not produce.
    """
    if failure_store is None:
        return
    try:
        from scripts.pipeline_adapter import update_failure_store_entry
        entries = getattr(failure_store, "entries", None)
        if entries is None or failure_id not in entries:
            return
        entry = entries[failure_id]
        status = None
        if outcome.final_status == "pr-created":
            status = "processing"
        elif outcome.final_status == "needs-human":
            status = "needs-human"
        elif outcome.final_status == "error":
            status = "error"
        update_failure_store_entry(
            entry,
            evidence=outcome.evidence,
            rejection=outcome.rejection_reason,
            pr_url=outcome.pr_url,
            status=status,
        )
    except Exception as exc:
        logger.warning("Failed to persist outcome for %s: %s", failure_id, exc)


def process_failure(
    *,
    failure_id: str,
    run_id: int | None,
    job_ids: list[str],
    workflow: str,
    failure_reports: list[dict[str, Any]],
    log_text: str | None = None,
    recent_commits: list[dict[str, Any]] | None = None,
    linked_urls: list[str] | None = None,
    bedrock_client: Any,
    validation_runner: Any,
    pr_manager: Any | None = None,
    config: Any | None = None,
    semaphore: threading.BoundedSemaphore | None = None,
    dry_run: bool = False,
    failure_store: Any | None = None,
) -> PipelineOutcome:
    """Process one failure through the full evidence-first pipeline."""
    stage_logs: list[StageLog] = []
    ai_cfg = getattr(config, "ai_stages", None)

    def _finalize(outcome: PipelineOutcome) -> PipelineOutcome:
        _persist_outcome(failure_store, failure_id, outcome)
        return outcome

    # Stage 0: Evidence
    with _timed("evidence", failure_id) as slog:
        try:
            evidence = build_for_ci_failure(
                failure_id=failure_id, run_id=run_id, job_ids=job_ids,
                workflow=workflow, failure_reports=failure_reports,
                log_text=log_text, recent_commits=recent_commits,
                linked_urls=linked_urls,
            )
            evidence.validate()
            slog.outcome = "accepted"
        except Exception as exc:
            slog.outcome = "error"
            logger.error("Evidence build failed for %s: %s", failure_id, exc)
            stage_logs.append(slog)
            return _finalize(PipelineOutcome(
                failure_id=failure_id, final_status="error", stage_logs=stage_logs,
            ))
    stage_logs.append(slog)

    # Stage 1-2: Root cause analyst + critic
    with _timed("root_cause", failure_id) as slog:
        try:
            analyst_model = getattr(ai_cfg, "root_cause_analyst", None)
            critic_model = getattr(ai_cfg, "root_cause_critic", None)
            min_conf = getattr(ai_cfg, "min_confidence_for_fix", "medium") if ai_cfg else "medium"
            rc_result = analyze_root_cause(
                evidence, bedrock_client,
                analyst_model=getattr(analyst_model, "model", "") if analyst_model else "",
                critic_model=getattr(critic_model, "model", "") if critic_model else "",
                min_confidence=min_conf,
            )
            if rc_result.accepted is None:
                slog.outcome = "rejected"
                slog.rejection_reason = rc_result.rejection_reason.value if rc_result.rejection_reason else ""
                stage_logs.append(slog)
                return _finalize(PipelineOutcome(
                    failure_id=failure_id, evidence=evidence,
                    root_cause=rc_result, final_status="needs-human",
                    rejection_reason=rc_result.rejection_reason,
                    stage_logs=stage_logs,
                ))
            slog.outcome = "accepted"
        except Exception as exc:
            slog.outcome = "error"
            logger.error("Root cause analysis failed for %s: %s", failure_id, exc)
            stage_logs.append(slog)
            return _finalize(PipelineOutcome(
                failure_id=failure_id, evidence=evidence,
                final_status="error", stage_logs=stage_logs,
            ))
    stage_logs.append(slog)

    # Stage 3-4: Fix tournament
    with _timed("tournament", failure_id) as slog:
        try:
            fix_model = getattr(ai_cfg, "fix_generator", None)
            fixes_cfg = getattr(ai_cfg, "fixes", None)
            candidate_count = getattr(fixes_cfg, "candidate_count", 1) if fixes_cfg else 1
            tournament = run_tournament(
                evidence, rc_result.accepted, bedrock_client, validation_runner,
                model_id=getattr(fix_model, "model", "") if fix_model else "",
                candidate_count=candidate_count,
                semaphore=semaphore,
            )
            if tournament.winning is None:
                slog.outcome = "rejected"
                slog.rejection_reason = RejectionReason.TOURNAMENT_EMPTY.value
                stage_logs.append(slog)
                return _finalize(PipelineOutcome(
                    failure_id=failure_id, evidence=evidence,
                    root_cause=rc_result, tournament=tournament,
                    final_status="needs-human",
                    rejection_reason=RejectionReason.TOURNAMENT_EMPTY,
                    stage_logs=stage_logs,
                ))
            slog.outcome = "accepted"
        except Exception as exc:
            slog.outcome = "error"
            logger.error("Tournament failed for %s: %s", failure_id, exc)
            stage_logs.append(slog)
            return _finalize(PipelineOutcome(
                failure_id=failure_id, evidence=evidence,
                root_cause=rc_result, final_status="error",
                stage_logs=stage_logs,
            ))
    stage_logs.append(slog)

    # Stage 5: Rubric gate
    with _timed("rubric", failure_id) as slog:
        rubric_model = getattr(ai_cfg, "rubric_critic", None)
        gate = RubricGate()
        failing_assertion = ""
        if evidence.parsed_failures:
            pf = evidence.parsed_failures[0]
            failing_assertion = pf.error_message or ""
        # Construct a commit message with DCO signoff for the rubric check.
        # The actual PR commit message is built later by pr_manager — this is
        # just for the rubric gate to verify the signoff convention.
        commit_message = (
            f"Fix: {rc_result.accepted.summary[:72]}\n\n"
            f"{rc_result.accepted.summary}\n\n"
            "Signed-off-by: valkey-ci-agent <ci-agent@valkey.io>"
        )
        verdict = gate.judge(
            patch=tournament.winning.candidate.patch,
            evidence=evidence,
            commit_message=commit_message,
            failing_assertion=failing_assertion,
            bedrock_client=bedrock_client,
            model_id=getattr(rubric_model, "model", "") if rubric_model else "",
        )
        if not verdict.overall_passed:
            slog.outcome = "rejected"
            slog.rejection_reason = RejectionReason.RUBRIC_FAILED.value
            stage_logs.append(slog)
            return _finalize(PipelineOutcome(
                failure_id=failure_id, evidence=evidence,
                root_cause=rc_result, tournament=tournament,
                rubric=verdict, final_status="needs-human",
                rejection_reason=RejectionReason.RUBRIC_FAILED,
                stage_logs=stage_logs,
            ))
        slog.outcome = "accepted"
    stage_logs.append(slog)

    # Stage 6: PR creation
    if dry_run:
        logger.info("DRY RUN: would create PR for %s", failure_id)
        return _finalize(PipelineOutcome(
            failure_id=failure_id, evidence=evidence,
            root_cause=rc_result, tournament=tournament,
            rubric=verdict, final_status="dry-run",
            stage_logs=stage_logs,
        ))

    pr_url = None
    if pr_manager:
        with _timed("pr_publish", failure_id) as slog:
            try:
                from scripts.pipeline_adapter import create_pr_via_legacy_manager
                pr_url = create_pr_via_legacy_manager(
                    pr_manager=pr_manager,
                    tournament=tournament,
                    root_cause=rc_result,
                    evidence=evidence,
                    job_name=job_ids[0] if job_ids else "",
                    workflow_file=workflow,
                )
                slog.outcome = "accepted"
            except Exception as exc:
                slog.outcome = "error"
                logger.error("PR creation failed for %s: %s", failure_id, exc)
        stage_logs.append(slog)

    return _finalize(PipelineOutcome(
        failure_id=failure_id, evidence=evidence,
        root_cause=rc_result, tournament=tournament,
        rubric=verdict, pr_url=pr_url,
        final_status="pr-created" if pr_url else "error",
        stage_logs=stage_logs,
    ))

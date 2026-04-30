"""EvidenceBuilder — Stage 0 of the evidence-first AI pipeline.

Builds a canonical EvidencePack before any model proposes a cause or fix.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from scripts.models import (
    CommitInfo,
    EvidencePack,
    InspectedFile,
    LogExcerpt,
    ParsedFailure,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_FILE_PATH_RE = re.compile(
    r"(?:^|[\s\"'(])"
    r"((?:src|tests|test|include|lib|modules)/[A-Za-z0-9_./-]+\.(?:c|h|tcl|cc|cpp|py))"
    r"(?=[:\s\"'),;]|$)"
)

_LOG_CONTEXT_BEFORE = 10
_LOG_CONTEXT_AFTER = 30


def _extract_file_refs(text: str) -> list[str]:
    """Extract file paths referenced in log/error text."""
    return list(dict.fromkeys(_FILE_PATH_RE.findall(text)))


def _excerpt_around(lines: list[str], center: int, before: int, after: int) -> tuple[str, int, int]:
    start = max(0, center - before)
    end = min(len(lines), center + after + 1)
    return "\n".join(lines[start:end]), start, end


def build_for_ci_failure(
    *,
    failure_id: str,
    run_id: int | None,
    job_ids: list[str],
    workflow: str,
    failure_reports: list[dict[str, Any]],
    log_text: str | None = None,
    repo_context: Any | None = None,
    recent_commits: list[dict[str, Any]] | None = None,
    linked_urls: list[str] | None = None,
) -> EvidencePack:
    """Build an EvidencePack from CI-failure inputs.

    Pure function modulo the injected data — no module-level state.
    """
    parsed_failures: list[ParsedFailure] = []
    for report in failure_reports:
        for pf_data in report.get("parsed_failures", []):
            if isinstance(pf_data, dict):
                parsed_failures.append(ParsedFailure(**pf_data))
            elif isinstance(pf_data, ParsedFailure):
                parsed_failures.append(pf_data)

    # Build log excerpts around each failure
    log_excerpts: list[LogExcerpt] = []
    if log_text:
        lines = log_text.splitlines()
        for pf in parsed_failures:
            # Try to find the failure in the log
            for i, line in enumerate(lines):
                if pf.failure_identifier and pf.failure_identifier in line:
                    content, start, end = _excerpt_around(
                        lines, i, _LOG_CONTEXT_BEFORE, _LOG_CONTEXT_AFTER
                    )
                    log_excerpts.append(LogExcerpt(
                        source="job-log",
                        content=content,
                        line_start=start,
                        line_end=end,
                    ))
                    break
        # If no specific excerpts, take the last 40 lines
        if not log_excerpts and lines:
            tail = "\n".join(lines[-40:])
            log_excerpts.append(LogExcerpt(
                source="job-log-tail",
                content=tail,
                line_start=max(0, len(lines) - 40),
                line_end=len(lines),
            ))

    # Collect file references from failures
    source_files: list[InspectedFile] = []
    test_files: list[InspectedFile] = []
    seen_paths: set[str] = set()
    for pf in parsed_failures:
        if pf.file_path and pf.file_path not in seen_paths:
            seen_paths.add(pf.file_path)
            target = test_files if pf.file_path.startswith("tests/") else source_files
            target.append(InspectedFile(path=pf.file_path, reason="parsed failure"))

    # Extract additional file refs from log text
    if log_text:
        for path in _extract_file_refs(log_text):
            if path not in seen_paths:
                seen_paths.add(path)
                target = test_files if path.startswith("tests/") else source_files
                target.append(InspectedFile(path=path, reason="log reference"))

    # Guidance used
    guidance: list[str] = []
    if repo_context and hasattr(repo_context, "guidance_docs_used"):
        guidance = list(repo_context.guidance_docs_used)

    # Recent commits
    commits: list[CommitInfo] = []
    for c in (recent_commits or []):
        if isinstance(c, dict):
            commits.append(CommitInfo(
                sha=str(c.get("sha", "")),
                message=str(c.get("message", "")),
                author=str(c.get("author", "")),
                files_changed=list(c.get("files_changed", [])),
            ))

    # Track unknowns
    unknowns: list[str] = []
    if not log_text:
        unknowns.append("log_text_unavailable")
    if not parsed_failures:
        unknowns.append("no_parsed_failures")

    return EvidencePack(
        failure_id=failure_id,
        run_id=run_id,
        job_ids=job_ids,
        workflow=workflow,
        parsed_failures=parsed_failures,
        log_excerpts=log_excerpts,
        source_files_inspected=source_files,
        test_files_inspected=test_files,
        valkey_guidance_used=guidance,
        recent_commits=commits,
        linked_urls=linked_urls or [],
        unknowns=unknowns,
        built_at=datetime.now(timezone.utc).isoformat(),
    )


def build_for_pr_review(
    *,
    pr_number: int,
    diff: str,
    files_changed: list[str],
    pr_title: str = "",
    pr_body: str = "",
    repo_context: Any | None = None,
    recent_commits: list[dict[str, Any]] | None = None,
) -> EvidencePack:
    """Build an EvidencePack for a PR review."""
    source_files: list[InspectedFile] = []
    test_files: list[InspectedFile] = []
    for path in files_changed:
        target = test_files if path.startswith("tests/") else source_files
        target.append(InspectedFile(path=path, reason="changed in PR"))

    guidance: list[str] = []
    if repo_context and hasattr(repo_context, "guidance_docs_used"):
        guidance = list(repo_context.guidance_docs_used)

    commits: list[CommitInfo] = []
    for c in (recent_commits or []):
        if isinstance(c, dict):
            commits.append(CommitInfo(
                sha=str(c.get("sha", "")),
                message=str(c.get("message", "")),
                author=str(c.get("author", "")),
                files_changed=list(c.get("files_changed", [])),
            ))

    return EvidencePack(
        failure_id=f"pr-{pr_number}",
        run_id=None,
        job_ids=[],
        workflow="pr-review",
        parsed_failures=[],
        log_excerpts=[LogExcerpt(source="pr-diff", content=diff[:50000])],
        source_files_inspected=source_files,
        test_files_inspected=test_files,
        valkey_guidance_used=guidance,
        recent_commits=commits,
        linked_urls=[f"https://github.com/valkey-io/valkey/pull/{pr_number}"],
        unknowns=[],
        built_at=datetime.now(timezone.utc).isoformat(),
    )

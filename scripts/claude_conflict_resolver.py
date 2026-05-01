"""Claude Code-based merge conflict resolver for the backport pipeline.

Replaces the Bedrock-based conflict_resolver.py. Instead of resolving
conflicts file-by-file via a tool-use loop, this gives Claude Code the
entire repo checkout (with conflict markers present) and lets it read
the source, understand the PR intent, and edit files in place.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.backport_models import ConflictedFile, ResolutionResult
from scripts.backport_utils import (
    has_conflict_markers,
    is_whitespace_only_conflict,
    validate_resolved_content,
)
from scripts.claude_code import run_claude_code

if TYPE_CHECKING:
    from scripts.backport_models import BackportPRContext

logger = logging.getLogger(__name__)


def resolve_conflicts_with_claude(
    repo_dir: str,
    conflicting_files: list[ConflictedFile],
    pr_context: BackportPRContext,
) -> list[ResolutionResult]:
    """Resolve cherry-pick merge conflicts using Claude Code.

    Whitespace-only conflicts are resolved without an LLM call.
    For real conflicts, Claude Code reads the repo (with conflict markers
    present in the working tree) and edits files in place.

    Returns a ResolutionResult per conflicting file.
    """
    results: list[ResolutionResult] = []
    llm_files: list[ConflictedFile] = []

    # Fast path: whitespace-only conflicts
    for cf in conflicting_files:
        if is_whitespace_only_conflict(cf.target_branch_content, cf.source_branch_content):
            results.append(ResolutionResult(
                path=cf.path,
                resolved_content=cf.source_branch_content,
                resolution_summary="whitespace-only (no LLM needed)",
                tokens_used=0,
                attempts=0,
            ))
        else:
            llm_files.append(cf)

    if not llm_files:
        return results

    # Build prompt for Claude Code
    file_list = "\n".join(f"- {cf.path}" for cf in llm_files)
    prompt = (
        f"You are resolving merge conflicts in the Valkey C codebase.\n\n"
        f"Source PR #{pr_context.source_pr_number}: \"{pr_context.source_pr_title}\"\n"
        f"URL: {pr_context.source_pr_url}\n"
        f"Target branch: {pr_context.target_branch}\n\n"
        f"This PR was cherry-picked onto the release branch but hit conflicts "
        f"in these files:\n{file_list}\n\n"
        f"The files currently have unresolved conflict markers (<<<<<<<, =======, >>>>>>>).\n\n"
        f"Your task:\n"
        f"1. Read each conflicted file\n"
        f"2. Understand the source PR's intent (preserve it — don't add new functionality)\n"
        f"3. Resolve each conflict by editing the files in place\n"
        f"4. After editing, verify no conflict markers remain\n\n"
        f"Do NOT wrap output in markdown. Just edit the files directly."
    )

    logger.info(
        "Calling Claude Code to resolve %d conflict(s) for PR #%d onto %s...",
        len(llm_files), pr_context.source_pr_number, pr_context.target_branch,
    )
    stdout, stderr, rc = run_claude_code(
        prompt, cwd=repo_dir, timeout=1200,
        allowed_tools="Read,Edit,Grep,Glob,Bash",
    )

    # Extract result from JSONL stream
    result_text = ""
    for line in stdout.strip().splitlines():
        try:
            event = json.loads(line)
            if event.get("type") == "result" and "result" in event:
                result_text = event["result"]
        except (json.JSONDecodeError, TypeError):
            continue

    logger.info(
        "Claude Code finished (rc=%d). Result: %s",
        rc, result_text[:200] if result_text else "(no result text)",
    )

    # Check each file for successful resolution
    for cf in llm_files:
        file_path = os.path.join(repo_dir, cf.path)
        try:
            resolved = Path(file_path).read_text()
        except OSError as exc:
            results.append(ResolutionResult(
                path=cf.path, resolved_content=None,
                resolution_summary=f"failed to read: {exc}",
                tokens_used=0, attempts=1,
            ))
            continue

        if has_conflict_markers(resolved):
            results.append(ResolutionResult(
                path=cf.path, resolved_content=None,
                resolution_summary="conflict markers remain after Claude Code",
                tokens_used=0, attempts=1,
            ))
            continue

        valid = validate_resolved_content(cf.path, resolved)
        results.append(ResolutionResult(
            path=cf.path,
            resolved_content=resolved,
            resolution_summary="resolved by Claude Code" + ("" if valid else " (validation warning)"),
            tokens_used=0,
            attempts=1,
        ))

    return results

"""Claude Code-based PR reviewer, summarizer, and chat responder.

Replaces code_reviewer.py (3298 LOC), pr_summarizer.py (288 LOC), and
review_chat.py (267 LOC) with three thin functions that delegate to
Claude Code CLI. Claude reads the full repo checkout and uses
Read/Grep/Glob/Bash to inspect the code.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from scripts.claude_code import run_claude_code
from scripts.models import ReviewFinding

if TYPE_CHECKING:
    from scripts.models import DiffScope, PullRequestContext, ReviewThread

logger = logging.getLogger(__name__)

_VALID_SEVERITIES = {"info", "low", "medium", "high", "critical"}
_VALID_CONFIDENCES = {"low", "medium", "high"}


def _extract_result_text(stdout: str) -> str:
    """Extract the final result text from Claude Code JSONL stream output."""
    result_text = ""
    for line in stdout.strip().splitlines():
        try:
            event = json.loads(line)
            if event.get("type") == "result" and "result" in event:
                result_text = event["result"]
        except (json.JSONDecodeError, TypeError):
            continue
    return result_text


def _parse_findings_json(text: str) -> list[dict[str, Any]]:
    """Extract a JSON array of findings from Claude's result text."""
    # Try to find a JSON array in the text
    # Claude may wrap it in markdown fences
    cleaned = text.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n(.*?)\n```", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1).strip()
    # Try parsing as JSON array
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "findings" in data:
            return data["findings"]
    except json.JSONDecodeError:
        pass
    # Try finding a JSON array within the text
    m = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    logger.warning("Could not parse findings JSON from Claude output")
    return []


def _validate_finding(raw: dict[str, Any], changed_paths: set[str]) -> ReviewFinding | None:
    """Validate and normalize a raw finding dict into a ReviewFinding."""
    path = str(raw.get("path") or "")
    if not path or path not in changed_paths:
        return None  # Reject hallucinated paths

    line = raw.get("line")
    if line is not None:
        try:
            line = int(line)
        except (ValueError, TypeError):
            line = None

    severity = str(raw.get("severity") or "medium").lower()
    if severity not in _VALID_SEVERITIES:
        severity = "medium"

    confidence = str(raw.get("confidence") or "medium").lower()
    if confidence not in _VALID_CONFIDENCES:
        confidence = "medium"

    body = str(raw.get("body") or "")
    if not body:
        return None

    return ReviewFinding(
        path=path,
        line=line,
        body=body,
        severity=severity,
        title=str(raw.get("title") or ""),
        confidence=confidence,
        trigger=str(raw.get("trigger") or ""),
        impact=str(raw.get("impact") or ""),
        supporting_paths=[str(p) for p in (raw.get("supporting_paths") or []) if p],
        verification_notes=str(raw.get("verification_notes") or ""),
    )


def _serialize_diff_scope(diff_scope: DiffScope) -> str:
    """Render the diff scope as text for the prompt."""
    parts = []
    for f in diff_scope.files:
        parts.append(f"### {f.path} ({f.status}, +{f.additions}/-{f.deletions})")
        if f.patch:
            parts.append(f"```diff\n{f.patch}\n```")
    return "\n".join(parts)


# ── Public API ────────────────────────────────────────────────────────

def review_pr(
    pr_context: PullRequestContext,
    diff_scope: DiffScope,
    repo_dir: str,
    *,
    previous_reviewed_sha: str | None = None,
) -> list[ReviewFinding]:
    """Review a PR using Claude Code with full repo access.

    Args:
        pr_context: PR metadata (number, title, body, base branch, etc.)
        diff_scope: The files and patches to review
        repo_dir: Path to the repo checkout (PR branch checked out at HEAD)
        previous_reviewed_sha: If set, only review commits after this SHA

    Returns:
        List of validated ReviewFinding objects
    """
    if not diff_scope.files:
        return []

    changed_paths = {f.path for f in diff_scope.files}
    diff_text = _serialize_diff_scope(diff_scope)

    incremental_note = ""
    if previous_reviewed_sha:
        incremental_note = (
            f"\n**Incremental review**: Only review changes after commit {previous_reviewed_sha}. "
            f"Use `git log {previous_reviewed_sha}..HEAD --oneline` to see new commits.\n"
        )

    prompt = (
        f"You are a Valkey core maintainer reviewing PR #{pr_context.number}. "
        f"Valkey is a C Redis-compatible database. You know the codebase deeply.\n\n"
        f"**Title**: {pr_context.title}\n"
        f"**Base**: {pr_context.base_ref}\n"
        f"**Description**: {pr_context.body[:2500]}\n\n"
        f"{incremental_note}"
        f"## Changed files\n{diff_text}\n\n"
        f"The repo is checked out at HEAD in the current directory. Use Read/Grep/Glob/Bash to investigate. "
        f"Read functions in full context, grep for callers, check error paths, memory, concurrency, tests.\n\n"
        f"## Review like a human maintainer\n"
        f"Your comments appear inline on the PR. Write the way senior maintainers do on GitHub:\n\n"
        f"**Good** (write like this):\n"
        f"- \"This can race with `freeClient` — do we hold the client lock here?\"\n"
        f"- \"`zmalloc` return isn't checked; OOM here will crash instead of returning a graceful error.\"\n"
        f"- \"Same issue exists in `tls.c:syncSSLRead`, worth addressing as a follow-up.\"\n"
        f"- \"Looks like this reverts commit abc123 — was that intentional?\"\n"
        f"- Use GitHub suggestion blocks for concrete one-line fixes:\n"
        f"  ```\n"
        f"  ```suggestion\n"
        f"      if (!buf) return C_ERR;\n"
        f"  ```\n"
        f"  ```\n\n"
        f"**Bad** (don't write like this):\n"
        f"- \"The diff shows +0/-5272 — every line deleted including processMultibulkBuffer...\" (forensic)\n"
        f"- \"I ran `git cat-file -s` and it returns 0 bytes...\" (showing methodology)\n"
        f"- Multi-paragraph explanations when a question will do\n"
        f"- Restating what the PR already said\n\n"
        f"## What matters\n"
        f"Flag real bugs: memory leaks, NULL derefs, UAF, races, missing error handling, protocol/RDB/AOF breakage, "
        f"incorrect locks, broken invariants, tests that don't test the changed code.\n\n"
        f"Skip: style nits, naming, formatting, preferences, things already handled elsewhere.\n\n"
        f"## Output\n"
        f"Return JSON array. Use line numbers from the NEW version of the file (after the PR's changes); "
        f"they must point to lines present in the diff for GitHub to post inline.\n"
        f"```json\n"
        f"[\n"
        f'  {{"path": "src/file.c", "line": 42, "severity": "high",\n'
        f'    "title": "Brief title",\n'
        f'    "body": "Short human comment. Use ```suggestion``` block for concrete fixes.",\n'
        f'    "confidence": "high"}}\n'
        f"]\n"
        f"```\n\n"
        f"Severities: info, low, medium, high, critical. Confidence: low, medium, high.\n"
        f"Prefer fewer high-confidence findings. A 500-line PR usually needs 3-10 comments. "
        f"Return `[]` if genuinely nothing to flag."
    )

    logger.info("Reviewing PR #%d (%d files)...", pr_context.number, len(diff_scope.files))
    stdout, stderr, rc = run_claude_code(
        prompt, cwd=repo_dir, timeout=1800,
        allowed_tools="Read,Grep,Glob,Bash",
        effort="max",
    )

    result_text = _extract_result_text(stdout)
    if not result_text:
        logger.warning("No result from Claude Code for PR #%d review", pr_context.number)
        return []

    raw_findings = _parse_findings_json(result_text)
    findings = []
    for raw in raw_findings:
        finding = _validate_finding(raw, changed_paths)
        if finding:
            findings.append(finding)

    logger.info("PR #%d: %d finding(s) from Claude Code (%d raw, %d validated)",
                pr_context.number, len(findings), len(raw_findings), len(findings))
    return findings


def summarize_pr(
    pr_context: PullRequestContext,
    diff_scope: DiffScope,
    repo_dir: str,
) -> str:
    """Generate a PR summary using Claude Code."""
    diff_text = _serialize_diff_scope(diff_scope)

    prompt = (
        f"Summarize PR #{pr_context.number} on the Valkey project for other maintainers.\n\n"
        f"**Title**: {pr_context.title}\n"
        f"**Description**:\n{pr_context.body[:2000]}\n\n"
        f"## Changed files\n{diff_text}\n\n"
        f"The repo is checked out at the PR's HEAD. Read the code as needed.\n\n"
        f"Write 2-3 short paragraphs in the style maintainers use:\n"
        f"- What the PR does (1-2 sentences, not a list of every commit)\n"
        f"- Why it matters / what bug it fixes\n"
        f"- Any concerns worth flagging (compatibility, edge cases, missing tests)\n\n"
        f"Keep it conversational. Don't restate the PR description. Don't include methodology (\"I looked at...\").\n"
        f"Return ONLY the summary markdown, no preamble."
    )

    logger.info("Summarizing PR #%d...", pr_context.number)
    stdout, _, _ = run_claude_code(
        prompt, cwd=repo_dir, timeout=600,
        allowed_tools="Read,Grep,Glob",
    )

    result_text = _extract_result_text(stdout)
    return result_text or f"Summary unavailable for PR #{pr_context.number}."


def reply_to_review_comment(
    pr_context: PullRequestContext,
    review_thread: ReviewThread,
    repo_dir: str,
) -> str:
    """Reply to a review thread comment using Claude Code."""
    conversation = "\n".join(review_thread.conversation)
    file_note = ""
    if review_thread.path:
        file_note = f"File: `{review_thread.path}`"
        if review_thread.line:
            file_note += f" line {review_thread.line}"

    prompt = (
        f"You are replying to a code review comment on PR #{pr_context.number} (Valkey project).\n\n"
        f"{file_note}\n\n"
        f"## Conversation so far\n{conversation}\n\n"
        f"The repo is checked out at the PR's HEAD. Read the relevant code before replying.\n"
        f"Write a helpful, concise reply. Return ONLY the reply text."
    )

    logger.info("Replying to review thread on PR #%d...", pr_context.number)
    stdout, _, _ = run_claude_code(
        prompt, cwd=repo_dir, timeout=600,
        allowed_tools="Read,Grep,Glob",
    )

    result_text = _extract_result_text(stdout)
    return result_text or "I wasn't able to generate a reply for this thread."

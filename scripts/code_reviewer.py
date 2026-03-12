"""Bedrock-backed detailed PR code review."""

from __future__ import annotations

import json
import logging
import re
from pathlib import PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

from scripts.bedrock_client import PromptClient
from scripts.bedrock_retriever import BedrockRetriever
from scripts.config import RetrievalConfig, ReviewerConfig
from scripts.models import ChangedFile, DiffScope, PullRequestContext, ReviewFinding

_SYSTEM_PROMPT = """You are a strict code reviewer.
Return only high-confidence, defect-oriented findings about correctness,
regressions, security, performance risks, or missing validation.
Only report issues that are directly supported by the provided patch/content.
The provided excerpts may be truncated; never treat missing context as a bug.
Do not speculate about symbols, methods, fields, workflows, or files that are
not shown, and do not ask maintainers to verify whether something exists.
Avoid duplicate or overlapping findings for the same root cause.
Do not include praise or generic approvals.
Return valid JSON only."""

_SPECULATIVE_SUBSTRINGS = (
    "not shown in the diff",
    "not shown in diff",
    "there is no evidence",
    "appears to be cut off",
    "truncated in the review",
    "verify whether",
    "verify that",
    "verify the full file",
    "older callers",
)

_SPECULATIVE_PATTERNS = (
    re.compile(r"\bif this method does not exist\b"),
    re.compile(r"\bif the model does not define\b"),
    re.compile(r"\bif the model doesn't define\b"),
    re.compile(r"\bif [`_a-zA-Z0-9.()'-]+ returns a\b"),
)


def _extract_json_payload(text: str) -> Any:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()

    start_object = candidate.find("{")
    start_array = candidate.find("[")
    if start_array != -1 and (start_object == -1 or start_array < start_object):
        end_array = candidate.rfind("]")
        if end_array == -1:
            raise ValueError("No JSON array found.")
        return json.loads(candidate[start_array : end_array + 1])

    if start_object == -1:
        raise ValueError("No JSON object found.")
    end_object = candidate.rfind("}")
    if end_object == -1:
        raise ValueError("No JSON object found.")
    return json.loads(candidate[start_object : end_object + 1])


def _looks_like_code(path: str) -> bool:
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in {
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".go",
        ".java",
        ".rb",
        ".rs",
        ".sh",
    }


def _serialize_scope(scope: DiffScope, *, max_chars: int = 200_000) -> str:
    chunks: list[str] = []
    used = 0
    for changed_file in scope.files:
        chunk = [
            f"Path: {changed_file.path}",
            f"Status: {changed_file.status}",
            f"Additions: {changed_file.additions}",
            f"Deletions: {changed_file.deletions}",
        ]
        if changed_file.patch:
            chunk.append("Patch (unified diff, each line prefixed with its right-side line number):")
            chunk.append(_annotate_patch(changed_file.patch))
        if changed_file.contents:
            chunk.append("Full file contents (may be truncated):")
            chunk.append(changed_file.contents[:6000])
        rendered = "\n".join(chunk)
        if used + len(rendered) > max_chars:
            # Try to fit a truncated version instead of skipping entirely
            budget = max_chars - used
            if budget > 500:
                truncated_patch = changed_file.patch[:budget - 200] if changed_file.patch else ""
                chunk_trunc = [
                    f"Path: {changed_file.path}",
                    f"Status: {changed_file.status}",
                    f"Additions: {changed_file.additions}",
                    f"Deletions: {changed_file.deletions}",
                ]
                if truncated_patch:
                    chunk_trunc.append("Patch (unified diff — TRUNCATED to fit budget):")
                    chunk_trunc.append(_annotate_patch(truncated_patch))
                chunks.append("\n".join(chunk_trunc))
            break
        chunks.append(rendered)
        used += len(rendered)
    return "\n\n".join(chunks)
def _annotate_patch(patch: str) -> str:
    """Prefix each diff line with its right-side line number.

    Hunk headers and deleted lines (which have no right-side position) are
    left unnumbered so the LLM can read line numbers directly instead of
    counting from ``@@`` headers.
    """
    out: list[str] = []
    current_line = 0
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            if match:
                current_line = int(match.group(1))
            out.append(raw)
            continue
        if raw.startswith("-"):
            # Deleted lines have no right-side position
            out.append(f"       {raw}")
            continue
        # '+' (added) or ' ' (context) lines map to the right side
        out.append(f"L{current_line:<5d} {raw}")
        current_line += 1
    return "\n".join(out)


def _parse_diff_lines(patch: str) -> tuple[set[int], set[int]]:
    """Extract valid RIGHT-side line numbers from a unified diff.

    Returns (added_lines, context_lines) where added_lines are ``+`` lines
    and context_lines are unchanged lines shown in the diff.
    """
    added_lines: set[int] = set()
    context_lines: set[int] = set()
    current_line = 0
    for raw_line in patch.splitlines():
        if raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)", raw_line)
            if match:
                current_line = int(match.group(1))
            continue
        if raw_line.startswith("-"):
            continue
        if raw_line.startswith("+"):
            added_lines.add(current_line)
            current_line += 1
        else:
            context_lines.add(current_line)
            current_line += 1
    return added_lines, context_lines


def _snap_line_to_diff(
    line: int,
    added_lines: set[int],
    context_lines: set[int],
) -> int | None:
    """Snap a line number to the nearest added line in the diff.

    Prefers ``+`` (added) lines over context lines.  Falls back to the
    nearest context line only when no added line is within range.
    Returns ``None`` when no diff line is within 5 lines.
    """
    all_lines = added_lines | context_lines
    if not all_lines:
        return None
    if line in added_lines:
        return line
    # Try nearest added line first (within 5 lines)
    if added_lines:
        closest_added = min(added_lines, key=lambda v: abs(v - line))
        if abs(closest_added - line) <= 5:
            return closest_added
    # Fall back to any diff line
    if line in context_lines:
        return line
    closest = min(all_lines, key=lambda v: abs(v - line))
    if abs(closest - line) <= 5:
        return closest
    return None


def _normalize_finding_text(text: str) -> str:
    """Collapse whitespace and lowercase text for filtering and dedupe."""
    return " ".join(text.lower().split())


def _is_speculative_finding(body: str) -> bool:
    """Reject findings that explicitly depend on missing or unseen evidence."""
    normalized = _normalize_finding_text(body)
    if any(marker in normalized for marker in _SPECULATIVE_SUBSTRINGS):
        return True
    return any(pattern.search(normalized) for pattern in _SPECULATIVE_PATTERNS)


def _build_retrieval_query(pr: PullRequestContext, diff_scope: DiffScope) -> str:
    """Build a retrieval query for detailed review context."""
    lines = [pr.title, pr.body]
    for changed_file in diff_scope.files:
        lines.extend([
            changed_file.path,
            changed_file.patch or "",
        ])
    return "\n".join(filter(None, lines))


class CodeReviewer:
    """Generates focused review findings for risky code changes."""

    def __init__(
        self,
        bedrock_client: PromptClient,
        *,
        retriever: BedrockRetriever | None = None,
        retrieval_config: RetrievalConfig | None = None,
    ) -> None:
        self._bedrock = bedrock_client
        self._retriever = retriever
        self._retrieval_config = retrieval_config or RetrievalConfig()

    def classify_simple_change(self, files: list[ChangedFile]) -> bool:
        """Return ``True`` for changes that are likely trivial."""
        if not files:
            return True

        total_delta = sum(changed_file.additions + changed_file.deletions for changed_file in files)
        if total_delta <= 5:
            return True

        return all(not _looks_like_code(changed_file.path) for changed_file in files)

    def review(
        self,
        pr: PullRequestContext,
        diff_scope: DiffScope,
        config: ReviewerConfig,
    ) -> list[ReviewFinding]:
        """Review the selected diff scope with the configured heavy model."""
        if not diff_scope.files:
            return []

        retrieved_context = ""
        if self._retriever is not None:
            retrieved_context = self._retriever.render_for_prompt(
                _build_retrieval_query(pr, diff_scope),
                self._retrieval_config,
                section_title="Retrieved Valkey Context",
            )
        user_prompt = f"""Review this pull request and return only actionable findings.

PR title: {pr.title}
PR description:
{pr.body}

Review scope excerpts (patch/content may be truncated):
{_serialize_scope(diff_scope)}

{retrieved_context}

Return JSON in one of these shapes:
[
  {{
    "path": "relative/path",
    "line": <line number from the diff's +N side, or null if unsure>,
    "severity": "high|medium|low",
    "body": "single concrete finding"
  }}
]

or
{{ "findings": [ ... ] }}

CRITICAL rules:
- Each diff line is prefixed with its right-side line number (e.g. "L42"). The "line" field MUST be one of these prefixed numbers. If you cannot identify the exact line, set "line" to null.
- Only return findings with direct evidence in the shown patch/content excerpts.
- Do NOT speculate about array sizes, buffer lengths, variable values, or code structure that is not fully visible in the provided excerpts.
- Do not infer missing definitions from other files or from omitted parts of a file.
- Do not report that a file, diff, or workflow looks truncated.
- Do not ask maintainers to verify whether a symbol exists.
- Prefer one strongest finding per root cause; if unsure, return [].
- Do not emit generic praise.
"""
        response = self._bedrock.invoke(
            _SYSTEM_PROMPT,
            user_prompt,
            model_id=config.models.heavy_model_id,
            max_output_tokens=config.max_output_tokens,
            temperature=config.models.temperature,
        )

        try:
            payload = _extract_json_payload(response)
        except Exception as exc:
            raise ValueError("Unparseable review response") from exc

        raw_findings = payload.get("findings", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_findings, list):
            raise ValueError("Review response did not contain a findings list.")

        logger.info("LLM returned %d raw finding(s).", len(raw_findings))

        allowed_paths = {changed_file.path for changed_file in diff_scope.files}
        reviewable_files = {changed_file.path: changed_file for changed_file in diff_scope.files}
        findings: list[ReviewFinding] = []
        seen_keys: set[tuple[str, int | None, str]] = set()
        filtered_no_line = 0
        filtered_speculative = 0
        filtered_lgtm = 0
        filtered_path = 0
        for raw_finding in raw_findings:
            if not isinstance(raw_finding, dict):
                continue
            path = str(raw_finding.get("path", "")).strip()
            if not path or path not in allowed_paths:
                filtered_path += 1
                continue
            body = str(raw_finding.get("body", "")).strip()
            if not body:
                continue
            lowered = body.lower()
            if not config.review_comment_lgtm and (
                "lgtm" in lowered or "looks good" in lowered or "no issues" in lowered
            ):
                filtered_lgtm += 1
                continue
            line = raw_finding.get("line")
            if _is_speculative_finding(body):
                filtered_speculative += 1
                continue
            changed_file = reviewable_files[path]
            normalized_body = _normalize_finding_text(body)
            normalized_line = int(line) if isinstance(line, int) and line > 0 else None
            if normalized_line is not None and changed_file.patch:
                added_lines, context_lines = _parse_diff_lines(changed_file.patch)
                snapped = _snap_line_to_diff(normalized_line, added_lines, context_lines)
                if snapped != normalized_line:
                    logger.info(
                        "Line %d for %s snapped to %s (added=%d, context=%d lines in diff)",
                        normalized_line,
                        path,
                        snapped,
                        len(added_lines),
                        len(context_lines),
                    )
                normalized_line = snapped
            dedupe_key = (path, normalized_line, normalized_body)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            findings.append(
                ReviewFinding(
                    path=path,
                    line=normalized_line,
                    body=body,
                    severity=str(raw_finding.get("severity", "medium")).strip() or "medium",
                )
            )

        logger.info(
            "Finding filter stats: path=%d, lgtm=%d, speculative=%d, kept=%d",
            filtered_path,
            filtered_lgtm,
            filtered_speculative,
            len(findings),
        )

        return findings[: config.max_review_comments]

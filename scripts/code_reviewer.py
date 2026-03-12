"""Bedrock-backed detailed PR code review."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from scripts.bedrock_client import PromptClient
from scripts.bedrock_retriever import BedrockRetriever
from scripts.config import RetrievalConfig, ReviewerConfig
from scripts.models import ChangedFile, DiffScope, PullRequestContext, ReviewFinding

_SYSTEM_PROMPT = """You are a strict code reviewer.
Return only defect-oriented findings about correctness, regressions,
security, performance risks, or missing validation.
Do not include praise or generic approvals.
Return valid JSON only."""


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


def _serialize_scope(scope: DiffScope, *, max_chars: int = 18_000) -> str:
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
            chunk.append("Patch:")
            chunk.append(changed_file.patch[:1800])
        if changed_file.contents:
            chunk.append("Contents:")
            chunk.append(changed_file.contents[:1200])
        rendered = "\n".join(chunk)
        if used + len(rendered) > max_chars:
            break
        chunks.append(rendered)
        used += len(rendered)
    return "\n\n".join(chunks)


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

Review scope:
{_serialize_scope(diff_scope)}

{retrieved_context}

Return JSON in one of these shapes:
[
  {{
    "path": "relative/path",
    "line": 123 or null,
    "severity": "high|medium|low",
    "body": "single concrete finding"
  }}
]

or
{{ "findings": [ ... ] }}

Do not emit generic praise.
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

        allowed_paths = {changed_file.path for changed_file in diff_scope.files}
        findings: list[ReviewFinding] = []
        for raw_finding in raw_findings:
            if not isinstance(raw_finding, dict):
                continue
            path = str(raw_finding.get("path", "")).strip()
            if not path or path not in allowed_paths:
                continue
            body = str(raw_finding.get("body", "")).strip()
            if not body:
                continue
            lowered = body.lower()
            if not config.review_comment_lgtm and (
                "lgtm" in lowered or "looks good" in lowered or "no issues" in lowered
            ):
                continue
            line = raw_finding.get("line")
            findings.append(
                ReviewFinding(
                    path=path,
                    line=int(line) if isinstance(line, int) and line > 0 else None,
                    body=body,
                    severity=str(raw_finding.get("severity", "medium")).strip() or "medium",
                )
            )

        return findings[: config.max_review_comments]

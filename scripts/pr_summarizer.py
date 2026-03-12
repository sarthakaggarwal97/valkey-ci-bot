"""Bedrock-backed PR summarization."""

from __future__ import annotations

import json
from typing import Any

from scripts.bedrock_client import PromptClient
from scripts.config import ReviewerConfig
from scripts.models import PullRequestContext, SummaryResult

_SYSTEM_PROMPT = """You summarize pull requests for maintainers.
Focus on intent, architecture, and the concrete files changed.
Return valid JSON only."""


def _extract_json_payload(text: str) -> Any:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in model response.")
    return json.loads(candidate[start : end + 1])


def _render_file_context(pr: PullRequestContext, *, max_chars: int = 12_000) -> str:
    chunks: list[str] = []
    for changed_file in pr.files:
        chunk = [
            f"Path: {changed_file.path}",
            f"Status: {changed_file.status}",
            f"Additions: {changed_file.additions}",
            f"Deletions: {changed_file.deletions}",
        ]
        if changed_file.patch:
            chunk.append("Patch:")
            chunk.append(changed_file.patch[:1200])
        if changed_file.contents:
            chunk.append("Contents:")
            chunk.append(changed_file.contents[:1200])
        rendered = "\n".join(chunk)
        if sum(len(item) for item in chunks) + len(rendered) > max_chars:
            break
        chunks.append(rendered)
    return "\n\n".join(chunks)


class PRSummarizer:
    """Generates PR walkthroughs and optional release notes."""

    def __init__(self, bedrock_client: PromptClient) -> None:
        self._bedrock = bedrock_client

    def summarize(
        self,
        pr: PullRequestContext,
        config: ReviewerConfig,
    ) -> SummaryResult:
        """Generate a summary using the configured light Bedrock model."""
        release_notes_instruction = (
            "Set release_notes to null."
            if config.disable_release_notes
            else "Include concise release notes for end-user-visible changes."
        )
        user_prompt = f"""Summarize this pull request.

Title: {pr.title}
Description:
{pr.body}

Changed files:
{_render_file_context(pr)}

Return JSON with this schema:
{{
  "walkthrough": "short maintainer-facing summary",
  "file_groups_markdown": "markdown bullets grouped by theme",
  "release_notes": "markdown or null"
}}

{release_notes_instruction}
"""
        response = self._bedrock.invoke(
            _SYSTEM_PROMPT,
            user_prompt,
            model_id=config.models.light_model_id,
            max_output_tokens=config.max_output_tokens,
            temperature=config.models.temperature,
        )
        try:
            payload = _extract_json_payload(response)
        except Exception:
            return SummaryResult(
                walkthrough=response.strip(),
                file_groups_markdown="",
                release_notes=None,
            )

        return SummaryResult(
            walkthrough=str(payload.get("walkthrough", "")).strip(),
            file_groups_markdown=str(
                payload.get("file_groups_markdown", "")
            ).strip(),
            release_notes=(
                str(payload["release_notes"]).strip()
                if payload.get("release_notes") is not None
                else None
            ),
        )

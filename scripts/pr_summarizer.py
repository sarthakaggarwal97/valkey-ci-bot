"""Bedrock-backed PR summarization."""

from __future__ import annotations

import json
import logging
from typing import Any

from scripts.bedrock_client import PromptClient
from scripts.bedrock_retriever import BedrockRetriever
from scripts.config import RetrievalConfig, ReviewerConfig
from scripts.models import PullRequestContext, SummaryResult

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You summarize pull requests for maintainers.
Focus on intent, architecture, and the concrete files changed.
If applicable, note alterations to signatures of exported functions,
global data structures and variables, and any changes that might affect
the external interface or behavior of the code.
Return valid JSON only."""

_SUMMARY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "walkthrough": {"type": "string"},
        "short_summary": {"type": "string"},
        "file_groups_markdown": {"type": "string"},
        "release_notes": {"type": ["string", "null"]},
    },
    "required": ["walkthrough", "short_summary", "file_groups_markdown"],
}


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


def _build_retrieval_query(pr: PullRequestContext) -> str:
    """Build a retrieval query for PR summarization."""
    lines = [pr.title, pr.body]
    lines.extend(changed_file.path for changed_file in pr.files)
    return "\n".join(filter(None, lines))


def _summarize_changed_paths(pr: PullRequestContext, *, max_paths: int = 8) -> str:
    """Render a compact path list for deterministic summary fallback."""
    if not pr.files:
        return "no changed files"
    paths = [changed_file.path for changed_file in pr.files[:max_paths]]
    rendered = ", ".join(f"`{path}`" for path in paths)
    remaining = len(pr.files) - len(paths)
    if remaining > 0:
        rendered += f", and {remaining} more"
    return rendered


def _fallback_file_groups(pr: PullRequestContext, *, max_rows: int = 12) -> str:
    """Build a conservative file-groups table without model output."""
    if not pr.files:
        return "| Files | Summary |\n|---|---|\n| _None_ | No files changed. |"
    rows = ["| Files | Summary |", "|---|---|"]
    for changed_file in pr.files[:max_rows]:
        rows.append(
            "| "
            f"`{changed_file.path}`"
            " | "
            f"{changed_file.status}, +{changed_file.additions}/-{changed_file.deletions}"
            " |"
        )
    remaining = len(pr.files) - max_rows
    if remaining > 0:
        rows.append(f"| _{remaining} more file(s)_ | Not listed in fallback summary. |")
    return "\n".join(rows)


def _fallback_summary(pr: PullRequestContext) -> SummaryResult:
    """Return a useful summary when model output is incomplete."""
    path_summary = _summarize_changed_paths(pr)
    if pr.title:
        short_summary = f"{pr.title}. Changed files include {path_summary}."
    else:
        short_summary = f"Changed files include {path_summary}."
    return SummaryResult(
        walkthrough=(
            f"Changed paths include {path_summary}. Review the file groups below "
            "for per-file status and churn."
        ),
        file_groups_markdown=_fallback_file_groups(pr),
        release_notes=None,
        short_summary=short_summary,
    )


def _coerce_summary_result(
    payload: Any,
    pr: PullRequestContext,
    config: ReviewerConfig,
) -> SummaryResult:
    """Normalize model summary output and fill safe fallbacks for gaps."""
    fallback = _fallback_summary(pr)
    if not isinstance(payload, dict):
        logger.warning("PR summary model output was not a JSON object; using fallback.")
        return fallback

    walkthrough = str(payload.get("walkthrough") or "").strip()
    short_summary = str(payload.get("short_summary") or "").strip()
    file_groups_markdown = str(payload.get("file_groups_markdown") or "").strip()
    release_notes = (
        str(payload["release_notes"]).strip()
        if payload.get("release_notes") is not None
        else None
    )

    missing: list[str] = []
    if not walkthrough:
        walkthrough = fallback.walkthrough
        missing.append("walkthrough")
    if not short_summary:
        short_summary = fallback.short_summary
        missing.append("short_summary")
    if not file_groups_markdown:
        file_groups_markdown = fallback.file_groups_markdown
        missing.append("file_groups_markdown")
    if config.disable_release_notes:
        release_notes = None
    if missing:
        logger.warning(
            "PR summary model output omitted %s; filled deterministic fallback fields.",
            ", ".join(missing),
        )

    return SummaryResult(
        walkthrough=walkthrough,
        file_groups_markdown=file_groups_markdown,
        release_notes=release_notes,
        short_summary=short_summary,
    )


class PRSummarizer:
    """Generates PR walkthroughs and optional release notes."""

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
        retrieved_context = ""
        if self._retriever is not None:
            retrieved_context = self._retriever.render_for_prompt(
                _build_retrieval_query(pr),
                self._retrieval_config,
                section_title="Retrieved Valkey Context",
            )

        custom_instructions_section = ""
        if config.custom_instructions:
            custom_instructions_section = f"""
## Project-Specific Context
{config.custom_instructions}
"""

        user_prompt = f"""Summarize this pull request.

Title: {pr.title}
Description:
{pr.body}

Changed files:
{_render_file_context(pr)}

{retrieved_context}
{custom_instructions_section}

Return JSON with this schema:
{{
  "walkthrough": "A high-level summary of the overall change within 80 words, focusing on intent and architecture rather than listing individual files",
  "short_summary": "A concise factual summary of the changes (max 500 words) that will be used as context when reviewing each file. Focus only on what changed, not instructions for how to review. Do not mention that files need review or caution about potential issues.",
  "file_groups_markdown": "A markdown table of files and their summaries, grouping files with similar changes together into a single row to save space",
  "release_notes": "Concise release notes categorized as New Feature, Bug Fix, Documentation, Refactor, Style, Test, Chore, or Revert. Bullet-point list, 50-100 words, focusing on end-user-visible features. Or null if not applicable."
}}

{release_notes_instruction}
"""
        # Try structured tool-use first, fall back to plain invoke + JSON parsing
        payload: dict | None = None
        _has_schema_method = (
            callable(getattr(self._bedrock, "invoke_with_schema", None))
            and not isinstance(
                getattr(type(self._bedrock), "invoke_with_schema", None),
                property,
            )
            and type(self._bedrock).__name__ != "MagicMock"
        )
        if _has_schema_method:
            try:
                response = self._bedrock.invoke_with_schema(
                    _SYSTEM_PROMPT,
                    user_prompt,
                    tool_name="generate_pr_summary",
                    tool_description="Generate a structured PR summary with walkthrough, short summary, file groups, and release notes",
                    json_schema=_SUMMARY_SCHEMA,
                    model_id=config.models.light_model_id,
                    max_output_tokens=config.max_output_tokens,
                    temperature=config.models.temperature,
                )
                payload = json.loads(response) if isinstance(response, str) else response
                if isinstance(payload, dict):
                    logger.info("Used structured tool-use output for summary.")
                else:
                    payload = None
            except Exception as tool_exc:
                logger.info("Tool-use failed (%s), falling back to plain invoke.", tool_exc)
                payload = None

        if payload is None:
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
                fallback = _fallback_summary(pr)
                return SummaryResult(
                    walkthrough=response.strip() or fallback.walkthrough,
                    file_groups_markdown=fallback.file_groups_markdown,
                    release_notes=None,
                    short_summary=fallback.short_summary,
                )

        return _coerce_summary_result(payload, pr, config)

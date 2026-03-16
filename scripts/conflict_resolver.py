"""LLM-based conflict resolver for the Backport Agent pipeline.

Uses Amazon Bedrock to resolve merge conflicts file-by-file, with
whitespace-only conflict detection, retry logic, and C syntax validation.
"""

from __future__ import annotations

import logging
import re

from scripts.backport_models import (
    BackportConfig,
    BackportPRContext,
    ConflictedFile,
    ResolutionResult,
)
from scripts.backport_utils import (
    has_conflict_markers,
    is_whitespace_only_conflict,
    validate_c_syntax,
)
from scripts.bedrock_client import BedrockClient

logger = logging.getLogger(__name__)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences that LLMs sometimes wrap around output."""
    # Match ```<optional lang>\n ... \n``` wrapping the entire response
    stripped = re.sub(
        r"^```[a-zA-Z]*\s*\n(.*?)```\s*$",
        r"\1",
        text.strip(),
        flags=re.DOTALL,
    )
    return stripped


_SYSTEM_PROMPT = (
    "You are a code merge conflict resolver for the Valkey project "
    "(a C codebase). Return ONLY raw file content — never wrap your "
    "response in markdown code fences (``` or ```c)."
)


class ConflictResolver:
    """Resolve cherry-pick merge conflicts using an LLM.

    Each conflicting file is processed independently.  Whitespace-only
    conflicts are resolved without an LLM call.  The resolver respects
    a per-backport token budget and a maximum conflicting-file count.
    """

    def __init__(
        self,
        bedrock_client: BedrockClient,
        config: BackportConfig,
    ) -> None:
        self._bedrock = bedrock_client
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_conflicts(
        self,
        conflicting_files: list[ConflictedFile],
        pr_context: BackportPRContext,
        token_budget: int,
    ) -> list[ResolutionResult]:
        """Resolve each conflicting file independently.

        * Whitespace-only conflicts are resolved without LLM calls.
        * If the number of conflicting files exceeds
          ``config.max_conflicting_files``, **all** files are left
          unresolved.
        * Processing stops when the cumulative token usage exceeds
          *token_budget*.

        Returns one :class:`ResolutionResult` per input file.
        """
        results: list[ResolutionResult] = []

        if len(conflicting_files) > self._config.max_conflicting_files:
            logger.warning(
                "Conflict count %d exceeds max_conflicting_files (%d). "
                "Skipping all resolutions.",
                len(conflicting_files),
                self._config.max_conflicting_files,
            )
            for conflict in conflicting_files:
                results.append(
                    ResolutionResult(
                        path=conflict.path,
                        resolved_content=None,
                        resolution_summary=(
                            "Skipped: conflict count exceeds limit "
                            f"({len(conflicting_files)} > "
                            f"{self._config.max_conflicting_files})"
                        ),
                        tokens_used=0,
                        attempts=0,
                    )
                )
            return results

        tokens_used_total = 0

        for conflict in conflicting_files:
            # Whitespace-only shortcut — no LLM needed.
            if is_whitespace_only_conflict(
                conflict.target_branch_content,
                conflict.source_branch_content,
            ):
                logger.info(
                    "File %s has whitespace-only conflict; resolving with "
                    "target branch version.",
                    conflict.path,
                )
                results.append(
                    ResolutionResult(
                        path=conflict.path,
                        resolved_content=conflict.target_branch_content,
                        resolution_summary="whitespace-only",
                        tokens_used=0,
                        attempts=0,
                    )
                )
                continue

            # Token budget check — stop resolving further files.
            if tokens_used_total >= token_budget:
                logger.warning(
                    "Token budget exhausted (%d / %d). Skipping %s.",
                    tokens_used_total,
                    token_budget,
                    conflict.path,
                )
                results.append(
                    ResolutionResult(
                        path=conflict.path,
                        resolved_content=None,
                        resolution_summary="Skipped: token budget exhausted",
                        tokens_used=0,
                        attempts=0,
                    )
                )
                continue

            result = self._resolve_single_file(
                conflict,
                pr_context,
                max_retries=self._config.max_conflict_retries,
            )
            tokens_used_total += result.tokens_used
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_single_file(
        self,
        conflict: ConflictedFile,
        pr_context: BackportPRContext,
        max_retries: int,
    ) -> ResolutionResult:
        """Attempt to resolve a single file, retrying on remaining markers.

        The initial attempt plus up to *max_retries* additional attempts
        are made.  On each retry the prompt is augmented with feedback
        about the remaining conflict markers.

        After a successful marker removal the resolved content is
        validated with :func:`validate_c_syntax`.  If the syntax check
        fails the file is left unresolved.
        """
        total_attempts = 1 + max_retries
        tokens_used = 0
        resolved_text: str | None = None
        feedback: str | None = None

        for attempt in range(1, total_attempts + 1):
            system_prompt, user_prompt = self._build_prompt(
                conflict, pr_context, feedback=feedback,
            )

            # Rough token estimate: len(text) // 4
            prompt_tokens = (len(system_prompt) + len(user_prompt)) // 4

            try:
                response = self._bedrock.invoke(
                    system_prompt,
                    user_prompt,
                    model_id=self._config.bedrock_model_id,
                )
            except Exception:
                logger.exception(
                    "Bedrock invocation failed for %s (attempt %d/%d).",
                    conflict.path,
                    attempt,
                    total_attempts,
                )
                # Estimate tokens for the failed attempt anyway.
                tokens_used += prompt_tokens
                break

            # Estimate tokens for this round (prompt + response).
            response_tokens = len(response) // 4
            tokens_used += prompt_tokens + response_tokens

            # Strip markdown code fences the LLM may wrap around the output.
            response = _strip_code_fences(response)

            if not has_conflict_markers(response):
                resolved_text = response
                break

            # Still has markers — prepare feedback for the next attempt.
            logger.warning(
                "Resolved content for %s still contains conflict markers "
                "(attempt %d/%d).",
                conflict.path,
                attempt,
                total_attempts,
            )
            feedback = (
                "The previous resolution still contains conflict markers "
                "(<<<<<<<, =======, >>>>>>>). Please resolve ALL conflict "
                "markers and return the complete file without any markers."
            )

        # Validate C syntax if we got a clean resolution.
        if resolved_text is not None:
            if not validate_c_syntax(resolved_text):
                logger.warning(
                    "Resolved content for %s failed C syntax validation. "
                    "Leaving file unresolved.",
                    conflict.path,
                )
                return ResolutionResult(
                    path=conflict.path,
                    resolved_content=None,
                    resolution_summary=(
                        "LLM resolution failed C syntax validation"
                    ),
                    tokens_used=tokens_used,
                    attempts=total_attempts,
                )

            return ResolutionResult(
                path=conflict.path,
                resolved_content=resolved_text,
                resolution_summary="Resolved by LLM",
                tokens_used=tokens_used,
                attempts=attempt,  # noqa: F821 — loop variable survives
            )

        # All retries exhausted or Bedrock error.
        return ResolutionResult(
            path=conflict.path,
            resolved_content=None,
            resolution_summary=(
                "LLM failed to remove conflict markers after "
                f"{total_attempts} attempt(s)"
            ),
            tokens_used=tokens_used,
            attempts=total_attempts,
        )

    def _build_prompt(
        self,
        conflict: ConflictedFile,
        pr_context: BackportPRContext,
        *,
        feedback: str | None = None,
    ) -> tuple[str, str]:
        """Build the (system_prompt, user_prompt) pair for the LLM.

        The user prompt includes:
        * Source PR title, body, and diff
        * The file with conflict markers
        * The original target branch version
        * The source branch version
        * Instruction to resolve by applying the PR's intent to the
          target branch code

        On retry, *feedback* is appended to the user prompt.
        """
        system_prompt = _SYSTEM_PROMPT

        parts: list[str] = [
            "## Source Pull Request",
            f"**Title:** {pr_context.source_pr_title}",
            "",
            f"**Body:**\n{pr_context.source_pr_body}",
            "",
            f"**Diff:**\n```\n{pr_context.source_pr_diff}\n```",
            "",
            f"## Conflicting File: `{conflict.path}`",
            "",
            "### File with conflict markers",
            f"```\n{conflict.content_with_markers}\n```",
            "",
            "### Target branch version (before cherry-pick)",
            f"```\n{conflict.target_branch_content}\n```",
            "",
            "### Source branch version",
            f"```\n{conflict.source_branch_content}\n```",
            "",
            "## Instructions",
            "Resolve the merge conflicts in the file above by applying the "
            "intent of the source pull request to the target branch version "
            "of the code. Return ONLY the complete resolved file content "
            "with no conflict markers (<<<<<<<, =======, >>>>>>>). "
            "Do NOT wrap your response in markdown code fences. "
            "Preserve the coding style and conventions of the target branch "
            "version. Limit your changes to the conflicting regions only.",
        ]

        if feedback:
            parts.append("")
            parts.append(f"## Feedback\n{feedback}")

        user_prompt = "\n".join(parts)
        return system_prompt, user_prompt

"""Fix generation using Amazon Bedrock.

Sends root cause analysis and relevant source files to Bedrock requesting
a unified diff patch. Validates the patch applies cleanly, enforces scope
limits, and retries on failure.
"""

from __future__ import annotations

import logging
import re
import subprocess

from scripts.bedrock_client import BedrockClient, BedrockError
from scripts.config import BotConfig
from scripts.models import RootCauseReport

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an expert C/C++ developer. Your task is to generate a code fix \
as a unified diff patch that can be applied with `git apply`.

Respond ONLY with the unified diff (no markdown fences, no explanation). \
The diff must:
- Use the standard unified diff format (--- a/file, +++ b/file, @@ hunks)
- Be applicable with `git apply`
- Only modify files relevant to the root cause
- Be minimal — change only what is necessary to fix the issue
"""

# Regex to find files modified in a unified diff (--- a/path or +++ b/path)
_DIFF_FILE_RE = re.compile(r"^(?:---|\+\+\+) [ab]/(.+)$", re.MULTILINE)


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from a response if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
        cleaned = cleaned.strip()
    return cleaned


def _count_patch_files(diff: str) -> set[str]:
    """Extract the set of files modified in a unified diff."""
    files = set(_DIFF_FILE_RE.findall(diff))
    # Filter out /dev/null which appears for new/deleted files
    files.discard("/dev/null")
    return files


def _meets_confidence_threshold(confidence: str, threshold: str) -> bool:
    """Return True when a confidence level meets the configured threshold."""
    rank = {"low": 0, "medium": 1, "high": 2}
    return rank.get(confidence, -1) >= rank.get(threshold, 1)


def _build_user_prompt(
    root_cause: RootCauseReport,
    source_files: dict[str, str],
    apply_error: str | None = None,
    validation_error: str | None = None,
) -> str:
    """Build the user prompt for fix generation."""
    parts: list[str] = []

    parts.append("## Root Cause Analysis")
    parts.append(f"Description: {root_cause.description}")
    parts.append(f"Confidence: {root_cause.confidence}")
    parts.append(f"Rationale: {root_cause.rationale}")
    if root_cause.files_to_change:
        parts.append(f"Files to change: {', '.join(root_cause.files_to_change)}")

    if source_files:
        parts.append("\n## Source Files")
        for path, content in source_files.items():
            parts.append(f"\n### {path}\n```\n{content}\n```")

    if apply_error:
        parts.append("\n## Previous Attempt Failed")
        parts.append(
            "The previous patch did not apply cleanly. "
            "Please generate a corrected patch."
        )
        parts.append(f"Error:\n{apply_error}")

    if validation_error:
        parts.append("\n## Validation Failure")
        parts.append(
            "The previous patch was applied but validation (build/test) failed. "
            "Please generate a corrected patch that addresses the validation failure."
        )
        parts.append(f"Validation output:\n{validation_error}")

    return "\n".join(parts)


def _validate_patch_applies(diff: str) -> tuple[bool, str]:
    """Check if a patch applies cleanly using `git apply --check`.

    Returns (success, error_output).
    """
    try:
        result = subprocess.run(
            ["git", "apply", "--check"],
            input=diff,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return False, str(exc)


class FixGenerator:
    """Bedrock-powered patch generation for CI failure fixes.

    Accepts a BedrockClient and BotConfig in its constructor.
    Generates unified diff patches, validates them, and enforces
    scope and retry limits.
    """

    def __init__(self, bedrock_client: BedrockClient, config: BotConfig):
        self._bedrock = bedrock_client
        self._config = config
        self.last_attempt_count = 0

    def generate(
        self,
        root_cause: RootCauseReport,
        source_files: dict[str, str],
        validation_error: str | None = None,
    ) -> str | None:
        """Generate a unified diff patch for the given root cause.

        Args:
            root_cause: The root cause analysis report.
            source_files: Mapping of file path to file content for
                relevant source files.
            validation_error: Optional validation failure output from a
                previous attempt, included as additional context.

        Returns:
            The unified diff string, or None if generation fails or
            is skipped.
        """
        self.last_attempt_count = 0
        # Skip generation for low confidence
        if not _meets_confidence_threshold(
            root_cause.confidence, self._config.confidence_threshold,
        ):
            logger.info(
                "Skipping fix generation: confidence '%s' does not meet threshold '%s'.",
                root_cause.confidence, self._config.confidence_threshold,
            )
            return None

        logger.info(
            "Fix generation started: confidence=%s, files_to_change=%s",
            root_cause.confidence, root_cause.files_to_change,
        )
        max_attempts = self._config.max_retries_fix + 1  # initial + retries
        apply_error: str | None = None

        for attempt in range(max_attempts):
            self.last_attempt_count = attempt + 1
            # Build prompt (include apply error feedback on retries)
            user_prompt = _build_user_prompt(
                root_cause, source_files, apply_error,
                validation_error=validation_error if attempt == 0 else None,
            )

            # Call Bedrock
            try:
                raw_response = self._bedrock.invoke(
                    _SYSTEM_PROMPT, user_prompt
                )
            except BedrockError as exc:
                logger.error(
                    "Bedrock error during fix generation (attempt %d/%d): %s",
                    attempt + 1, max_attempts, exc,
                )
                return None

            # Parse diff from response
            diff = _strip_markdown_fences(raw_response)
            if not diff:
                logger.warning(
                    "Empty diff from Bedrock (attempt %d/%d).",
                    attempt + 1, max_attempts,
                )
                apply_error = "Empty diff returned."
                continue

            # Check patch scope — reject if too many files
            modified_files = _count_patch_files(diff)
            if len(modified_files) > self._config.max_patch_files:
                logger.warning(
                    "Patch modifies %d files (limit %d). Rejecting.",
                    len(modified_files), self._config.max_patch_files,
                )
                apply_error = (
                    f"Patch modified {len(modified_files)} files which exceeds the "
                    f"limit of {self._config.max_patch_files}."
                )
                continue

            if root_cause.files_to_change:
                unexpected_files = modified_files.difference(root_cause.files_to_change)
                if unexpected_files:
                    logger.warning(
                        "Patch modified files outside the allowed scope: %s",
                        ", ".join(sorted(unexpected_files)),
                    )
                    apply_error = (
                        "Patch modified files outside the allowed scope: "
                        f"{', '.join(sorted(unexpected_files))}."
                    )
                    continue

            # Validate patch applies cleanly
            success, error_output = _validate_patch_applies(diff)
            if success:
                logger.info(
                    "Patch generated successfully on attempt %d/%d "
                    "(%d file(s) modified).",
                    attempt + 1, max_attempts, len(modified_files),
                )
                return diff

            # Apply failed — retry with feedback
            logger.warning(
                "Patch apply failed (attempt %d/%d): %s",
                attempt + 1, max_attempts, error_output,
            )
            apply_error = error_output

        logger.error(
            "Fix generation failed after %d attempts.", max_attempts
        )
        return None

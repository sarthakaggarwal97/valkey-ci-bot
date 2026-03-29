"""Fix generation using Amazon Bedrock.

Sends root cause analysis and relevant source files to Bedrock requesting
a unified diff patch. Validates the patch applies cleanly, enforces scope
limits, and retries on failure.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

from scripts.bedrock_client import BedrockClient, BedrockError
from scripts.bedrock_retriever import BedrockRetriever
from scripts.config import BotConfig, RetrievalConfig
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
    retrieved_context: str = "",
    apply_error: str | None = None,
    validation_error: str | None = None,
    failed_hypotheses: list[str] | None = None,
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

    if retrieved_context:
        parts.append(f"\n{retrieved_context}")

    if failed_hypotheses:
        parts.append("\n## Previous Failed Approaches")
        parts.append(
            "Avoid repeating these prior ideas unless the new evidence directly "
            "contradicts them."
        )
        for item in failed_hypotheses:
            parts.append(f"- {item}")

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


def _build_retrieval_query(
    root_cause: RootCauseReport,
    source_files: dict[str, str],
) -> str:
    """Build a retrieval query for broader fix-generation context."""
    lines = [
        root_cause.description,
        root_cause.rationale,
        " ".join(root_cause.files_to_change),
        " ".join(source_files.keys()),
    ]
    return "\n".join(filter(None, lines))


def _validate_patch_applies(diff: str, source_files: dict[str, str]) -> tuple[bool, str]:
    """Check if a patch applies cleanly using `git apply --check`.

    Returns (success, error_output).
    """
    try:
        with tempfile.TemporaryDirectory(prefix="ci-bot-patch-check-") as tmpdir:
            work_dir = Path(tmpdir)
            subprocess.run(
                ["git", "init", "-q"],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            for file_path, contents in source_files.items():
                target = work_dir / file_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents)

            result = subprocess.run(
                ["git", "apply", "--check"],
                input=diff,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(work_dir),
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

    def __init__(
        self,
        bedrock_client: BedrockClient,
        config: BotConfig,
        *,
        github_client: "object | None" = None,
        repo_full_name: str = "",
    ):
        self._bedrock = bedrock_client
        self._config = config
        self.last_attempt_count = 0
        self._retriever: BedrockRetriever | None = None
        self._retrieval_config = RetrievalConfig()
        self._github_client = github_client
        self._repo_full_name = repo_full_name

    def with_retriever(
        self,
        retriever: BedrockRetriever | None,
        retrieval_config: RetrievalConfig | None,
    ) -> FixGenerator:
        """Attach optional retrieval support to fix generation."""
        self._retriever = retriever
        self._retrieval_config = retrieval_config or RetrievalConfig()
        return self

    def generate(
        self,
        root_cause: RootCauseReport,
        source_files: dict[str, str],
        validation_error: str | None = None,
        failed_hypotheses: list[str] | None = None,
        *,
        repo_ref: str | None = None,
    ) -> str | None:
        """Generate a unified diff patch for the given root cause.

        Args:
            root_cause: The root cause analysis report.
            source_files: Mapping of file path to file content for
                relevant source files.
            validation_error: Optional validation failure output from a
                previous attempt, included as additional context.
            repo_ref: Optional Git ref or commit SHA used when the
                agentic path fetches additional repository files.

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

        # Try agentic generation first
        agentic_diff = self._generate_agentic(
            root_cause,
            source_files,
            validation_error=validation_error,
            repo_ref=repo_ref,
        )
        if agentic_diff is not None:
            self.last_attempt_count = 1
            return agentic_diff

        max_attempts = self._config.max_retries_fix + 1  # initial + retries
        apply_error: str | None = None
        retrieved_context = ""
        if self._retriever is not None:
            retrieved_context = self._retriever.render_for_prompt(
                _build_retrieval_query(root_cause, source_files),
                self._retrieval_config,
                section_title="Retrieved Valkey Context",
            )

        for attempt in range(max_attempts):
            self.last_attempt_count = attempt + 1
            # Build prompt (include apply error feedback on retries)
            user_prompt = _build_user_prompt(
                root_cause, source_files, retrieved_context, apply_error,
                validation_error=validation_error if attempt == 0 else None,
                failed_hypotheses=failed_hypotheses,
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
            effective_limit = (
                self._config.max_patch_files_override
                if self._config.max_patch_files_override is not None
                and self._config.max_patch_files_override > 0
                else self._config.max_patch_files
            )
            if len(modified_files) > effective_limit:
                logger.warning(
                    "Patch modifies %d files (limit %d). Rejecting.",
                    len(modified_files), effective_limit,
                )
                apply_error = (
                    f"Patch modified {len(modified_files)} files which exceeds the "
                    f"limit of {effective_limit}."
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
            success, error_output = _validate_patch_applies(diff, source_files)
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

    def _generate_agentic(
        self,
        root_cause: RootCauseReport,
        source_files: dict[str, str],
        validation_error: str | None = None,
        failed_hypotheses: list[str] | None = None,
        *,
        repo_ref: str | None = None,
    ) -> str | None:
        """Try to generate a fix using the agentic tool-use loop.

        Returns a unified diff string on success, or None to fall back.
        """
        if self._github_client is None or not self._repo_full_name:
            return None

        from scripts.code_reviewer import (
            ReviewToolHandler,
            _GET_FILE_TOOL,
            _LIST_FILES_TOOL,
            _SEARCH_CODE_TOOL,
        )

        _SUBMIT_FIX_TOOL: dict = {
            "toolSpec": {
                "name": "submit_fix",
                "description": (
                    "Submit the unified diff patch that fixes the issue. "
                    "The diff must use standard unified diff format "
                    "(--- a/file, +++ b/file, @@ hunks) and be applicable "
                    "with `git apply`."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "diff": {
                                "type": "string",
                                "description": "The unified diff patch.",
                            },
                        },
                        "required": ["diff"],
                    },
                },
            },
        }

        retrieved_context = ""
        if self._retriever is not None:
            retrieved_context = self._retriever.render_for_prompt(
                _build_retrieval_query(root_cause, source_files),
                self._retrieval_config,
                section_title="Retrieved Valkey Context",
            )

        user_prompt = _build_user_prompt(
            root_cause, source_files, retrieved_context, None,
            validation_error=validation_error,
            failed_hypotheses=failed_hypotheses,
        )
        user_prompt += (
            "\n\nYou have tools to fetch additional files from the repository "
            "if you need more context to generate the fix. Use get_file to "
            "read source files, headers, tests, or configs. Use search_code "
            "to find function definitions, callers, or usages. When ready, "
            "call submit_fix with the unified diff."
        )

        tool_handler = ReviewToolHandler(
            github_client=self._github_client,
            repo_name=self._repo_full_name,
            head_sha=repo_ref or "HEAD",
            max_fetches=8,
        )

        tools = [_GET_FILE_TOOL, _LIST_FILES_TOOL, _SEARCH_CODE_TOOL, _SUBMIT_FIX_TOOL]

        import json as _json
        try:
            response = self._bedrock.converse_with_tools(
                _SYSTEM_PROMPT,
                user_prompt,
                tools=tools,
                tool_handler=tool_handler,
                terminal_tool="submit_fix",
                max_turns=20,
            )
        except Exception as exc:
            logger.warning("Agentic fix generation failed: %s. Falling back.", exc)
            return None

        try:
            payload = _json.loads(response) if isinstance(response, str) else response
        except Exception:
            payload = {}

        diff = payload.get("diff", "") if isinstance(payload, dict) else str(payload)
        diff = _strip_markdown_fences(diff)

        if not diff:
            return None

        # Validate the patch
        modified_files = _count_patch_files(diff)
        effective_limit = (
            self._config.max_patch_files_override
            if self._config.max_patch_files_override is not None
            and self._config.max_patch_files_override > 0
            else self._config.max_patch_files
        )
        if len(modified_files) > effective_limit:
            logger.warning("Agentic patch modifies too many files (%d).", len(modified_files))
            return None

        if root_cause.files_to_change:
            unexpected_files = modified_files.difference(root_cause.files_to_change)
            if unexpected_files:
                logger.warning(
                    "Agentic patch modified files outside the allowed scope: %s",
                    ", ".join(sorted(unexpected_files)),
                )
                return None

        success, error_output = _validate_patch_applies(diff, source_files)
        if success:
            logger.info("Agentic fix generation succeeded (%d file(s)).", len(modified_files))
            return diff

        logger.warning("Agentic patch failed to apply: %s", error_output)
        return None

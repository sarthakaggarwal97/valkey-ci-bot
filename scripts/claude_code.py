"""Thin wrapper around the Claude Code CLI.

Replaces the custom Bedrock tool-use loops (root_cause_analyzer,
fix_generator, code_reviewer) with a single subprocess call to
``claude --print``. Claude Code handles file reading, code search,
and diff generation natively.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

_DEFAULT_CLAUDE_MODEL = "opus"
_DEFAULT_BEDROCK_OPUS_MODEL = "us.anthropic.claude-opus-4-7"
_DIFF_FENCE_RE = re.compile(
    r"```(?:diff|patch)?\n(---\s.+?)\n```", re.DOTALL
)
_RAW_DIFF_RE = re.compile(
    r"^(---\s+a/.+?)(?=\n(?:[^-+ @\\]|$)|\Z)", re.DOTALL | re.MULTILINE
)


def run_claude_code(
    prompt: str,
    *,
    cwd: str | None = None,
    timeout: int = 600,
    model: str | None = _DEFAULT_CLAUDE_MODEL,
    effort: str | None = "high",
    max_turns: int = 80,
    allowed_tools: str = "Read,Edit,MultiEdit,Write,Bash,Glob,Grep",
) -> tuple[str, str, int]:
    """Run claude CLI and return (stdout, stderr, exit_code).

    Requires ``claude`` on PATH and Bedrock credentials in the
    environment (CLAUDE_CODE_USE_BEDROCK=1 + AWS creds).
    """
    env = {**os.environ}
    env["CLAUDE_CODE_USE_BEDROCK"] = "1"
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = _DEFAULT_BEDROCK_OPUS_MODEL
    if "AWS_REGION" not in env:
        env["AWS_REGION"] = "us-east-1"

    cmd = [
        "claude", "--print",
        "--max-turns", str(max_turns),
        "--allowedTools", allowed_tools,
    ]
    if model:
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])

    logger.info("Running claude: cwd=%s, timeout=%d, prompt=%s…", cwd, timeout, prompt[:120])
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            env=env,
        )
        logger.info(
            "Claude exited %d (%d chars stdout, %d chars stderr).",
            result.returncode, len(result.stdout), len(result.stderr),
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        logger.error("Claude timed out after %ds.", timeout)
        return "", f"timeout after {timeout}s", 1
    except FileNotFoundError:
        logger.error("claude CLI not found on PATH.")
        return "", "claude not found", 127


def extract_diff(claude_output: str) -> str | None:
    """Extract a unified diff from Claude's output.

    Tries fenced ```diff blocks first, then raw --- a/ patterns.
    Returns None if no diff found.
    """
    # Try fenced diff block
    m = _DIFF_FENCE_RE.search(claude_output)
    if m:
        return m.group(1).strip()

    # Try raw diff starting with --- a/
    m = _RAW_DIFF_RE.search(claude_output)
    if m:
        return m.group(1).strip()

    return None

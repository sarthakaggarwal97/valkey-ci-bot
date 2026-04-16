"""Shared text processing utilities."""

from __future__ import annotations

import re

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from LLM output if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()
    return cleaned


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_ESCAPE_RE.sub("", text)

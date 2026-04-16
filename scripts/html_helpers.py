"""Shared HTML rendering helpers used across dashboard modules."""

from __future__ import annotations

import html as html_lib

from scripts.json_helpers import safe_str


class SafeHtml(str):
    """Marker for trusted HTML assembled in this module."""


def safe_html(value: str) -> SafeHtml:
    """Mark a string as trusted HTML."""
    return SafeHtml(value)


def html_escape(value: object) -> str:
    """Escape a value for safe HTML text content."""
    return html_lib.escape(safe_str(value), quote=False)


def html_attr(value: object) -> str:
    """Escape a value for safe use in an HTML attribute."""
    return html_lib.escape(safe_str(value), quote=True)


def html_cell(value: object) -> str:
    """Render a table cell value — pass through SafeHtml, escape everything else."""
    if isinstance(value, SafeHtml):
        return str(value)
    return html_escape(value)

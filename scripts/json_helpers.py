"""Shared JSON safe-access helpers used across dashboard and reporting modules."""

from __future__ import annotations

from typing import Any

JsonObject = dict[str, Any]


def mapping(value: Any) -> JsonObject:
    """Return *value* if it is a dict, otherwise an empty dict."""
    return value if isinstance(value, dict) else {}


def safe_list(value: Any) -> list[Any]:
    """Return *value* if it is a list, otherwise an empty list."""
    return value if isinstance(value, list) else []


def safe_str(value: Any, default: str = "") -> str:
    """Return *value* as a string, or *default* if None."""
    if value is None:
        return default
    return str(value)


def safe_int(value: Any, default: int = 0) -> int:
    """Return *value* as an int, or *default* on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    """Return *value* as a float, or *default* on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def bool_text(value: bool) -> str:
    """Return ``'yes'`` or ``'no'``."""
    return "yes" if value else "no"

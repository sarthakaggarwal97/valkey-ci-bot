"""Stable fingerprints for Valkey fuzzer incidents."""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

from scripts.models import FuzzerSignal

_HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_ADDR_RE = re.compile(r"0x[0-9a-f]+", re.IGNORECASE)
_NODE_RE = re.compile(r"\bnode[-_ ]?\d+\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d+\b")
_SPACE_RE = re.compile(r"\s+")


def compute_fuzzer_incident_fingerprint(
    *,
    repo: str,
    workflow_file: str,
    root_cause_category: str | None,
    anomalies: Iterable[FuzzerSignal],
    failed_checks: Iterable[object] = (),
) -> str:
    """Group repeated fuzzer failures by stable failure shape, not run IDs."""
    parts = [
        _normalize(repo),
        _normalize(workflow_file),
        _normalize(root_cause_category or "uncategorized"),
    ]
    checks = sorted({_normalize(str(check)) for check in failed_checks if str(check).strip()})
    parts.extend(checks[:8])

    normalized_anomalies = sorted({
        f"{_normalize(signal.title)}:{_normalize(signal.evidence)}"
        for signal in anomalies
        if signal.title or signal.evidence
    })
    parts.extend(normalized_anomalies[:8])
    basis = "|".join(part for part in parts if part)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def _normalize(value: str) -> str:
    text = value.strip().lower()
    text = _ADDR_RE.sub("<addr>", text)
    text = _HEX_RE.sub("<sha>", text)
    text = _NODE_RE.sub("<node>", text)
    text = _NUMBER_RE.sub("<num>", text)
    return _SPACE_RE.sub(" ", text)

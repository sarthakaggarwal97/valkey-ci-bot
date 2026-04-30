"""Cross-failure correlation engine.

Clusters related failures from the same workflow run or commit before
they reach root cause analysis, so the analyzer sees grouped failures
instead of individual ones.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

from scripts.models import FailureReport

logger = logging.getLogger(__name__)

_FUZZY_THRESHOLD = 0.6


@dataclass
class CorrelatedFailureGroup:
    """A cluster of related failures sharing a common root signal."""

    group_id: str
    failures: list[FailureReport]
    correlation_reason: str
    shared_files: list[str]
    shared_error_pattern: str | None


def _group_id(*parts: str) -> str:
    """Generate a deterministic group ID from key parts."""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _file_paths(report: FailureReport) -> set[str]:
    """Extract all file paths from a failure report's parsed failures."""
    return {pf.file_path for pf in report.parsed_failures if pf.file_path}


def _error_messages(report: FailureReport) -> list[str]:
    """Extract non-empty error messages from a failure report."""
    return [pf.error_message for pf in report.parsed_failures if pf.error_message]


def _test_prefixes(report: FailureReport) -> set[str]:
    """Extract directory-level test name prefixes from a failure report."""
    prefixes: set[str] = set()
    for pf in report.parsed_failures:
        name = pf.test_name or pf.failure_identifier or ""
        # Use the directory portion as the prefix (e.g. "tests/unit/cluster/")
        dirname = os.path.dirname(name)
        if dirname:
            prefixes.add(dirname + "/")
    return prefixes


def _fuzzy_match(a: str, b: str) -> bool:
    """Return True if two error messages are similar above the threshold."""
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= _FUZZY_THRESHOLD


def correlate_failures(
    reports: list[FailureReport],
) -> list[CorrelatedFailureGroup]:
    """Cluster related failures into correlated groups.

    Grouping strategies (applied in order, each report assigned once):
    1. Shared file paths — 2+ failures referencing the same source file.
    2. Shared error message patterns — fuzzy match on error_message.
    3. Shared test name prefix — e.g. all ``tests/unit/cluster/`` failures.

    Ungrouped failures are returned as single-item groups.
    """
    if not reports:
        return []

    assigned: set[int] = set()
    groups: list[CorrelatedFailureGroup] = []

    # --- Strategy 1: shared file paths ---
    file_to_indices: dict[str, list[int]] = defaultdict(list)
    for idx, report in enumerate(reports):
        for fp in _file_paths(report):
            file_to_indices[fp].append(idx)

    # Merge indices that share any file into connected components
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for fp, indices in file_to_indices.items():
        if len(indices) >= 2:
            for i in range(1, len(indices)):
                union(indices[0], indices[i])

    components: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(reports)):
        if find(idx) != idx or idx in parent:
            components[find(idx)].append(idx)
    # Only keep components with 2+ members
    for root, members in components.items():
        if root not in members:
            members.insert(0, root)
        if len(members) < 2:
            continue
        member_set = set(members)
        shared = set.intersection(*(
            _file_paths(reports[i]) for i in members
        ))
        groups.append(CorrelatedFailureGroup(
            group_id=_group_id("file", *sorted(shared)),
            failures=[reports[i] for i in members],
            correlation_reason="shared_file_paths",
            shared_files=sorted(shared),
            shared_error_pattern=None,
        ))
        assigned.update(member_set)

    # --- Strategy 2: shared error message patterns (fuzzy) ---
    remaining = [i for i in range(len(reports)) if i not in assigned]
    error_groups: list[list[int]] = []
    used_in_error: set[int] = set()

    for i, idx_a in enumerate(remaining):
        if idx_a in used_in_error:
            continue
        msgs_a = _error_messages(reports[idx_a])
        if not msgs_a:
            continue
        cluster = [idx_a]
        for idx_b in remaining[i + 1:]:
            if idx_b in used_in_error:
                continue
            msgs_b = _error_messages(reports[idx_b])
            if any(_fuzzy_match(a, b) for a in msgs_a for b in msgs_b):
                cluster.append(idx_b)
        if len(cluster) >= 2:
            error_groups.append(cluster)
            used_in_error.update(cluster)

    for cluster in error_groups:
        all_msgs = []
        for idx in cluster:
            all_msgs.extend(_error_messages(reports[idx]))
        # Pick the shortest message as the representative pattern
        pattern = min(all_msgs, key=len) if all_msgs else None
        shared = set.intersection(*(_file_paths(reports[i]) for i in cluster)) if cluster else set()
        groups.append(CorrelatedFailureGroup(
            group_id=_group_id("error", *(str(i) for i in cluster)),
            failures=[reports[i] for i in cluster],
            correlation_reason="shared_error_pattern",
            shared_files=sorted(shared),
            shared_error_pattern=pattern,
        ))
        assigned.update(cluster)

    # --- Strategy 3: shared test name prefix ---
    remaining = [i for i in range(len(reports)) if i not in assigned]
    prefix_to_indices: dict[str, list[int]] = defaultdict(list)
    for idx in remaining:
        for prefix in _test_prefixes(reports[idx]):
            prefix_to_indices[prefix].append(idx)

    used_in_prefix: set[int] = set()
    for prefix, indices in sorted(prefix_to_indices.items()):
        unassigned = [i for i in indices if i not in used_in_prefix]
        if len(unassigned) < 2:
            continue
        shared = set.intersection(*(_file_paths(reports[i]) for i in unassigned)) if unassigned else set()
        groups.append(CorrelatedFailureGroup(
            group_id=_group_id("prefix", prefix),
            failures=[reports[i] for i in unassigned],
            correlation_reason=f"shared_test_prefix:{prefix}",
            shared_files=sorted(shared),
            shared_error_pattern=None,
        ))
        used_in_prefix.update(unassigned)
    assigned.update(used_in_prefix)

    # --- Ungrouped: wrap each as a single-item group ---
    for idx in range(len(reports)):
        if idx not in assigned:
            report = reports[idx]
            groups.append(CorrelatedFailureGroup(
                group_id=_group_id("single", str(idx), report.commit_sha, report.job_name),
                failures=[report],
                correlation_reason="ungrouped",
                shared_files=sorted(_file_paths(report)),
                shared_error_pattern=None,
            ))

    logger.info(
        "Correlated %d failure(s) into %d group(s).",
        len(reports), len(groups),
    )
    return groups

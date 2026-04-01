"""Tests for structured failure selection before the per-run analysis cap."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SelectionCandidate:
    """Minimal candidate metadata for pre-analysis selection tests."""

    name: str
    incident_key: str
    is_parseable: bool


def apply_structured_failure_selection(
    candidates: list[SelectionCandidate],
    max_failures_per_run: int,
) -> tuple[
    list[SelectionCandidate],
    list[SelectionCandidate],
    list[SelectionCandidate],
    list[SelectionCandidate],
]:
    """Replicate the intended structured-failure selection policy.

    Returns `(selected, rate_limited, unparseable, duplicates)`.
    """
    selected: list[SelectionCandidate] = []
    rate_limited: list[SelectionCandidate] = []
    unparseable: list[SelectionCandidate] = []
    duplicates: list[SelectionCandidate] = []
    seen_incidents: set[str] = set()

    for candidate in sorted(candidates, key=lambda item: item.name):
        if not candidate.is_parseable:
            unparseable.append(candidate)
            continue
        if candidate.incident_key in seen_incidents:
            duplicates.append(candidate)
            continue
        seen_incidents.add(candidate.incident_key)
        if len(selected) < max_failures_per_run:
            selected.append(candidate)
        else:
            rate_limited.append(candidate)

    return selected, rate_limited, unparseable, duplicates


def test_selection_never_exceeds_structured_failure_cap() -> None:
    candidates = [
        SelectionCandidate("job-a", "a", True),
        SelectionCandidate("job-b", "b", True),
        SelectionCandidate("job-c", "c", True),
    ]

    selected, _, _, _ = apply_structured_failure_selection(candidates, 2)

    assert len(selected) == 2


def test_unparseable_candidates_do_not_consume_structured_slots() -> None:
    candidates = [
        SelectionCandidate("job-a-unparseable", "ua", False),
        SelectionCandidate("job-b-parseable", "b", True),
        SelectionCandidate("job-c-unparseable", "uc", False),
        SelectionCandidate("job-d-parseable", "d", True),
        SelectionCandidate("job-e-parseable", "e", True),
    ]

    selected, rate_limited, unparseable, duplicates = apply_structured_failure_selection(
        candidates,
        2,
    )

    assert [candidate.name for candidate in selected] == [
        "job-b-parseable",
        "job-d-parseable",
    ]
    assert [candidate.name for candidate in rate_limited] == ["job-e-parseable"]
    assert [candidate.name for candidate in unparseable] == [
        "job-a-unparseable",
        "job-c-unparseable",
    ]
    assert duplicates == []


def test_duplicate_incidents_are_grouped_before_rate_limit() -> None:
    candidates = [
        SelectionCandidate("job-a-representative", "shared", True),
        SelectionCandidate("job-b-duplicate", "shared", True),
        SelectionCandidate("job-c-unique", "unique", True),
    ]

    selected, rate_limited, _, duplicates = apply_structured_failure_selection(
        candidates,
        1,
    )

    assert [candidate.name for candidate in selected] == ["job-a-representative"]
    assert [candidate.name for candidate in rate_limited] == ["job-c-unique"]
    assert [candidate.name for candidate in duplicates] == ["job-b-duplicate"]


def test_rate_limited_candidates_are_parseable_unique_incidents_only() -> None:
    candidates = [
        SelectionCandidate("job-a", "a", True),
        SelectionCandidate("job-b", "b", True),
        SelectionCandidate("job-c-unparseable", "uc", False),
        SelectionCandidate("job-d-duplicate-a", "a", True),
        SelectionCandidate("job-e", "e", True),
    ]

    selected, rate_limited, unparseable, duplicates = apply_structured_failure_selection(
        candidates,
        2,
    )

    assert [candidate.name for candidate in selected] == ["job-a", "job-b"]
    assert [candidate.name for candidate in rate_limited] == ["job-e"]
    assert [candidate.name for candidate in unparseable] == ["job-c-unparseable"]
    assert [candidate.name for candidate in duplicates] == ["job-d-duplicate-a"]

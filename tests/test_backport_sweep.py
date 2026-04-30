"""Tests for the weekly project-driven backport sweep."""

from __future__ import annotations

from types import SimpleNamespace

from scripts.backport_sweep import (
    BackportTestResult,
    CandidateBackportResult,
    ProjectBackportCandidate,
    ProjectBackportDiscovery,
    build_job_summary,
    build_weekly_pr_body,
    discover_release_branches,
)


class _FakeGraphQL:
    def __init__(self, items: list[dict]) -> None:
        self.items = items

    def execute(self, query: str, variables: dict) -> dict:
        return {
            "organization": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": self.items,
                    }
                }
            }
        }


def _field_value(typename: str, field_name: str, **values: object) -> dict:
    return {
        "__typename": typename,
        "field": {"name": field_name},
        **values,
    }


def _project_item(
    *,
    number: int,
    merged: bool,
    status: str,
    branch: str,
) -> dict:
    return {
        "content": {
            "__typename": "PullRequest",
            "number": number,
            "title": f"Fix {number}",
            "url": f"https://github.com/valkey-io/valkey/pull/{number}",
            "merged": merged,
            "mergeCommit": {"oid": f"merge{number}"},
            "commits": {
                "nodes": [
                    {"commit": {"oid": f"sha{number}a"}},
                    {"commit": {"oid": f"sha{number}b"}},
                ]
            },
        },
        "fieldValues": {
            "nodes": [
                _field_value(
                    "ProjectV2ItemFieldSingleSelectValue",
                    "Status",
                    name=status,
                ),
                _field_value(
                    "ProjectV2ItemFieldTextValue",
                    "Release Branch",
                    text=branch,
                ),
            ]
        },
    }


def test_discover_release_branches_filters_and_sorts_semver() -> None:
    repo = SimpleNamespace(
        get_branches=lambda: [
            SimpleNamespace(name="unstable"),
            SimpleNamespace(name="8.1"),
            SimpleNamespace(name="9.0"),
            SimpleNamespace(name="7.2"),
            SimpleNamespace(name="feature-x"),
        ]
    )

    assert discover_release_branches(repo, r"\d+\.\d+") == ["9.0", "8.1", "7.2"]


def test_project_discovery_filters_merged_status_and_release_branch() -> None:
    discovery = ProjectBackportDiscovery(
        _FakeGraphQL(
            [
                _project_item(number=101, merged=True, status="To be backported", branch="8.1"),
                _project_item(number=102, merged=False, status="To be backported", branch="8.1"),
                _project_item(number=103, merged=True, status="Done", branch="8.1"),
                _project_item(number=104, merged=True, status="To be backported", branch="7.2"),
            ]
        ),
        project_owner="valkey-io",
        project_number=1,
    )

    by_branch = discovery.discover(["8.1"])

    assert [candidate.source_pr_number for candidate in by_branch["8.1"]] == [101]
    assert by_branch["8.1"][0].merge_commit_sha == "merge101"
    assert by_branch["8.1"][0].commit_shas == ["sha101a", "sha101b"]


def test_weekly_pr_body_lists_cherry_picked_prs_commits_and_skips() -> None:
    candidate = ProjectBackportCandidate(
        source_pr_number=101,
        source_pr_title="Fix replication edge case",
        source_pr_url="https://github.com/valkey-io/valkey/pull/101",
        target_branch="8.1",
    )
    duplicate = ProjectBackportCandidate(
        source_pr_number=102,
        source_pr_title="Already done",
        source_pr_url="https://github.com/valkey-io/valkey/pull/102",
        target_branch="8.1",
    )

    body = build_weekly_pr_body(
        "8.1",
        [
            CandidateBackportResult(
                candidate=candidate,
                outcome="applied",
                commits_cherry_picked=["abc123", "def456"],
                test_result=BackportTestResult(status="passed"),
            ),
            CandidateBackportResult(
                candidate=duplicate,
                outcome="duplicate",
                existing_url="https://github.com/valkey-io/valkey/pull/200",
            ),
        ],
    )

    assert "Cherry-picked 1 merged PR(s) onto `8.1`" in body
    assert "https://github.com/valkey-io/valkey/pull/101" in body
    assert "`abc123`<br>`def456`" in body
    assert "Skipped PRs" in body
    assert "https://github.com/valkey-io/valkey/pull/200" in body


def test_job_summary_reports_branch_outcomes() -> None:
    candidate = ProjectBackportCandidate(
        source_pr_number=101,
        source_pr_title="Fix",
        source_pr_url="https://github.com/valkey-io/valkey/pull/101",
        target_branch="8.1",
    )
    branch_result = SimpleNamespace(
        target_branch="8.1",
        outcome="success",
        applied_count=1,
        candidates_found=1,
        pr_url="https://github.com/valkey-io/valkey/pull/200",
        error_message=None,
        results=[CandidateBackportResult(candidate=candidate, outcome="applied")],
    )
    summary = SimpleNamespace(release_branches=["8.1"], branch_results=[branch_result])

    text = build_job_summary(summary)

    assert "`8.1`: success; 1/1 applied" in text
    assert "https://github.com/valkey-io/valkey/pull/200" in text

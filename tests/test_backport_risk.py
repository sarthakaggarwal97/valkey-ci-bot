from __future__ import annotations

from scripts.backport_models import BackportPRContext, ResolutionResult
from scripts.backport_risk import assess_backport_risk


def _context(diff: str, target_branch: str = "9.1") -> BackportPRContext:
    return BackportPRContext(
        source_pr_number=123,
        source_pr_title="Fix issue",
        source_pr_body="",
        source_pr_url="https://github.com/owner/repo/pull/123",
        source_pr_diff=diff,
        target_branch=target_branch,
        commits=["abc1234"],
        repo_full_name="owner/repo",
    )


def test_backport_risk_marks_core_conflict_high() -> None:
    risk = assess_backport_risk(
        _context("diff --git a/src/cluster.c b/src/cluster.c\n", target_branch="8.1"),
        had_conflicts=True,
        resolution_results=[
            ResolutionResult(
                path="src/cluster.c",
                resolved_content="resolved",
                resolution_summary="kept both changes",
                tokens_used=0,
                attempts=1,
            )
        ],
    )

    assert risk.level == "high"
    assert "src/cluster.c" in risk.touched_paths
    assert any("core code" in reason for reason in risk.reasons)


def test_backport_risk_keeps_clean_doc_change_low() -> None:
    risk = assess_backport_risk(
        _context("diff --git a/docs/release.md b/docs/release.md\n"),
        had_conflicts=False,
        resolution_results=None,
    )

    assert risk.level == "low"
    assert risk.touched_paths == ["docs/release.md"]


def test_backport_risk_treats_9x_target_as_older_release_line() -> None:
    """Regression: _CURRENT_DEV_MAJOR = 10, so 9.x is older and gets the bump."""
    risk = assess_backport_risk(
        _context(
            "diff --git a/src/cluster.c b/src/cluster.c\n",
            target_branch="9.1",
        ),
        had_conflicts=False,
        resolution_results=None,
    )

    # src/cluster.c is high-risk (+2) and target=9.1 is older (+1) -> score 3 -> high
    assert risk.level == "high"
    assert any(
        "9.1" in reason and "older release line" in reason
        for reason in risk.reasons
    )

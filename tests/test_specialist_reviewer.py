"""Tests for the 9-specialist parallel PR code review module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from scripts.config import ReviewerConfig
from scripts.models import ChangedFile, PullRequestContext
from scripts.specialist_reviewer import (
    _SPECIALISTS,
    _UNTRUSTED_FENCE,
    SpecialistFinding,
    SpecialistReviewer,
    _deduplicate,
    _determine_verdict,
    _render_markdown,
)


def _context() -> PullRequestContext:
    return PullRequestContext(
        repo="owner/repo",
        number=17,
        title="Improve failover logic",
        body="This updates failover behavior.",
        base_sha="base123",
        head_sha="head456",
        author="alice",
        files=[
            ChangedFile(
                path="src/failover.c",
                status="modified",
                additions=8,
                deletions=2,
                patch="@@ -10,2 +10,8 @@\n-old\n+new",
                contents="int failover(void) { return 1; }",
                is_binary=False,
            )
        ],
    )


def _canned_response(findings: list[dict]) -> str:
    return json.dumps({"findings": findings})


def _no_findings_response() -> str:
    return _canned_response([])


def _high_finding_response() -> str:
    return _canned_response([{
        "path": "src/failover.c",
        "line": 14,
        "severity": "high",
        "title": "Stale state after timeout",
        "description": "Timeout path skips cleanup.",
        "suggestion": "Add cleanup call.",
    }])


def _medium_finding_response() -> str:
    return _canned_response([{
        "path": "src/failover.c",
        "line": 20,
        "severity": "medium",
        "title": "Missing log message",
        "description": "No log on error path.",
        "suggestion": "Add serverLog call.",
    }])


# ── Test 1: All 9 specialists defined with unique slugs and non-empty prompts ──


def test_all_specialists_defined() -> None:
    assert len(_SPECIALISTS) == 9
    slugs = [s.slug for s in _SPECIALISTS]
    assert len(slugs) == len(set(slugs)), "Specialist slugs must be unique"
    for spec in _SPECIALISTS:
        assert spec.name, "Specialist name must not be empty"
        assert spec.slug, "Specialist slug must not be empty"
        assert len(spec.system_prompt) > 0, f"{spec.name} has empty prompt"


# ── Test 2: All specialist prompts contain untrusted-data fencing ──


def test_specialist_prompts_contain_untrusted_data_fence() -> None:
    for spec in _SPECIALISTS:
        assert _UNTRUSTED_FENCE in spec.system_prompt, (
            f"{spec.name} prompt missing untrusted-data fence"
        )


# ── Test 3: Parallel execution calls all 9 specialists ──


def test_parallel_execution() -> None:
    bedrock = MagicMock()
    bedrock.invoke.return_value = _no_findings_response()
    reviewer = SpecialistReviewer(bedrock)
    config = ReviewerConfig()

    result = reviewer.review(_context(), config, ["src/failover.c"])

    assert bedrock.invoke.call_count == 9
    # Each call should use the heavy model
    for call in bedrock.invoke.call_args_list:
        assert call.kwargs["model_id"] == config.models.heavy_model_id


# ── Test 4: Verdict is "Ready to Merge" when no critical/high findings ──


def test_synthesis_verdict_ready_to_merge() -> None:
    findings: list[SpecialistFinding] = []
    assert _determine_verdict(findings) == "Ready to Merge"

    low_only = [SpecialistFinding(
        specialist="Test", path="a.c", line=1,
        severity="low", title="Minor", description="",
    )]
    assert _determine_verdict(low_only) == "Ready to Merge"


# ── Test 5: Verdict is "Needs Work" when critical or high findings exist ──


def test_synthesis_verdict_needs_work() -> None:
    critical = [SpecialistFinding(
        specialist="Security", path="a.c", line=1,
        severity="critical", title="UAF", description="",
    )]
    assert _determine_verdict(critical) == "Needs Work"

    high = [SpecialistFinding(
        specialist="Code Reviewer", path="a.c", line=1,
        severity="high", title="Bug", description="",
    )]
    assert _determine_verdict(high) == "Needs Work"


# ── Test 6: Verdict is "Needs Attention" when only medium findings ──


def test_synthesis_verdict_needs_attention() -> None:
    medium = [SpecialistFinding(
        specialist="Quality", path="a.c", line=1,
        severity="medium", title="Style", description="",
    )]
    assert _determine_verdict(medium) == "Needs Attention"


# ── Test 7: Deduplication removes same file+line+title findings ──


def test_deduplication() -> None:
    findings = [
        SpecialistFinding(
            specialist="Security", path="src/a.c", line=10,
            severity="high", title="Buffer Overflow",
            description="From security reviewer.",
        ),
        SpecialistFinding(
            specialist="Performance", path="src/a.c", line=10,
            severity="medium", title="buffer overflow",
            description="From performance reviewer.",
        ),
        SpecialistFinding(
            specialist="Quality", path="src/b.c", line=20,
            severity="low", title="Style issue",
            description="Different file.",
        ),
    ]
    deduped = _deduplicate(findings)
    assert len(deduped) == 2
    assert deduped[0].specialist == "Security"
    assert deduped[1].path == "src/b.c"


# ── Test 8: Rendered markdown contains the verdict string ──


def test_rendered_markdown_contains_verdict() -> None:
    findings = [SpecialistFinding(
        specialist="Code Reviewer", path="src/a.c", line=5,
        severity="high", title="Logic error", description="Bad branch.",
    )]
    md = _render_markdown(findings, "Needs Work", [])
    assert "### Verdict: Needs Work" in md

    md_clean = _render_markdown([], "Ready to Merge", ["Test Runner"])
    assert "### Verdict: Ready to Merge" in md_clean


# ── Test 9: Performance specialist prompt mentions zmalloc/zfree ──


def test_memory_safety_in_performance_prompt() -> None:
    perf = next(s for s in _SPECIALISTS if s.slug == "performance")
    assert "zmalloc" in perf.system_prompt
    assert "zfree" in perf.system_prompt


# ── Test 10: Security specialist prompt mentions use-after-free / buffer overflow ──


def test_memory_safety_in_security_prompt() -> None:
    sec = next(s for s in _SPECIALISTS if s.slug == "security")
    prompt_lower = sec.system_prompt.lower()
    assert "use-after-free" in prompt_lower
    assert "buffer overflow" in prompt_lower.replace("overflows", "overflow")


# ── Test 11: Skeptic pass filters false positives ──


def test_skeptic_pass_drops_findings() -> None:
    """Skeptic pass should drop findings the verifier marks as 'drop'."""
    mock_bedrock = MagicMock()
    skeptic_response = json.dumps({
        "results": [
            {"index": 0, "verdict": "drop", "reason": "speculative"},
            {"index": 1, "verdict": "keep", "severity": "medium", "reason": "concrete"},
        ]
    })
    mock_bedrock.invoke = MagicMock(return_value=skeptic_response)

    reviewer = SpecialistReviewer(mock_bedrock)
    findings = [
        SpecialistFinding(
            specialist="Security", path="src/a.c", line=10,
            severity="high", title="Speculative issue", description="Maybe bad.",
        ),
        SpecialistFinding(
            specialist="Performance", path="src/b.c", line=20,
            severity="medium", title="Real issue", description="Concrete bug.",
        ),
    ]
    context = MagicMock()
    context.title = "Test PR"
    config = MagicMock()
    config.models = MagicMock()
    config.models.light_model_id = "test-model"
    config.max_output_tokens = 4096

    result = reviewer._run_skeptic_pass(findings, context, config)
    assert len(result) == 1
    assert result[0].title == "Real issue"

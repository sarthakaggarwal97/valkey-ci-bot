"""Tests for scripts/stages/review_specialist.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from scripts.models import EvidencePack, InspectedFile, ReviewFinding
from scripts.stages.review_specialist import (
    SecurityPolicyReviewer,
    SubsystemReviewer,
    VerifierAggregator,
    cluster_diff,
    run_specialist_review,
)


def _make_evidence(files: list[str] | None = None) -> EvidencePack:
    files = files or ["src/server.c", "tests/unit/hash.tcl"]
    sources = [InspectedFile(path=f, reason="PR") for f in files if not f.startswith("tests/")]
    tests = [InspectedFile(path=f, reason="PR") for f in files if f.startswith("tests/")]
    return EvidencePack(
        failure_id="pr-1", run_id=None, job_ids=[],
        workflow="pr-review", parsed_failures=[],
        log_excerpts=[], source_files_inspected=sources,
        test_files_inspected=tests, valkey_guidance_used=[],
        recent_commits=[], linked_urls=[], unknowns=[],
        built_at="2025-01-01T00:00:00Z",
    )


# --- cluster_diff ---

def test_cluster_diff_routes_security_files_separately():
    clusters = cluster_diff(
        [".github/workflows/ci.yml", "src/server.c"],
        "diff content",
    )
    specialists = {c.specialist for c in clusters}
    assert "subsystem" in specialists
    assert "security_policy" in specialists


def test_cluster_diff_routes_auth_files_to_security():
    clusters = cluster_diff(["src/acl.c"], "diff")
    assert any(c.specialist == "security_policy" for c in clusters)


def test_cluster_diff_routes_src_to_subsystem():
    clusters = cluster_diff(["src/t_hash.c", "src/t_list.c"], "diff")
    assert any(c.specialist == "subsystem" for c in clusters)
    assert not any(c.specialist == "security_policy" for c in clusters)


def test_cluster_diff_routes_tests_to_subsystem():
    clusters = cluster_diff(["tests/unit/foo.tcl"], "diff")
    assert any(c.specialist == "subsystem" for c in clusters)


def test_cluster_diff_routes_tls_to_security():
    clusters = cluster_diff(["src/tls.c"], "diff")
    assert any(c.specialist == "security_policy" for c in clusters)


def test_cluster_diff_unknown_paths_go_to_subsystem():
    clusters = cluster_diff(["random/path.txt"], "diff")
    assert any(c.specialist == "subsystem" for c in clusters)


# --- SubsystemReviewer ---

def test_subsystem_reviewer_returns_findings():
    from scripts.stages.review_specialist import DiffCluster
    bedrock = MagicMock()
    bedrock.invoke.return_value = json.dumps([
        {
            "path": "src/server.c", "line": 42, "body": "Missing null check",
            "severity": "high", "title": "Null pointer risk", "confidence": "high",
        }
    ])
    reviewer = SubsystemReviewer()
    cluster = DiffCluster(specialist="subsystem", files=["src/server.c"], diff_text="...")
    findings = reviewer.review(cluster, _make_evidence(), bedrock)
    assert len(findings) == 1
    assert findings[0].path == "src/server.c"
    assert findings[0].severity == "high"


def test_subsystem_reviewer_returns_empty_on_model_error():
    from scripts.stages.review_specialist import DiffCluster
    bedrock = MagicMock()
    bedrock.invoke.side_effect = RuntimeError("fail")
    reviewer = SubsystemReviewer()
    cluster = DiffCluster(specialist="subsystem", files=["src/server.c"], diff_text="...")
    findings = reviewer.review(cluster, _make_evidence(), bedrock)
    assert findings == []


# --- SecurityPolicyReviewer ---

def test_security_reviewer_returns_findings():
    from scripts.stages.review_specialist import DiffCluster
    bedrock = MagicMock()
    bedrock.invoke.return_value = json.dumps([
        {
            "path": ".github/workflows/ci.yml", "line": 10,
            "body": "pull_request_target unsafe",
            "severity": "high", "title": "Workflow trigger risk",
            "confidence": "high",
        }
    ])
    reviewer = SecurityPolicyReviewer()
    cluster = DiffCluster(specialist="security_policy", files=[".github/workflows/ci.yml"], diff_text="...")
    findings = reviewer.review(cluster, _make_evidence([".github/workflows/ci.yml"]), bedrock)
    assert len(findings) == 1


# --- VerifierAggregator ---

def test_aggregator_dedupes_findings_with_same_key():
    ev = _make_evidence()
    findings = [
        ReviewFinding(
            path="src/server.c", line=10, body="bug", severity="high",
            title="A null check needed",
        ),
        ReviewFinding(
            path="src/server.c", line=10, body="bug duplicate", severity="high",
            title="A null check needed",
        ),
    ]
    agg = VerifierAggregator()
    result = agg.aggregate(findings, ev)
    assert len(result) == 1


def test_aggregator_drops_findings_not_in_inspected_files():
    ev = _make_evidence(["src/server.c"])
    findings = [
        ReviewFinding(
            path="src/server.c", line=10, body="good", severity="high",
            title="Valid",
        ),
        ReviewFinding(
            path="src/unrelated.c", line=20, body="bad", severity="high",
            title="Invalid",
        ),
    ]
    agg = VerifierAggregator()
    result = agg.aggregate(findings, ev)
    paths = [f.path for f in result]
    assert "src/server.c" in paths
    assert "src/unrelated.c" not in paths


def test_aggregator_respects_max_comments():
    ev = _make_evidence(["src/x.c"])
    findings = [
        ReviewFinding(
            path="src/x.c", line=i, body=f"b{i}", severity="high",
            title=f"T{i}",
        )
        for i in range(30)
    ]
    agg = VerifierAggregator()
    result = agg.aggregate(findings, ev, max_comments=10)
    assert len(result) == 10


# --- run_specialist_review end-to-end ---

def test_run_specialist_review_combines_both_specialists():
    bedrock = MagicMock()
    # First call (subsystem), second (security)
    bedrock.invoke.side_effect = [
        json.dumps([{
            "path": "src/server.c", "line": 10, "body": "b1", "severity": "medium",
            "title": "t1", "confidence": "high",
        }]),
        json.dumps([{
            "path": ".github/workflows/ci.yml", "line": 5, "body": "b2",
            "severity": "high", "title": "t2", "confidence": "high",
        }]),
    ]
    ev = _make_evidence(["src/server.c", ".github/workflows/ci.yml"])
    findings = run_specialist_review(
        ev, "diff", ["src/server.c", ".github/workflows/ci.yml"],
        bedrock, max_comments=25,
    )
    # Both specialists contributed
    paths = {f.path for f in findings}
    assert "src/server.c" in paths
    assert ".github/workflows/ci.yml" in paths

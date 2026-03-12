"""Tests for Valkey fuzzer workflow run analysis."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from scripts.fuzzer_run_analyzer import FuzzerRunAnalyzer
from scripts.workflow_artifact_client import WorkflowArtifact


def _make_run(run_id: int = 10, conclusion: str = "failure") -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.html_url = f"https://github.com/valkey-io/valkey-fuzzer/actions/runs/{run_id}"
    run.conclusion = conclusion
    run.head_sha = "abc123"
    run.jobs.return_value = []
    return run


def test_analyzer_prefers_artifacts_and_keeps_deterministic_findings() -> None:
    github_client = MagicMock()
    repo = github_client.get_repo.return_value
    repo.get_workflow_run.return_value = _make_run()
    artifact_client = MagicMock()
    artifact_client.list_run_artifacts.return_value = [
        WorkflowArtifact(
            artifact_id=5,
            name="fuzzer-run-artifacts-10",
            size_in_bytes=1,
            expired=False,
        )
    ]
    artifact_client.download_artifact_files.return_value = {
        "bundle/manifest.json": json.dumps(
            {"scenario_id": "839534793", "seed": 839534793, "success": False}
        ).encode("utf-8"),
        "bundle/results.json": json.dumps(
            {
                "results": [
                    {
                        "scenario_id": "839534793",
                        "success": False,
                        "seed": 839534793,
                        "final_validation": {
                            "failed_checks": ["slot_coverage"],
                            "error_messages": [
                                "Slot Coverage: CRITICAL: 1024 slots still assigned to killed nodes."
                            ],
                            "checks": {
                                "replication": {"success": True, "error": None},
                                "slot_coverage": {
                                    "success": False,
                                    "error": "CRITICAL: 1024 slots still assigned to killed nodes.",
                                },
                            },
                        },
                    }
                ]
            }
        ).encode("utf-8"),
        "bundle/logs/839534793.json": json.dumps(
            {
                "chaos_events": [
                    {
                        "chaos_type": "process_kill",
                        "target_node": "node-4",
                        "success": True,
                    }
                ],
                "errors": [],
            }
        ).encode("utf-8"),
        "bundle/logs/node-4.log": b"Failover election won\n",
    }
    bedrock_client = MagicMock()
    bedrock_client.invoke.return_value = json.dumps(
        {
            "overall_status": "warning",
            "summary": "The run exposed slot coverage loss after chaos.",
            "anomalies": [],
            "normal_signals": ["The run captured a successful failover election."],
            "reproduction_hint": "valkey-fuzzer cluster --seed 839534793",
        }
    )

    analyzer = FuzzerRunAnalyzer(
        github_client,
        bedrock_client,
        artifact_client=artifact_client,
        log_retriever=MagicMock(),
    )
    analysis = analyzer.analyze_workflow_run(
        "valkey-io/valkey-fuzzer",
        10,
        workflow_file="fuzzer-run.yml",
    )

    assert analysis.scenario_id == "839534793"
    assert analysis.seed == "839534793"
    assert analysis.overall_status == "anomalous"
    assert any("Slot Coverage" in signal.evidence for signal in analysis.anomalies)
    assert "Replication validation passed." in analysis.normal_signals
    assert any("Chaos event process_kill" in signal for signal in analysis.normal_signals)
    assert analysis.raw_log_fallback_used is False


def test_analyzer_falls_back_to_job_log_when_artifacts_are_missing() -> None:
    github_client = MagicMock()
    repo = github_client.get_repo.return_value
    run = _make_run(run_id=11, conclusion="success")
    job = MagicMock()
    job.id = 77
    job.name = "random-fuzzer"
    run.jobs.return_value = [job]
    repo.get_workflow_run.return_value = run

    artifact_client = MagicMock()
    artifact_client.list_run_artifacts.return_value = []
    artifact_client.download_run_log_files.return_value = {}
    log_retriever = MagicMock()
    log_retriever.get_job_log.return_value = (
        "random-fuzzer\tUNKNOWN STEP\t2026-03-12T07:05:45.7114408Z Scenario: 12345\n"
        "random-fuzzer\tUNKNOWN STEP\t2026-03-12T07:05:45.7114678Z Status: PASSED\n"
        "random-fuzzer\tUNKNOWN STEP\t2026-03-12T07:05:45.7115767Z Seed: 12345 (use to reproduce)\n"
        "random-fuzzer\tUNKNOWN STEP\t2026-03-12T07:05:45.7117293Z Chaos Events:\n"
        "random-fuzzer\tUNKNOWN STEP\t2026-03-12T07:05:45.7117469Z   [PASS] process_kill on node-0 (8.40s)\n"
        "random-fuzzer\tUNKNOWN STEP\t2026-03-12T07:05:45.7118462Z Final Validation Details:\n"
        "random-fuzzer\tUNKNOWN STEP\t2026-03-12T07:05:45.7118655Z   Replication: PASS\n"
    )
    bedrock_client = MagicMock()
    bedrock_client.invoke.side_effect = RuntimeError("bedrock unavailable")

    analyzer = FuzzerRunAnalyzer(
        github_client,
        bedrock_client,
        artifact_client=artifact_client,
        log_retriever=log_retriever,
    )
    analysis = analyzer.analyze_workflow_run(
        "valkey-io/valkey-fuzzer",
        11,
        workflow_file="fuzzer-run.yml",
    )

    assert analysis.scenario_id == "12345"
    assert analysis.seed == "12345"
    assert analysis.overall_status == "normal"
    assert analysis.raw_log_fallback_used is True
    assert analysis.summary.startswith("Run 11")


def test_analyzer_does_not_treat_serverassert_object_name_as_crash() -> None:
    github_client = MagicMock()
    repo = github_client.get_repo.return_value
    run = _make_run(run_id=12, conclusion="success")
    repo.get_workflow_run.return_value = run

    artifact_client = MagicMock()
    artifact_client.list_run_artifacts.return_value = []
    artifact_client.download_run_log_files.return_value = {
        "logs/random-fuzzer.txt": (
            b"serverassert.d monotonic.d util.d\n"
            b"Successful build output only\n"
        )
    }
    bedrock_client = MagicMock()
    bedrock_client.invoke.side_effect = RuntimeError("bedrock unavailable")

    analyzer = FuzzerRunAnalyzer(
        github_client,
        bedrock_client,
        artifact_client=artifact_client,
        log_retriever=MagicMock(),
    )
    analysis = analyzer.analyze_workflow_run(
        "valkey-io/valkey-fuzzer",
        12,
        workflow_file="fuzzer-run.yml",
    )

    assert analysis.overall_status == "normal"
    assert analysis.anomalies == []

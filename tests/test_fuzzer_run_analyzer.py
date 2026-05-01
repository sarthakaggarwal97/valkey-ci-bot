from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
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


def _agent_result(stdout: str, stderr: str = "", rc: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


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
            {
                "scenario_id": "839534793",
                "seed": 839534793,
                "success": False,
                "tested_valkey_sha": "1234567890abcdef1234567890abcdef12345678",
            }
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
    claude_response = json.dumps(
        {
            "overall_status": "warning",
            "summary": "The run exposed slot coverage loss after chaos.",
            "anomalies": [],
            "normal_signals": ["The run captured a successful failover election."],
            "reproduction_hint": "valkey-fuzzer cluster --seed 839534793",
        }
    )

    import unittest.mock
    with unittest.mock.patch(
        "scripts.fuzzer_run_analyzer.run_agent",
        return_value=_agent_result(claude_response),
    ):
        analyzer = FuzzerRunAnalyzer(
            github_client,
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
    assert analysis.triage_verdict == "likely-core-valkey-bug"
    assert analysis.suggested_labels == ["possible-valkey-bug"]
    assert any("Slot Coverage" in signal.evidence for signal in analysis.anomalies)
    assert "Replication validation passed." in analysis.normal_signals
    assert any("Chaos event process_kill" in signal for signal in analysis.normal_signals)
    assert analysis.raw_log_fallback_used is False
    assert analysis.tested_valkey_sha == "1234567890abcdef1234567890abcdef12345678"
    assert analysis.evidence_quality == "complete"
    assert analysis.missing_artifact_fields == []
    assert analysis.incident_fingerprint


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
    import unittest.mock
    with unittest.mock.patch(
        "scripts.fuzzer_run_analyzer.run_agent",
        side_effect=RuntimeError("claude unavailable"),
    ):
        analyzer = FuzzerRunAnalyzer(
            github_client,
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
    assert analysis.triage_verdict == "expected-chaos-noise"
    assert analysis.raw_log_fallback_used is True
    assert analysis.evidence_quality == "degraded"
    assert "tested_valkey_sha" in analysis.missing_artifact_fields
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

    import unittest.mock
    with unittest.mock.patch(
        "scripts.fuzzer_run_analyzer.run_agent",
        side_effect=RuntimeError("claude unavailable"),
    ):
        analyzer = FuzzerRunAnalyzer(
            github_client,
            artifact_client=artifact_client,
            log_retriever=MagicMock(),
        )
        analysis = analyzer.analyze_workflow_run(
            "valkey-io/valkey-fuzzer",
        12,
        workflow_file="fuzzer-run.yml",
    )

    assert analysis.overall_status == "normal"
    assert analysis.triage_verdict == "expected-chaos-noise"
    assert analysis.anomalies == []


def test_write_context_to_dir_creates_all_files(tmp_path: Path) -> None:
    from scripts.fuzzer_run_analyzer import _write_context_to_dir
    from scripts.models import FuzzerRunContext

    context = FuzzerRunContext(
        repo="valkey-io/valkey-fuzzer",
        workflow_file="fuzzer-run.yml",
        run_id=99,
        run_url="https://example.com/99",
        conclusion="failure",
        head_sha="abc",
        scenario_yaml="chaos:\n  kill: node-4",
        results={"success": False, "error_message": "slot loss"},
        structured_logs={"events.json": {"chaos_events": []}},
        node_logs={"node-4.log": "ASSERTION FAILED\nstack trace here"},
        raw_job_log="some raw log output",
    )
    files = _write_context_to_dir(context, tmp_path)
    assert (tmp_path / "results.json").exists()
    assert (tmp_path / "scenario.yaml").exists()
    assert (tmp_path / "logs" / "events.json").exists()
    assert (tmp_path / "logs" / "node-4.log").exists()
    assert (tmp_path / "job-log.txt").exists()
    assert len(files) == 5
    # Verify content round-trips
    assert "slot loss" in (tmp_path / "results.json").read_text()
    assert "ASSERTION FAILED" in (tmp_path / "logs" / "node-4.log").read_text()


def test_format_deterministic_summary() -> None:
    from scripts.fuzzer_run_analyzer import _format_deterministic_summary
    from scripts.models import FuzzerSignal

    anomalies = [
        FuzzerSignal(title="Crash", severity="critical", evidence="node-4: SIGABRT"),
    ]
    normal = ["Failover election won (node-7.log)."]
    result = _format_deterministic_summary(anomalies, normal)
    assert "Anomalies (1):" in result
    assert "[critical] Crash" in result
    assert "Normal signals (1):" in result
    assert "Failover election won" in result


def test_invoke_claude_code_parses_json(tmp_path: Path, monkeypatch: object) -> None:
    import json

    from scripts.fuzzer_run_analyzer import _invoke_claude_code
    from scripts.models import FuzzerRunContext

    context = FuzzerRunContext(
        repo="valkey-io/valkey-fuzzer",
        workflow_file="fuzzer-run.yml",
        run_id=42,
        run_url="https://example.com/42",
        conclusion="failure",
        head_sha="def456",
    )
    mock_response = json.dumps({
        "overall_status": "anomalous",
        "triage_verdict": "likely-core-valkey-bug",
        "root_cause_category": "split-brain",
        "summary": "Split brain detected.",
        "anomalies": [{"title": "Split brain", "severity": "critical", "evidence": "two primaries"}],
        "normal_signals": [],
        "reproduction_hint": None,
    })
    monkeypatch.setattr(
        "scripts.fuzzer_run_analyzer.run_agent",
        lambda profile, prompt, **kw: _agent_result(mock_response),
    )
    result = _invoke_claude_code(
        system_prompt="You analyze fuzzer runs.",
        deterministic_summary="",
        retrieved_context="",
        artifact_dir=tmp_path,
        context=context,
    )
    assert result["overall_status"] == "anomalous"
    assert result["triage_verdict"] == "likely-core-valkey-bug"
    assert result["root_cause_category"] == "split-brain"

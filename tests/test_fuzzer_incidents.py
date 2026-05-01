from __future__ import annotations

from scripts.fuzzer_incidents import compute_fuzzer_incident_fingerprint
from scripts.models import FuzzerSignal


def test_fuzzer_incident_fingerprint_normalizes_run_specific_values() -> None:
    first = compute_fuzzer_incident_fingerprint(
        repo="valkey-io/valkey-fuzzer",
        workflow_file="fuzzer-run.yml",
        root_cause_category="split-brain",
        failed_checks=["slot_coverage"],
        anomalies=[
            FuzzerSignal(
                title="Split-brain or slot loss",
                severity="critical",
                evidence="node-4: 1024 slots still assigned to killed nodes at 1234567890abcdef",
            )
        ],
    )
    second = compute_fuzzer_incident_fingerprint(
        repo="valkey-io/valkey-fuzzer",
        workflow_file="fuzzer-run.yml",
        root_cause_category="split-brain",
        failed_checks=["slot_coverage"],
        anomalies=[
            FuzzerSignal(
                title="Split-brain or slot loss",
                severity="critical",
                evidence="node-8: 2048 slots still assigned to killed nodes at fedcba9876543210",
            )
        ],
    )

    assert first == second

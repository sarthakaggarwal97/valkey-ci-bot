from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExpectedFinding:
    path: str
    description: str
    severity: str = "medium"


@dataclass
class EvalFixture:
    name: str
    repo: str
    description: str
    flow: str = ""
    pr_number: int = 0
    workflow_run_id: int = 0
    expected_findings: list[ExpectedFinding] = field(default_factory=list)
    ground_truth_root_cause: str = ""
    ground_truth_fix_files: list[str] = field(default_factory=list)
    ground_truth: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


def load_fixtures(fixtures_dir: str | Path) -> list[EvalFixture]:
    fixtures_path = Path(fixtures_dir)
    results = []
    for f in sorted(fixtures_path.glob("*.json")):
        data = json.loads(f.read_text())
        findings = [
            ExpectedFinding(**ef)
            for ef in data.get("expected_findings", [])
        ]
        results.append(EvalFixture(
            name=data["name"],
            repo=data["repo"],
            description=data.get("description", ""),
            flow=data.get("flow", ""),
            pr_number=data.get("pr_number", 0),
            workflow_run_id=data.get("workflow_run_id", 0),
            expected_findings=findings,
            ground_truth_root_cause=data.get("ground_truth_root_cause", ""),
            ground_truth_fix_files=data.get("ground_truth_fix_files", []),
            ground_truth=data.get("ground_truth", {}),
            tags=data.get("tags", []),
        ))
    return results

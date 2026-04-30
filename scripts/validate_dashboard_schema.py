"""Validate a dashboard JSON file against the schema_version=1 contract."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, List

_REQUIRED_SECTIONS = [
    "schema_version", "generated_at", "snapshot", "ci_failures", "flaky_tests",
    "pr_reviews", "acceptance", "fuzzer", "agent_outcomes", "ai_reliability",
    "state_health", "trends", "daily_health", "wow_trends",
]

_SNAPSHOT_INT_FIELDS = [
    "failure_incidents", "queued_failures", "active_flaky_campaigns",
    "tracked_review_prs", "review_comments", "fuzzer_runs_analyzed",
    "fuzzer_anomalous_runs", "daily_runs_seen", "ai_token_usage",
    "agent_events", "instrumentation_gaps",
]

_SECTION_CHECKS: dict = {
    "ci_failures": {"int": ["failure_incidents", "queued_failures"], "list": ["recent_incidents"]},
    "flaky_tests": {"int": ["campaigns", "active_campaigns"], "list": ["recent_campaigns"]},
    "pr_reviews": {"int": ["tracked_prs"], "list": ["recent_reviews"]},
    "acceptance": {"int": ["review_cases"], "list": ["recent_review_results", "recent_workflow_results"]},
    "fuzzer": {"int": ["runs_analyzed"], "list": ["recent_anomalies"]},
    "agent_outcomes": {"int": ["events"], "list": ["recent_events"]},
    "ai_reliability": {"int": ["token_usage"], "list": ["instrumentation_gaps"]},
    "state_health": {"list": ["input_warnings", "recent_watermarks"]},
    "trends": {"list": ["labels"]},
    "daily_health": {"list": ["dates", "heatmap", "runs"]},
}


def validate(data: Any) -> List[str]:
    """Return a list of error strings. Empty list means valid."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["root must be an object"]

    # schema_version
    sv = data.get("schema_version")
    if sv is None:
        errors.append("missing required field: schema_version")
    elif sv != 1:
        errors.append("schema_version must be 1, got {}".format(sv))

    # generated_at
    if not isinstance(data.get("generated_at"), str):
        errors.append("generated_at must be a string")

    # required sections
    for key in _REQUIRED_SECTIONS:
        if key not in data:
            errors.append("missing required section: {}".format(key))

    # snapshot
    snapshot = data.get("snapshot")
    if isinstance(snapshot, dict):
        for field in _SNAPSHOT_INT_FIELDS:
            val = snapshot.get(field)
            if val is not None and not isinstance(val, int):
                errors.append("snapshot.{} must be int, got {}".format(field, type(val).__name__))
    elif snapshot is not None:
        errors.append("snapshot must be an object")

    # section-level checks
    for section, checks in _SECTION_CHECKS.items():
        obj = data.get(section)
        if not isinstance(obj, dict):
            if obj is not None:
                errors.append("{} must be an object".format(section))
            continue
        for field in checks.get("int", []):
            val = obj.get(field)
            if val is not None and not isinstance(val, int):
                errors.append("{}.{} must be int, got {}".format(section, field, type(val).__name__))
        for field in checks.get("list", []):
            val = obj.get(field)
            if val is not None and not isinstance(val, list):
                errors.append("{}.{} must be a list, got {}".format(section, field, type(val).__name__))

    # wow_trends.has_data
    wow = data.get("wow_trends")
    if isinstance(wow, dict):
        hd = wow.get("has_data")
        if hd is not None and not isinstance(hd, bool):
            errors.append("wow_trends.has_data must be bool, got {}".format(type(hd).__name__))
    elif wow is not None:
        errors.append("wow_trends must be an object")

    return errors


def main(argv: list | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m scripts.validate_dashboard_schema <path>")
        return 2
    path = Path(args[0])
    if not path.exists():
        print("error: {} not found".format(path))
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    errors = validate(data)
    if errors:
        for err in errors:
            print("error: {}".format(err))
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

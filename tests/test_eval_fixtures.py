from __future__ import annotations

from pathlib import Path

from scripts.eval.eval_fixtures import load_fixtures


def test_load_fixtures_from_directory():
    fixtures_dir = Path(__file__).parent.parent / "eval" / "fixtures"
    if not fixtures_dir.exists():
        return  # skip if no fixtures yet
    fixtures = load_fixtures(fixtures_dir)
    assert len(fixtures) >= 1
    assert fixtures[0].name == "pr-117-empty-files"
    assert len(fixtures[0].expected_findings) == 3

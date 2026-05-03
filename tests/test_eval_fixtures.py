from __future__ import annotations

from pathlib import Path

from scripts.eval.eval_fixtures import load_fixtures


def test_load_fixtures_from_directory():
    fixtures_dir = Path(__file__).parent.parent / "eval" / "fixtures"
    if not fixtures_dir.exists():
        return  # skip if no fixtures yet
    fixtures = load_fixtures(fixtures_dir)
    assert len(fixtures) >= 1
    names = {f.name for f in fixtures}
    assert "pr-117-empty-files" in names

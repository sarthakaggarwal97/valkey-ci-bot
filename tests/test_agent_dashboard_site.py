"""Tests for the static dashboard site publisher."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.agent_dashboard_site import build_site, main
from scripts.validate_dashboard_schema import validate

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DASHBOARD_APP = _REPO_ROOT / "dashboard-app"
_FIXTURE_FULL = _REPO_ROOT / "fixtures" / "dashboard" / "full.json"


def _load_fixture() -> dict:
    return json.loads(_FIXTURE_FULL.read_text(encoding="utf-8"))


def test_build_site_copies_dashboard_app(tmp_path: Path) -> None:
    site_dir = tmp_path / "out"
    dashboard = _load_fixture()

    build_site(dashboard, site_dir)

    # Core shell + redirect stubs all present
    assert (site_dir / "index.html").is_file()
    assert (site_dir / "review.html").is_file()
    assert (site_dir / "fuzzer.html").is_file()
    assert (site_dir / "diagnostics.html").is_file()
    assert (site_dir / "ops.html").is_file()

    # Assets copied
    assert (site_dir / "assets" / "css" / "tokens.css").is_file()
    assert (site_dir / "assets" / "css" / "base.css").is_file()
    assert (site_dir / "assets" / "css" / "components.css").is_file()
    assert (site_dir / "assets" / "js" / "app.js").is_file()
    assert (site_dir / "assets" / "js" / "dom.js").is_file()
    assert (site_dir / "assets" / "js" / "router.js").is_file()
    assert (site_dir / "assets" / "js" / "theme.js").is_file()
    assert (site_dir / "assets" / "js" / "utils.js").is_file()
    assert (site_dir / "assets" / "js" / "pages" / "daily.js").is_file()
    assert (site_dir / "assets" / "js" / "pages" / "prs.js").is_file()
    assert (site_dir / "assets" / "js" / "pages" / "fuzzer.js").is_file()
    assert (site_dir / "assets" / "js" / "pages" / "diagnostics.js").is_file()
    assert (site_dir / "assets" / "js" / "components" / "heatmap.js").is_file()
    assert (site_dir / "assets" / "js" / "components" / "table.js").is_file()
    assert (site_dir / "assets" / "valkey-horizontal.svg").is_file()


def test_build_site_writes_dashboard_json_into_data_dir(tmp_path: Path) -> None:
    site_dir = tmp_path / "out"
    dashboard = _load_fixture()

    build_site(dashboard, site_dir)

    data_file = site_dir / "data" / "dashboard.json"
    assert data_file.is_file()
    parsed = json.loads(data_file.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == 1
    # The full fixture round-trips
    assert parsed == dashboard
    # And the written JSON passes the schema validator
    assert validate(parsed) == []


def test_build_site_refuses_to_copy_into_source(tmp_path: Path) -> None:
    dashboard = _load_fixture()
    with pytest.raises(ValueError, match="source and output directories must differ"):
        build_site(dashboard, _DASHBOARD_APP, source_dir=_DASHBOARD_APP)


def test_build_site_raises_on_missing_source(tmp_path: Path) -> None:
    dashboard = _load_fixture()
    with pytest.raises(FileNotFoundError):
        build_site(dashboard, tmp_path / "out", source_dir=tmp_path / "does-not-exist")


def test_cli_reads_dashboard_json_and_writes_site(tmp_path: Path) -> None:
    dashboard_path = tmp_path / "dashboard.json"
    dashboard_path.write_text(_FIXTURE_FULL.read_text(encoding="utf-8"), encoding="utf-8")

    site_dir = tmp_path / "site"
    exit_code = main([
        "--dashboard-json", str(dashboard_path),
        "--site-dir", str(site_dir),
    ])
    assert exit_code == 0
    assert (site_dir / "index.html").is_file()
    assert (site_dir / "data" / "dashboard.json").is_file()


def test_build_site_overwrites_existing_output(tmp_path: Path) -> None:
    site_dir = tmp_path / "out"
    site_dir.mkdir()
    # Put a stale file that should be left untouched (we only overwrite,
    # we don't clean), but assets should still be copied fresh.
    (site_dir / "stale.txt").write_text("old", encoding="utf-8")

    dashboard = _load_fixture()
    build_site(dashboard, site_dir)

    # Core assets appear regardless of pre-existing content.
    assert (site_dir / "index.html").is_file()
    # Stale file was not deleted — this is expected (no --clean flag).
    assert (site_dir / "stale.txt").is_file()


def test_custom_source_dir(tmp_path: Path) -> None:
    # Build a minimal fake source and verify the CLI honors --source-dir.
    src = tmp_path / "src"
    src.mkdir()
    (src / "marker.html").write_text("hi", encoding="utf-8")

    dashboard = _load_fixture()
    out = tmp_path / "out"
    build_site(dashboard, out, source_dir=src)

    assert (out / "marker.html").is_file()
    assert (out / "data" / "dashboard.json").is_file()

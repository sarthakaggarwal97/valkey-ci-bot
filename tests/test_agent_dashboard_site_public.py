"""Tests for the public-facing Valkey CI Health dashboard site."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.agent_dashboard_site_public import build_public_site, main
from scripts.validate_dashboard_schema import validate

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DASHBOARD_APP_PUBLIC = _REPO_ROOT / "dashboard-app-public"
_FIXTURE_FULL = _REPO_ROOT / "fixtures" / "dashboard" / "full.json"


def _load_fixture() -> dict:
    return json.loads(_FIXTURE_FULL.read_text(encoding="utf-8"))


def test_build_public_site_copies_dashboard_app_public(tmp_path: Path) -> None:
    site_dir = tmp_path / "out"
    build_public_site(_load_fixture(), site_dir)

    assert (site_dir / "index.html").is_file()
    assert (site_dir / "review.html").is_file()
    assert (site_dir / "fuzzer.html").is_file()
    # Public site has NO diagnostics page
    assert not (site_dir / "diagnostics.html").exists()

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
    assert (site_dir / "assets" / "js" / "components" / "heatmap.js").is_file()
    assert (site_dir / "assets" / "js" / "components" / "table.js").is_file()
    assert (site_dir / "assets" / "valkey-horizontal.svg").is_file()


def test_build_public_site_writes_dashboard_json(tmp_path: Path) -> None:
    site_dir = tmp_path / "out"
    dashboard = _load_fixture()
    build_public_site(dashboard, site_dir)

    data_file = site_dir / "data" / "dashboard.json"
    assert data_file.is_file()
    parsed = json.loads(data_file.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == 1
    assert parsed == dashboard
    assert validate(parsed) == []


def test_public_site_refuses_to_copy_into_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source and output directories must differ"):
        build_public_site(_load_fixture(), _DASHBOARD_APP_PUBLIC, source_dir=_DASHBOARD_APP_PUBLIC)


def test_public_site_raises_on_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_public_site(_load_fixture(), tmp_path / "out", source_dir=tmp_path / "nope")


def test_public_cli_reads_dashboard_json_and_writes_site(tmp_path: Path) -> None:
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


def test_public_index_has_valkey_ci_health_branding(tmp_path: Path) -> None:
    site_dir = tmp_path / "out"
    build_public_site(_load_fixture(), site_dir)

    index_html = (site_dir / "index.html").read_text(encoding="utf-8")
    assert "Health" in index_html
    assert "Operator Console" not in index_html


def test_public_index_has_no_diagnostics_link(tmp_path: Path) -> None:
    """The public site has no link to the operator diagnostics page."""
    site_dir = tmp_path / "out"
    build_public_site(_load_fixture(), site_dir)

    index_html = (site_dir / "index.html").read_text(encoding="utf-8")
    assert "Agent diagnostics" not in index_html
    assert "operator/" not in index_html


def test_public_index_has_no_diagnostics_nav(tmp_path: Path) -> None:
    site_dir = tmp_path / "out"
    build_public_site(_load_fixture(), site_dir)

    index_html = (site_dir / "index.html").read_text(encoding="utf-8")
    assert 'data-nav-link="diagnostics"' not in index_html


def _read_public_pages(tmp_path: Path) -> dict[str, str]:
    """Build the public site and return the content of all HTML pages."""
    site_dir = tmp_path / "out"
    build_public_site(_load_fixture(), site_dir)
    pages = {}
    for name in ("index.html", "review.html", "fuzzer.html"):
        pages[name] = (site_dir / name).read_text(encoding="utf-8")
    return pages


def test_public_pages_contain_no_operator_language(tmp_path: Path) -> None:
    """The public site HTML shell must not contain agent-internal terminology."""
    pages = _read_public_pages(tmp_path)
    banned = [
        "Operator Console",
    ]
    for page_name, html in pages.items():
        for term in banned:
            assert term not in html, f"{page_name} contains banned term: {term!r}"


def test_public_js_pages_contain_no_agent_internals(tmp_path: Path) -> None:
    """The public JS page modules must not reference agent-internal concepts."""
    site_dir = tmp_path / "out"
    build_public_site(_load_fixture(), site_dir)

    banned_in_daily = ["signalCards", "signal-cards"]
    daily_js = (site_dir / "assets" / "js" / "pages" / "daily.js").read_text(encoding="utf-8")
    for term in banned_in_daily:
        assert term not in daily_js, f"daily.js contains banned term: {term!r}"

    banned_in_prs = ["Replay", "Acceptance", "Coverage gaps", "Findings"]
    prs_js = (site_dir / "assets" / "js" / "pages" / "prs.js").read_text(encoding="utf-8")
    for term in banned_in_prs:
        assert term not in prs_js, f"prs.js contains banned term: {term!r}"

    banned_in_fuzzer = ["Runs seen", "Runs analyzed"]
    fuzzer_js = (site_dir / "assets" / "js" / "pages" / "fuzzer.js").read_text(encoding="utf-8")
    for term in banned_in_fuzzer:
        assert term not in fuzzer_js, f"fuzzer.js contains banned term: {term!r}"


def test_public_router_has_no_diagnostics_route(tmp_path: Path) -> None:
    site_dir = tmp_path / "out"
    build_public_site(_load_fixture(), site_dir)

    router_js = (site_dir / "assets" / "js" / "router.js").read_text(encoding="utf-8")
    assert "diagnostics" not in router_js

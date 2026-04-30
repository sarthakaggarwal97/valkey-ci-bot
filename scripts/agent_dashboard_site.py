"""Publish the static dashboard site.

This module does the bare minimum the frontend needs:
  1. Copy the checked-in ``dashboard-app/`` directory to ``<site_dir>/``.
  2. Write the dashboard payload to ``<site_dir>/data/dashboard.json``.

All rendering happens in the browser via the ES modules under
``dashboard-app/assets/js/``. The JSON contract lives in
``docs/dashboard-schema.md`` and is enforced by
``scripts.validate_dashboard_schema``.

This replaces a 96KB module that used to generate every HTML page via Python
string concatenation. See git history if you need the old behavior.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Dict

JsonObject = Dict[str, Any]

_DEFAULT_SOURCE = Path(__file__).resolve().parent.parent / "dashboard-app"


def build_site(
    dashboard: JsonObject,
    site_dir: Path,
    *,
    source_dir: Path | None = None,
) -> None:
    """Write the static dashboard site.

    Copies every file from ``source_dir`` (default ``dashboard-app/``) into
    ``site_dir``, then writes the dashboard JSON to
    ``<site_dir>/data/dashboard.json``.

    The source and output directories must differ — the function refuses to
    copy into its own source to avoid self-referential writes.
    """
    src = (source_dir or _DEFAULT_SOURCE).resolve()
    dst = Path(site_dir).resolve()
    if not src.is_dir():
        raise FileNotFoundError(
            "source directory not found: {}".format(src)
        )
    if src == dst:
        raise ValueError(
            "source and output directories must differ (got {} for both)".format(src)
        )

    dst.mkdir(parents=True, exist_ok=True)
    # Copy every file from source, preserving the directory structure.
    # Existing files in dst are overwritten.
    for entry in src.rglob("*"):
        rel = entry.relative_to(src)
        target = dst / rel
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)

    data_dir = dst / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "dashboard.json").write_text(
        json.dumps(dashboard, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard-json", required=True,
                        help="Path to the dashboard JSON payload.")
    parser.add_argument("--site-dir", default="dashboard-site",
                        help="Output directory for the static site.")
    parser.add_argument("--source-dir", default=None,
                        help="Override the dashboard-app/ source directory.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dashboard = json.loads(Path(args.dashboard_json).read_text(encoding="utf-8"))
    source_dir = Path(args.source_dir) if args.source_dir else None
    build_site(dashboard, Path(args.site_dir), source_dir=source_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

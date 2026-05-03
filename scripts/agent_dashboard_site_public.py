"""Publish the public-facing Valkey CI Health dashboard site.

Sibling of ``agent_dashboard_site`` that copies ``dashboard-app-public/``
instead of ``dashboard-app/``.  The public site strips agent-internal
panels (diagnostics, AI reliability, replay acceptance) and rebrands the
sidebar as "Valkey CI Health" for community consumption.

Both sites read the same ``data/dashboard.json`` — the difference is
purely in the client-side rendering.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Dict

JsonObject = Dict[str, Any]

_DEFAULT_SOURCE = Path(__file__).resolve().parent.parent / "dashboard-app-public"


def build_public_site(
    dashboard: JsonObject,
    site_dir: Path,
    *,
    source_dir: Path | None = None,
) -> None:
    """Write the public-facing Valkey CI Health site.

    Copies every file from ``source_dir`` (default ``dashboard-app-public/``)
    into ``site_dir``, then writes the dashboard JSON to
    ``<site_dir>/data/dashboard.json``.
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
    parser.add_argument("--site-dir", default="dashboard-site-public",
                        help="Output directory for the public site.")
    parser.add_argument("--source-dir", default=None,
                        help="Override the dashboard-app-public/ source directory.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dashboard = json.loads(Path(args.dashboard_json).read_text(encoding="utf-8"))
    source_dir = Path(args.source_dir) if args.source_dir else None
    build_public_site(dashboard, Path(args.site_dir), source_dir=source_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

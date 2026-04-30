"""Generate redirect-stub HTML files for old dashboard URLs.

Drops small <meta refresh> HTML files into ``dashboard-app/`` for every
legacy URL so bookmarks keep working after the SPA rewrite.
"""
from __future__ import annotations

import sys
from pathlib import Path

# old_name -> hash route in the new SPA
REDIRECTS = {
    "daily.html": "#/daily",
    "review.html": "#/prs",
    "fuzzer.html": "#/fuzzer",
    "diagnostics.html": "#/diagnostics",
    "ops.html": "#/diagnostics",
    "flaky.html": "#/daily/campaigns",
    "acceptance.html": "#/prs/replay",
    "ai.html": "#/diagnostics/ai-reliability",
}


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Valkey CI Agent Dashboard — redirecting</title>
  <meta http-equiv="refresh" content="0; url=index.html{hash_route}">
  <link rel="canonical" href="index.html{hash_route}">
  <link rel="stylesheet" href="assets/css/tokens.css">
  <link rel="stylesheet" href="assets/css/base.css">
  <style>
    body {{ display: grid; place-items: center; min-height: 100vh; }}
    .redirect-card {{
      max-width: 520px;
      padding: 28px;
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      background: var(--surface);
      text-align: center;
      box-shadow: var(--shadow);
    }}
    .redirect-card img {{
      width: 176px;
      background: #fff;
      border-radius: 12px;
      padding: 10px 14px;
      margin-bottom: 14px;
    }}
    .redirect-card h1 {{ margin: 0 0 8px; color: var(--text-heading); font-size: 20px; }}
    .redirect-card p {{ color: var(--text-muted); margin: 6px 0; }}
  </style>
</head>
<body>
  <main class="redirect-card">
    <img src="assets/valkey-horizontal.svg" alt="Valkey">
    <h1>This page has moved</h1>
    <p>The dashboard is now a single-page app.</p>
    <p><a href="index.html{hash_route}">Open the updated page</a></p>
  </main>
</body>
</html>
"""


def main() -> int:
    app_dir = Path("dashboard-app")
    if not app_dir.is_dir():
        print("error: dashboard-app/ not found — run from valkey-ci-agent/", file=sys.stderr)
        return 1
    for name, route in REDIRECTS.items():
        (app_dir / name).write_text(_TEMPLATE.format(hash_route=route), encoding="utf-8")
        print("wrote dashboard-app/{}".format(name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

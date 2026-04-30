# dashboard-app/

The source for the Valkey CI Agent community dashboard. Plain HTML, CSS, and
vanilla ES modules — no build toolchain.

## How it fits together

```
dashboard-app/                                (source, checked into git)
├── index.html                                single-page app shell
├── {daily,review,fuzzer,diagnostics,...}.html redirect stubs for old URLs
├── assets/
│   ├── css/{tokens,base,components}.css      design tokens + layout + UI
│   ├── js/
│   │   ├── app.js                             entry: fetch + schema check
│   │   ├── router.js                          hash-based SPA router
│   │   ├── theme.js                           theme toggle + sidebar polish
│   │   ├── dom.js                             XSS-safe DOM builder (el/text)
│   │   ├── utils.js                           formatters, GitHub URL helpers
│   │   ├── components/                        reusable UI pieces
│   │   └── pages/                             per-route renderers
│   └── valkey-horizontal.svg                  logo

dashboard-site/                               (generated, never in git)
├── (copy of dashboard-app/)
└── data/dashboard.json                       written by scripts.agent_dashboard_site
```

The Python side only builds `dashboard.json` against the schema in
`docs/dashboard-schema.md`. `scripts.agent_dashboard_site` copies this
directory to the output and drops the JSON into `data/`. The JS then reads it
client-side.

## Schema contract

The JSON shape is versioned. The frontend checks `schema_version === 1` on
every page load and shows an error banner if the data layer is newer or older.

See `../docs/dashboard-schema.md` for the full contract and
`../fixtures/dashboard/{full,empty,partial}.json` for reference fixtures.

## XSS safety

The app never sets `innerHTML` on untrusted data. All DOM is built through
`dom.js`:

```js
import { el } from './dom.js';
el('p', { class: 'note' }, [
  'User title: ',
  el('strong', {}, [prTitleFromAPI]),   // text node — cannot inject markup
]);
```

Any string passed as a child becomes a text node, so `<script>alert(1)</script>`
renders as literal text. The fixtures include adversarial payloads to verify
this on every change.

## Local preview

```
# Stage the site manually:
mkdir -p /tmp/dashboard-site/data
cp -r dashboard-app/. /tmp/dashboard-site/
cp fixtures/dashboard/full.json /tmp/dashboard-site/data/dashboard.json
cd /tmp/dashboard-site && python3 -m http.server 8080
# open http://localhost:8080/
```

ES modules require `file://` access to fail with CORS, so you do need an HTTP
server locally — `python3 -m http.server` is enough.

## Adding a new page

1. Add a renderer at `assets/js/pages/<name>.js` that exports
   `render(container, dashboard, ctx)`.
2. Register it in `assets/js/router.js` (`ROUTES` map) and add a nav link to
   `index.html`.
3. Use the components in `assets/js/components/` — do not roll new DOM helpers.

## Adding a new section to an existing page

Import the `panel` helper and compose with existing components. Every table
should use `components/table.js` so sort/filter/ARIA come for free. Use
`chip`, `metric`, `statGrid`, `externalLink`, `sparkline`, `heatmap`, and
`wowTrends` where applicable.

## Accessibility

- `index.html` ships a skip-to-content link, semantic landmarks (`<aside>`,
  `<nav>`, `<main>`), and proper `<title>`.
- Tables use `<th scope="col">` and update `aria-sort` on each header.
- Interactive icons have `aria-label`. Sparkline dots are keyboard-focusable.
- The drawer traps focus, returns focus on close, and closes on ESC.
- All interactive elements have `:focus-visible` rings using the design
  tokens.
- `prefers-reduced-motion` disables drawer slide animations.

## CSS architecture

- `tokens.css` — colors, spacing, radii, fonts. Dark is default, light has
  both `[data-theme="light"]` and `@media (prefers-color-scheme: light)`
  overrides.
- `base.css` — reset, body, layout grid, responsive breakpoints, skip-link.
- `components.css` — one file with every component class. Not split further
  because the total is modest (~800 lines) and a single stylesheet keeps
  load simple.

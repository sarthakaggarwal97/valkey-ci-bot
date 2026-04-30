# Valkey CI Dashboard — User Guide

The dashboard is a read-only view of how Valkey's CI is doing and what the CI
agent has been working on. This guide explains what each section means and how
to use it.

## Getting there

Public GitHub Pages site for the agent repo, or open the
`dashboard-site/` artifact from any run of the
`Publish Dashboard Site` workflow.

The dashboard works in any recent browser. It reads a single JSON file
(`data/dashboard.json`) client-side and renders the pages in JavaScript.

## The sidebar

- **Status card** — When the JSON was generated and how long ago. Color-coded:
  - Green: fresh (less than 6 hours old)
  - Amber: stale (6–12 hours old)
  - Red: very stale (over 12 hours old) — the refresh workflow may be broken.
- **Light/Dark toggle** — Remembers your choice. Falls back to your
  system preference.
- **Share view** — Copies the current URL, including filters and the active
  page. Good for linking a specific cell or campaign in Slack/chat.

## Pages

The dashboard has four pages in the sidebar:

### Daily CI

Where most community questions are answered.

- **Signal map** — Four cards at the top linking to each page with the headline
  stat. Use this as a quick status check.
- **Header metrics** — Tracked days, total runs, failed runs (with pass rate),
  and active remediation campaigns.
- **Failure heatmap** — The big grid. Rows are failure names, columns are
  dates, red intensity is how many times that failure fired on that day.
  - **Hover** a cell for the date, count, and error details.
  - **Click** a red cell to open the drill-down drawer with the full context
    and links to the failed jobs on GitHub.
  - The **Run status row** at the top shows ✓ / ✗ / ⋯ for each day overall.
  - If there are multiple workflows (e.g. `daily.yml` + `weekly.yml`), a tab
    switcher appears above the heatmap so weekly regressions don't distort
    the daily view.
- **Week-over-Week trends** — Compares the last 7 days to the 7 before them.
  Shows new failures, resolved failures, and biggest movers.
- **Recent monitored runs** — One row per run, sortable and filterable. Each
  commit SHA is a link to GitHub; hover it to see the commit message. "Commits
  since prev" shows the SHAs between this run and the previous one, so you can
  see what landed before a regression.
- **Active remediation campaigns** — Each flaky failure the agent is trying
  to fix. Status chip shows where the loop is (active / validated / queued
  / landed / abandoned). If the fix has a proof run, "Proof" links to it.

### PRs

How well the agent is reviewing pull requests.

- **Tracked pull requests** — PRs the reviewer has durable state for, with
  links to the summary comment and the review comments on GitHub.
- **Replay review cases** — The acceptance-harness results. "Coverage" says
  whether the review claimed enough about the PR's changed files. "Verdict"
  says whether the agent hit the expected outcome.
- **Workflow contract cases** — Acceptance checks for the agent's own
  workflows (does each workflow pass the expected policy checks?).

### Fuzzer

Anomaly surface from the fuzzer monitor.

- **Root-cause mix** — Horizontal bar chart of the top root-cause categories
  seen in anomalous runs.
- **Status + issue actions** — Raw counts of how runs were classified and
  which GitHub issues were created / updated.
- **Recent anomalies** — One row per anomalous run. Click a row to open the
  full summary drawer. Each run links back to the GitHub Actions run and
  each issue action links to the GitHub issue.

### Diagnostics

Operator-level state. Useful when the dashboard itself looks wrong, or when
something appears stale.

- **Data coverage** — The most important diagnostics section. For every data
  source the dashboard expects (daily health, review state, fuzzer runs, AI
  metrics, etc.) this shows `available`, `partial`, or `missing`. A missing
  source means that panel on another page is empty _because the data never
  arrived_, not because there's nothing to report.
- **Incident queue** — The failure-store entries the agent is currently
  tracking.
- **Event stream** — Recent entries from the append-only event ledger. This
  is the audit log for what the agent did — PRs created, validations run,
  proofs dispatched, etc.
- **Monitor watermarks** — The last run each monitor processed, per workflow.
  If a watermark hasn't moved in a long time, a monitor may be stuck.
- **AI reliability** — Bedrock call counts, schema successes, retries, token
  usage, and prompt-safety guard coverage. Use this to spot drift.
- **Input warnings** — Raw warnings from the data loader (missing files,
  parse errors). These flow into the "missing" / "partial" classification
  on the coverage table.

## How to read the heatmap

Each cell represents one failure on one day:

- Empty cell: that failure did not fire that day.
- Red cell: it fired at least once. Darker red = more occurrences.
- Dash (`—`): there was no run at all that day (workflow was skipped or
  data is missing).
- The left column shows the failure name. The next column is how many days
  in the window that failure fired at least once.

Click a red cell to open the details drawer. Inside:
- The specific date, count, and failing job names.
- Links to each failing job on GitHub.
- Left / Right arrow keys jump to adjacent cells without closing the drawer.

## How to share a specific view

- Any hash-route, sort, or filter is reflected in the URL. Use the
  **Share view** button in the sidebar to copy the current URL.
- Example: open Daily CI, filter campaigns by "memory", click share — the
  recipient opens the same filtered view.

## What to do when something looks wrong

1. Check the **status card** at the top-left for staleness.
2. Open **Diagnostics → Data coverage** to see which sources are missing or
   partial.
3. Check **Diagnostics → Input warnings** for parse errors.
4. If a specific page is empty and coverage says `available`, file an issue on
   the agent repo with the current URL.

## What the readiness chip means

On the PRs page hero, the **Readiness** chip summarizes the replay
acceptance scorecard into one of:

- `pilot-ready` — acceptance is passing end-to-end; the agent is a reasonable
  default reviewer for this repo.
- `needs-work` — some replay cases are failing; see the Replay review cases
  table for detail.
- `unknown` — no acceptance payload was available for this report.

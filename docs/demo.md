# Live Demo — Valkey CI Agent

A hands-on tour of all five production flows. Everything in this demo runs against `sarthakaggarwal97/*` forks — **nothing touches `valkey-io/valkey`**.

## Safety guardrails (read first)

- **Target repo for all writes:** `sarthakaggarwal97/valkey`, `sarthakaggarwal97/valkey-fuzzer`, `sarthakaggarwal97/valkey-ci-agent`. Never `valkey-io/*`.
- **Read-only references to upstream are fine** (the agent reads `valkey-io/valkey` PRs for context) but does not post there.
- **Mirror PRs** on `sarthakaggarwal97/valkey` are `[Eval] Agent review bench` drafts. Body text: `Mirror for CI agent eval. Do not merge.` No upstream PR numbers embedded.
- **Close demo PRs when done** to avoid clutter.

## One-click bundle

The simplest path: dispatch the Demo workflow and it will package all five surfaces into a browsable HTML bundle.

```bash
gh workflow run demo-valkey-agent.yml \
  --repo sarthakaggarwal97/valkey-ci-agent \
  --field run_dashboard=true \
  --field run_replay=true \
  --field run_daily=true \
  --field daily_dry_run=true \
  --field run_fuzzer=true \
  --field fuzzer_dry_run=true
```

When it finishes (~10 min), download the `demo-bundle` artifact. Open `bundle/index.html` locally — it links to every live surface.

## Per-flow demos

### 1. PR Reviewer — the flagship

**Inputs:** a PR on `sarthakaggarwal97/valkey`. Post the upstream PR content as a mirror using the eval tooling (see `docs/eval.md`).

**Trigger:**
```bash
# PR 189 is the default mirror of upstream #3591 (NULL pointer in streamTrim)
gh workflow run review-external-pr.yml \
  --repo sarthakaggarwal97/valkey-ci-agent \
  --field target_repo=sarthakaggarwal97/valkey \
  --field pr_number=189
```

**Expected output:**
- A summary comment on the PR (general summary, not inline)
- Inline review comments (1–8 depending on PR complexity, avg ~2.5 after skeptic pass)
- Every comment reads like a senior maintainer wrote it (no forensic patterns like "wc -l" or "the diff shows N bytes")
- Run completes in 10–25 minutes

**Cost:** $3–8 per review (2 Claude Opus calls: stage 1 deep review + skeptic pass).

**Latest evaluation:** see [eval/RESULTS.md](../eval/RESULTS.md) for the 30-PR shadow comparison.

**External validation signal:** On PR [#143](https://github.com/sarthakaggarwal97/valkey/pull/143) (v1/v2 shadow of upstream AOF-integrity PR #3565), upstream author @sumitk163 accepted 7 of the 10 bot-posted findings (`Fixed.`, `Updated.`).

### 2. Daily CI Failure Analyzer + Fix Generator

**What it does:** watches `valkey-io/valkey` scheduled CI, finds failures, generates and validates fixes, opens PRs on `sarthakaggarwal97/valkey` (the fork).

**Dry-run trigger (safe):**
```bash
gh workflow run monitor-valkey-daily.yml \
  --repo sarthakaggarwal97/valkey-ci-agent \
  --field dry_run=true \
  --field max_runs=3
```

This will identify recent failures but NOT post issues or PRs. Output: a `monitor-result.json` artifact + updated dashboard.

**Live mode (creates fix PRs on `sarthakaggarwal97/valkey`):**
```bash
gh workflow run monitor-valkey-daily.yml \
  --repo sarthakaggarwal97/valkey-ci-agent \
  --field dry_run=false \
  --field max_runs=1
```

**Cost per failure analyzed:** $2–5. Per fix PR generated: additional $3–8.

### 3. Fuzzer Triage

**What it does:** watches `valkey-io/valkey-fuzzer` runs, computes stable incident fingerprints, creates GitHub issues on `valkey-io/valkey-fuzzer` (allowed — fuzzer repo is explicitly in scope).

**Dry-run trigger:**
```bash
gh workflow run monitor-valkey-fuzzer.yml \
  --repo sarthakaggarwal97/valkey-ci-agent \
  --field dry_run=true
```

**Live mode:** triggered on a schedule (every 4 hours). Filed issues show up at https://github.com/valkey-io/valkey-fuzzer/issues.

**Key demo angle:** show two issues from different fuzzer runs that collapse to the **same incident fingerprint** (hex addresses, node numbers, and slot counts are normalized). Proves the fingerprinting approach.

### 4. Backport Agent

**What it does:** cherry-picks merged PRs onto release branches (8.1, 9.0, etc.), resolves conflicts via Claude Code, opens backport PRs on `sarthakaggarwal97/valkey`.

**Demo trigger — weekly sweep (dry-run):**
```bash
gh workflow run weekly-backport-sweep.yml \
  --repo sarthakaggarwal97/valkey-ci-agent \
  --field dry_run=true
```

**Demo trigger — manual backport:**
```bash
gh workflow run manual-backport.yml \
  --repo sarthakaggarwal97/valkey-ci-agent \
  --field pr_url=https://github.com/valkey-io/valkey/pull/3416 \
  --field target_branch=8.1
```

The manual-backport workflow reads the upstream PR and creates a backport PR on `sarthakaggarwal97/valkey`. Does NOT post to `valkey-io/valkey`.

**Key demo angle:** inspect `scripts/backport_risk.py`. The agent scores each backport (low/medium/high) based on file paths touched, conflict count, older-release-branch bonus. High-risk backports get a PR comment warning maintainers.

### 5. Capability Dashboard

**What it does:** publishes a static multi-page dashboard to GitHub Pages summarizing all the above.

**Live URL:** [https://sarthakaggarwal97.github.io/valkey-ci-agent/](https://sarthakaggarwal97.github.io/valkey-ci-agent/)

**Force-refresh:**
```bash
gh workflow run agent-dashboard.yml \
  --repo sarthakaggarwal97/valkey-ci-agent
```

**Key pages:**
- `/` — Overview: trend watch, event stream, data coverage
- `/daily.html` — Daily CI: failure heatmap, recent runs, active campaigns
- `/prs.html` — PR review: tracked state, replay acceptance, workflow contracts
- `/fuzzer.html` — Fuzzer: anomalies, root cause categories, issue status
- `/ops.html` — Ops: incident queue, event ledger, rate limiter state

**Key demo angle:** everything is **static HTML**. No database, no app server, no runtime to secure. Refreshed by workflows on schedule and manual trigger.

## Cost summary (ballpark)

| Flow | Cost per run | Typical frequency |
|------|--------------|-------------------|
| PR review | $3–8 | Per labeled PR (if adopted opt-in) |
| Daily CI analyzer | $2–5 per failure analyzed | Daily, batched |
| Fuzzer triage | $1–3 per run analyzed | Every 4 hours |
| Backport sweep | $2–10 depending on conflicts | Weekly |
| Dashboard refresh | ~$0.10 (no LLM) | Every 6 hours |

## Demo narratives

Pick one based on the audience:

### "It actually works" narrative (15 minutes)
1. Open the dashboard site — show live state.
2. Trigger one PR review (pick a medium-complexity mirror PR). Show it post inline comments.
3. Show the `eval/RESULTS.md` file — 30-PR evaluation against real maintainer decisions.
4. Close with PR [#143](https://github.com/sarthakaggarwal97/valkey/pull/143) where upstream author accepted 7/10 findings.

### "Five flows, one platform" narrative (30 minutes)
1. Dashboard (2 min) — pattern-of-life summary.
2. PR review (10 min) — trigger live, wait for completion, walk through the findings.
3. Fuzzer triage (5 min) — show a fingerprint dedup example from valkey-io/valkey-fuzzer issues.
4. Backport (5 min) — show `backport_risk.py` scoring + a manual backport.
5. Daily CI (5 min) — dry-run + show historical proof PR links in `bot-data` branch.
6. Q&A (3 min).

### "Technical deep dive" narrative (45 minutes)
Start from `docs/architecture.md`. Walk through the five flows using it as a map. End with the reviewer-specific deep dive in `docs/reviewer.md` and the eval methodology in `docs/eval.md`.

## Troubleshooting

**Review workflow exits non-zero:** Check logs for `ReviewGenerationError`. Common causes:
- Claude hit `max_turns=240` (uncommon; was common before v4.1 which bumped from 150)
- Review state on `bot-data` says already-reviewed — reset by deleting the PR's entry from `review-state.json`

**Dashboard artifact empty:** check `bot-data` branch has recent `failure-store.json` + `monitor-state.json` snapshots.

**Demo bundle workflow fails with permission errors:** ensure `VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH` is NOT set (default blocks writes to `valkey-io/*`).

## References

- [docs/architecture.md](architecture.md) — system overview
- [docs/reviewer.md](reviewer.md) — PR reviewer deep dive
- [docs/eval.md](eval.md) — evaluation framework
- [eval/RESULTS.md](../eval/RESULTS.md) — latest 30-PR results
- [CONTRIBUTING.md](../CONTRIBUTING.md) — dev setup, test conventions

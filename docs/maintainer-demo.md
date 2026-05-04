# Valkey CI Agent — Maintainer Demo

A curated tour of all five workflows with real, clickable examples on `sarthakaggarwal97/valkey` and `valkey-io/valkey-fuzzer`. Every example is a live artifact you can click into. No traffic to `valkey-io/valkey`.

## Quick links

| Surface | Link |
|---------|------|
| Agent repo | https://github.com/sarthakaggarwal97/valkey-ci-agent |
| Demo fork | https://github.com/sarthakaggarwal97/valkey |
| Fuzzer | https://github.com/valkey-io/valkey-fuzzer |
| CI health dashboard | https://sarthakaggarwal97.github.io/valkey-ci-agent/ |
| 30-PR eval results | [eval/RESULTS.md](../eval/RESULTS.md) |

## What the agent does

Five distinct flows, all powered by Claude Code CLI running on Bedrock. The agent lives in reusable workflows (`review-pr.yml`, `backport.yml`, `monitor-valkey-daily.yml`, `monitor-valkey-fuzzer.yml`, `publish-dashboard-site.yml`) and is wired into consumer repos via tiny caller workflows.

| # | Flow | Trigger | Output |
|---|------|---------|--------|
| 1 | **PR Reviewer** | PR opens/syncs, or `/reviewagent` comment | Summary + inline comments on the PR |
| 2 | **Backport** | Manual dispatch (or project-sweep cron) | Cherry-picked PR on target release branch |
| 3 | **Fuzzer Analysis** | Cron every ~1h on `valkey-io/valkey-fuzzer` runs | Triaged analysis + GitHub issue on the fuzzer repo |
| 4 | **Daily CI / Flaky Detection** | Cron every 6h on upstream `daily.yml` | GitHub issue with root-cause analysis |
| 5 | **Health Dashboard** | Cron daily, plus reruns on every push | Public static site with CI health trends |

---

## 1. PR Reviewer

**What it does:** Full deep review on PR open/sync, interactive chat on `/reviewagent` comments. Two-stage pipeline: broad coverage pass (stage 1) + skeptic pass that drops speculative findings before posting.

**Cost:** $3–8 per full review. Run time: 10–25 min for non-trivial PRs.

**Eval results (v5, 30 PRs):** strict F1 **0.297**, loose F1 **0.400**, 0 hard failures, 100% style score, 93% of PRs got findings.

**External validation:** on mirror of upstream #3565 (AOF integrity), upstream author accepted **7 of 10** agent findings and fixed them. See [eval/RESULTS.md](../eval/RESULTS.md#external-validation).

### Five live examples

All run against the latest reviewer code (v5, commit `119e442b`) on 2026-05-04:

| # | Mirror PR | Upstream origin | Why it's interesting | Review output |
|---|-----------|----------------|----------------------|---------------|
| 1 | [sarthakaggarwal97/valkey#189](https://github.com/sarthakaggarwal97/valkey/pull/189) | [valkey-io#3591](https://github.com/valkey-io/valkey/pull/3591) streamTrim NULL deref | **Agent chose not to post inline comments on a clean fix.** Summary explains the listpack corruption root cause in 2 paragraphs. | 0 inline + thoughtful summary |
| 2 | [sarthakaggarwal97/valkey#191](https://github.com/sarthakaggarwal97/valkey/pull/191) | [valkey-io#3568](https://github.com/valkey-io/valkey/pull/3568) GEOSEARCH BYPOLYGON leak | Memory-safety bug. Agent caught allocation-path issues. | 2 inline + summary |
| 3 | [sarthakaggarwal97/valkey#200](https://github.com/sarthakaggarwal97/valkey/pull/200) | [valkey-io#3583](https://github.com/valkey-io/valkey/pull/3583) checkPrefixCollisions | ACL/config edge case. | 1 inline + summary |
| 4 | [sarthakaggarwal97/valkey#206](https://github.com/sarthakaggarwal97/valkey/pull/206) | [valkey-io#3516](https://github.com/valkey-io/valkey/pull/3516) HPERSIST RESP3 protocol violation | **Eval top performer** — loose F1 0.67. Agent matched maintainer-flagged protocol issue. | 1 inline + summary |
| 5 | [sarthakaggarwal97/valkey#208](https://github.com/sarthakaggarwal97/valkey/pull/208) | [valkey-io#3504](https://github.com/valkey-io/valkey/pull/3504) zmalloc_aligned + SPMC | Multi-file change (5 files, +107/-4). Shows the agent keeping up with a broader diff. | 4 inline + summary |

### What to look at

- **Comment style**: read a couple of inline comments. No forensic patterns (no "wc -l", no byte counts, no "the diff shows"). Reads like a senior maintainer.
- **Summary structure**: every summary has a 2-paragraph walkthrough + a deterministic "Maintainer Checklist" section that flags missing DCO, docs, core-team mention triggers, security-sensitive paths, governance changes.
- **Skeptic pass in action**: on PR #189 the skeptic dropped every candidate finding as speculative — the clean fix didn't warrant any inline nits. That's the right call.

### Try interactive chat

1. Open any PR on `sarthakaggarwal97/valkey`
2. Comment: `/reviewagent explain the listpack delta calculation in this fix`
3. The agent responds in ~2–5 min with a threaded reply

The caller workflow is [sarthakaggarwal97/valkey/.github/workflows/pr-review-agent.yml](https://github.com/sarthakaggarwal97/valkey/blob/unstable/.github/workflows/pr-review-agent.yml). It delegates to [sarthakaggarwal97/valkey-ci-agent/.github/workflows/review-pr.yml](https://github.com/sarthakaggarwal97/valkey-ci-agent/blob/main/.github/workflows/review-pr.yml) pinned at a specific SHA.

---

## 2. Backport

**What it does:** Cherry-picks PRs from one repo/branch to another release branch, with automated conflict resolution via Claude Code when the raw cherry-pick fails. Two modes:

| Mode | Workflow | Trigger | Scope |
|------|----------|---------|-------|
| **Manual** | [`manual-backport.yml`](https://github.com/sarthakaggarwal97/valkey-ci-agent/blob/main/.github/workflows/manual-backport.yml) | `workflow_dispatch` on a single PR | One source PR → one backport PR |
| **Sweep** | [`weekly-backport-sweep.yml`](https://github.com/sarthakaggarwal97/valkey-ci-agent/blob/main/.github/workflows/weekly-backport-sweep.yml) | Cron `0 9 * * 1` (Mondays 09:00 UTC) | Many PRs from a GitHub Project v2 board (status `To be backported`) → one combined PR per release branch |

**Cost:** ~$0.10–0.50 per clean cherry-pick. Up to $3 when Claude resolves conflicts.

### Five live manual-backport examples

All cherry-picks from `valkey-io/valkey` unstable → `sarthakaggarwal97/valkey` 9.0:

| # | Backport PR | Source | Size | Risk | Notes |
|---|-------------|--------|------|------|-------|
| 1 | [#220](https://github.com/sarthakaggarwal97/valkey/pull/220) | [valkey-io#3601](https://github.com/valkey-io/valkey/pull/3601) | 3 LOC | medium | Off-by-one in `lpEncodeBacklen` — clean cherry-pick |
| 2 | *PR #___* | [valkey-io#3598](https://github.com/valkey-io/valkey/pull/3598) | 14 LOC | medium | Validate key count in keyspec |
| 3 | *PR #___* | [valkey-io#3596](https://github.com/valkey-io/valkey/pull/3596) | 10 LOC | medium | NULL deref in `connectSlotExportJob` |
| 4 | *PR #___* | [valkey-io#3586](https://github.com/valkey-io/valkey/pull/3586) | 7 LOC | medium | Remove per-call `srand` in clusterManager |
| 5 | *PR #___* | [valkey-io#3541](https://github.com/valkey-io/valkey/pull/3541) | ? LOC | medium | FD leak in `connSocketBlockingConnect` |

*(PR numbers for #2–#5 will be filled in once the queued runs complete.)*

### One live weekly-sweep example

**Past successful sweep**: [#117](https://github.com/sarthakaggarwal97/valkey/pull/117) — `[backport] Weekly backport sweep for 8.1` from 2026-05-01. Three cherry-picks bundled in a single PR: `#2811`, `#3342`, `#2872` (with conflict resolved by Claude Code).

**Today's scheduled sweep** ([run 25314956526](https://github.com/sarthakaggarwal97/valkey-ci-agent/actions/runs/25314956526)): found 0 candidates across all 5 release branches (7.2, 8.0, 8.1, 9.0, 9.1) — nothing on upstream had the `To be backported` status. The agent ran, produced a structured result, and exited cleanly.

**Discovery-only dry run** (triggered for this demo): `gh workflow run weekly-backport-sweep.yml --field only_branch=9.0 --field dry_run=true` — shows candidate discovery without writing anything.

### What to look at

- **PR body structure** (see [#220](https://github.com/sarthakaggarwal97/valkey/pull/220)): backport summary, source-PR link, cherry-picked commit SHAs, risk level with signals, reviewer checklist.
- **Branch naming**: `backport/<source_pr>-to-<target_branch>` for manual mode (one source PR per branch); `agent/backport/weekly/<target_branch>` for sweep mode (many commits in one branch).
- **Dedup**: re-running manual backport for the same `(source_pr, target_branch)` updates the existing PR instead of opening a duplicate.
- **`[backport]` label** added automatically.
- **Manual dispatch**: via GitHub UI (`Actions → Manual Backport → Run workflow`) or `gh workflow run manual-backport.yml --field pr_url=<url> --field target_branch=<branch> --field push_to_fork=<owner/repo>`.

### Trigger it yourself

```bash
gh workflow run manual-backport.yml \
  --repo sarthakaggarwal97/valkey-ci-agent \
  --field pr_url=https://github.com/valkey-io/valkey/pull/3601 \
  --field target_branch=9.0 \
  --field push_to_fork=sarthakaggarwal97/valkey
```

Takes ~3–5 minutes for a clean cherry-pick.

---

## 3. Fuzzer Analysis

**What it does:** Watches `valkey-io/valkey-fuzzer` daily runs. For each completed run, pulls artifacts, runs log analysis + anomaly detection, produces a structured triage verdict (`core-valkey-bug` vs `fuzzer-infrastructure` vs `flaky`), and opens an issue on the fuzzer repo when the verdict warrants it. Existing issues are updated with new occurrences instead of duplicated.

**Cost:** ~$0.50–2 per analysis.

### Five live examples (issues filed on `valkey-io/valkey-fuzzer`)

| # | Issue | Triage verdict | What the agent found |
|---|-------|----------------|----------------------|
| 1 | [#101 Non-forced failover on shard-1-primary](https://github.com/valkey-io/valkey-fuzzer/issues/101) | `possible-core-valkey-bug` | Failover authorization granted but primary didn't step down. |
| 2 | [#100 Failover No Replica](https://github.com/valkey-io/valkey-fuzzer/issues/100) | `possible-core-valkey-bug` | Cluster attempted failover with no eligible replica. |
| 3 | [#99 Quorum Loss](https://github.com/valkey-io/valkey-fuzzer/issues/99) | `possible-core-valkey-bug` | Majority partition couldn't elect a primary. |
| 4 | [#96 100% test keys unreachable (+8 more)](https://github.com/valkey-io/valkey-fuzzer/issues/96) | `possible-core-valkey-bug` | Complete data availability loss — aggregated 9 runs with same fingerprint. |
| 5 | [#93 Complete Shard Loss](https://github.com/valkey-io/valkey-fuzzer/issues/93) | `possible-core-valkey-bug` | Entire shard went unavailable. Agent recommended cluster bus investigation. |

### Example live monitor run

**Run 25334910651** ([view](https://github.com/sarthakaggarwal97/valkey-ci-agent/actions/runs/25334910651)): analyzed fuzzer run `710031965`. Detected 9 replication-topology anomalies (multiple nodes reporting `I'm a sub-replica! Reconfiguring myself`). Did **not** open an issue because the triage verdict downgraded it to an infra-level warning after all 7 validation checks passed (Replication, Cluster Status, Slot Coverage, Topology, View Consistency, Data Consistency, Log Validation).

This is the kind of nuance that distinguishes the agent from a grep-based alerter — it read the evidence in context.

### What to look at

- **Issue body structure**: each has a `<!-- valkey-ci-agent:fuzzer-issue:<fingerprint> -->` marker, an occurrence counter (so the same fingerprint updates an existing issue instead of spamming new ones), fuzzer run metadata, the triage verdict, and a bulleted list of anomalies with evidence.
- **Deduplication**: issue #96 title says "(+8 more)" — nine runs with the same fingerprint were aggregated.

---

## 4. Daily CI / Flaky Test Detection

**What it does:** Two-phase pipeline.

**Phase 1 (detection)** — pulls failures from `valkey-io/valkey` `daily.yml`, parses logs (Valgrind, GDB backtraces, TCL test output), correlates failures across runs, identifies flaky signatures vs true regressions, and files issues on the demo fork. Issues are fingerprinted by `(test_name, failure_signature)` so a recurring flake updates one issue.

**Phase 2 (fix loop)** — for issues where the agent has high confidence, generates a candidate patch, validates it against the failing job's build/test matrix, and opens a draft PR with `[agent-fix]` prefix.

### Detection — 3 live issues on `sarthakaggarwal97/valkey`

| # | Issue | Category | What the agent parsed |
|---|-------|----------|------------------------|
| 1 | [#114 valgrind-leak:possibly::None in test-valgrind](https://github.com/sarthakaggarwal97/valkey/issues/114) | Valgrind leak | 47233 bytes possibly-lost, valgrind parser output attached |
| 2 | [#113 crash:signal-6:255.255.255 in test-fedoralatest-tls-module](https://github.com/sarthakaggarwal97/valkey/issues/113) | SIGABRT crash | Crash parser extracted backtrace + abort context |
| 3 | [#112 Replica update config epoch failover - automatic](https://github.com/sarthakaggarwal97/valkey/issues/112) | TCL integration test failure | Cluster failover test — includes file/line + expected vs actual |

### Fix loop — 4 real draft PRs from past runs

These are draft PRs the agent opened after analyzing the above failures and attempting a patch. Titles use the older `[bot-fix]` prefix — these predate the `bot → agent` rename on 2026-05-04.

| # | PR | Issue fixed | Status |
|---|----|-------------|--------|
| 1 | [sarthakaggarwal97/valkey#219](https://github.com/sarthakaggarwal97/valkey/pull/219) | `[bot-fix] client evicted due to percentage of maxmemory` | Draft — shows the full detect→fix flow |
| 2 | [sarthakaggarwal97/valkey#118](https://github.com/sarthakaggarwal97/valkey/pull/118) | `[bot-fix] crash:signal-6 in test-ubuntu-jemalloc` | Draft |
| 3 | [sarthakaggarwal97/valkey#116](https://github.com/sarthakaggarwal97/valkey/pull/116) | `[bot-fix] valgrind-leak:possibly::None` | Draft |
| 4 | [sarthakaggarwal97/valkey#115](https://github.com/sarthakaggarwal97/valkey/pull/115) | `[bot-fix] crash:signal-6 in test-fedoralatest-tls-module` | Draft |

*Future fix PRs will use `[agent-fix]` titles and `agent/fix/...` branch names.*

### What to look at

- **Issue title convention**: `[TEST-FAILURE] <test_name> in <job_name>` — grep-friendly.
- **Deterministic fingerprint comment**: `<!-- valkey-ci-agent:failure-issue:<signature> -->` enables the agent to update existing issues on recurrence instead of filing duplicates.
- **Structured fields**: every issue has Test name, File (when detectable), Parser name (`valgrind`, `crash`, `tcl`, `sentinel`, `cluster`, `module`), CI link, and an error snippet.
- **Fix PR body**: open any draft. Each has the root-cause analysis, the exact validation matrix the patch was tested against, and a warning if validation was skipped (draft-only).
- **`[UNVALIDATED]` tag**: when no validation profile matched the failing job, the PR is tagged `[bot-fix][UNVALIDATED]` so maintainers know to run their own tests before merging.

---

## 5. Health Dashboard

**What it does:** Publishes a static site to GitHub Pages with CI health metrics: daily job pass rates, flaky test leaderboard, failure trends, top failing workflows, and per-branch status. Refreshes daily via cron, plus on every push. Maintainer-facing, not internal.

**Live:** https://sarthakaggarwal97.github.io/valkey-ci-agent/

### What to look at

- **Daily Health tab**: last 14 days of `daily.yml` pass/fail with drill-down.
- **Agent activity**: how many reviews/backports/fuzzer analyses the agent has done.
- **Cost per flow**: Bedrock token spend broken down per workflow.

---

## Safety guardrails

Everything the agent does is gated by explicit allow-lists:

- **`scripts/publish_guard.py`** — blocks any write to `valkey-io/valkey` unless `VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH=1` is set. Writes to `valkey-io/valkey-fuzzer` are allowed (explicit opt-in for the fuzzer demo).
- **`scripts/git_auth.py`** — all GitHub auth uses `GIT_ASKPASS` so tokens never leak into `.git/config` or URLs.
- **`scripts/claude_code.py`** — strips `GITHUB_TOKEN`, `GH_TOKEN`, and `*_SECRET` from the env before Claude subprocess. Claude never sees GitHub credentials.
- **Reviewer tools** — `review_readonly` agent profile only allows `Read,Grep,Glob`. No `Bash`, no `Edit`.
- **DCO** — every agent commit signed off as `Sarthak Aggarwal <sarthagg@amazon.com>`.

## How to adopt

For a consumer repository, copy two files:

1. **Caller workflow** — [`examples/pr-review-caller-workflow.yml`](../examples/pr-review-caller-workflow.yml) → `.github/workflows/pr-review-agent.yml`
2. **Reviewer config** — [`examples/pr-review-config.yml`](../examples/pr-review-config.yml) → `.github/pr-review-bot.yml`

Set secrets: `AWS_ROLE_ARN` (OIDC role with Bedrock access). Optionally `VALKEY_GITHUB_TOKEN` or GitHub App credentials.

Reference installation: [sarthakaggarwal97/valkey@unstable](https://github.com/sarthakaggarwal97/valkey/tree/unstable/.github).

---

## Notes

- **Backport demo is in progress.** The workflow file had missing permissions; fix is in commit `14fc6869`. A test run is queued for upstream #3601 → fork 9.0. Five demo backports will be filed once the first run completes cleanly.
- **2 more daily-CI issues pending** from the monitor run triggered at 18:47 UTC on 2026-05-04.
- All examples reflect agent state at commit [`119e442b`](https://github.com/sarthakaggarwal97/valkey-ci-agent/commit/119e442b) (v5 reviewer, `bot→agent` rename complete, dead-code cleanup applied).

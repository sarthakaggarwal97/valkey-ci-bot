# valkey-ci-agent

An AI agent for Valkey CI failure remediation, PR review, and automated backports.

## Features

- **CI Failure Agent** — analyzes workflow failures, generates and validates fixes, opens PRs with approval gating
- **Flaky Failure Campaigns** — persists experiment history for flaky failures, repeats validation runs, and feeds failed hypotheses back into later attempts
- **PR Review Agent** — reviews pull requests via the GitHub API, posts summaries, publishes review comments, answers follow-up questions
- **Backport Agent** — cherry-picks merged PRs onto release branches with LLM-based conflict resolution
- **Fuzzer Monitor** — watches fuzzer runs, detects anomalies, creates GitHub issues
- **Central Valkey Monitor** — watches scheduled CI runs, tracks failure history, queues validated fixes
- **Capability Dashboard** — publishes a static report for flaky campaigns, CI outcomes, PR review coverage, fuzzer anomalies, AI reliability, and state health

## Setup

Model selection is configured in YAML, not in secrets:

- `examples/config.yml` controls the CI failure agent model through `bedrock.model_id`
- `examples/pr-review-config.yml` controls the PR reviewer model through `reviewer.models.*`
- both configs also support optional `retrieval` settings for explicit Bedrock Knowledge Base lookup

AWS authentication is wired for GitHub Actions OIDC by default:

- GitHub Actions secret: `CI_BOT_AWS_ROLE_ARN`
- GitHub Actions variable: `CI_BOT_AWS_REGION`

Local development:

- copy `.env.example` to `.env.local`
- fill in your own `GITHUB_TOKEN`, `AWS_REGION`, and `AWS_PROFILE`
- when targeting repositories that require DCO, also set `CI_BOT_COMMIT_NAME`, `CI_BOT_COMMIT_EMAIL`, and `CI_BOT_REQUIRE_DCO_SIGNOFF=true`
- source `.env.local` manually before running scripts

## Capability Dashboard

Workflow at `.github/workflows/agent-dashboard.yml`.

The dashboard generator turns the bot's durable state and monitor artifacts
into:

- `agent-dashboard.html` — the single-page executive dashboard artifact
- `agent-dashboard.md` — a GitHub-summary-friendly report
- `agent-dashboard.json` — the structured payload for automation
- `dashboard-site/` — a multi-page operator dashboard with focused workflow views

The site is intentionally static: no database to operate, no app server to
deploy, and no extra runtime surface to secure. It covers:

- `Overview` for trend watch, event stream, and explicit data-coverage checks
- `Daily CI` for the red failure heatmap, recent runs, and active remediation campaigns
- `PRs` for tracked review state, replay acceptance cases, and workflow contract checks
- `Fuzzer` for anomalies, seeds, issue actions, and root-cause categories
- `Ops` for incident queue, event ledger, monitor watermarks, AI reliability counters, and loader warnings

The standalone workflow runs on a schedule and manual dispatch, checks out the
`bot-data` branch snapshots when present, refreshes the Daily health report,
runs the replay acceptance scorecard, writes the Markdown summary into the
workflow summary, and uploads the full site plus supporting JSON artifacts.
The Daily, Fuzzer, and Replay Lab workflows also generate the site artifact for
their own richer workflow-specific payloads.

Open `dashboard-site/index.html` from the workflow artifact for the prettiest
experience. The single-page HTML stays useful for quick inspection, the
Markdown file stays optimized for GitHub step summaries, and the JSON file
stays optimized for automation.

The GitHub Pages publisher at `.github/workflows/publish-dashboard-site.yml`
now republishes the site automatically after successful `main`-branch runs of
CI, dashboard refreshes, replay lab, Daily monitoring, Fuzzer monitoring,
review workflows, and backport automation. It also keeps a 6-hour scheduled
refresh and still supports manual dispatch if you want to force a republish.

Local usage:

```bash
python3 -m scripts.agent_dashboard \
  --failure-store bot-data/failure-store.json \
  --rate-state bot-data/rate-state.json \
  --monitor-state bot-data/monitor-state.json \
  --review-state bot-data/review-state.json \
  --event-log bot-data/agent-events.jsonl \
  --acceptance-result acceptance-report.json \
  --daily-health daily-health-report.json \
  --daily-result monitor-result.json \
  --fuzzer-result fuzzer-monitor-result.json \
  --output-markdown agent-dashboard.md \
  --output-json agent-dashboard.json \
  --output-html agent-dashboard.html

python3 -m scripts.agent_dashboard_site \
  --dashboard-json agent-dashboard.json \
  --site-dir dashboard-site
```

To keep the Daily/Weekly heatmap durable across GitHub retention gaps, you can
also backfill and reuse stored run snapshots from `bot-data`:

```bash
python3 -m scripts.backfill_daily_health_history \
  --repo "valkey-io/valkey" \
  --workflow "daily.yml" "weekly.yml" \
  --branch "unstable" \
  --days 14 \
  --token "$GITHUB_TOKEN" \
  --state-token "$GITHUB_TOKEN" \
  --state-repo "owner/repo" \
  --mirror-dir "bot-data/dashboard-history/daily-health"

python3 -m scripts.daily_health_report \
  --repo "valkey-io/valkey" \
  --workflow "daily.yml" "weekly.yml" \
  --branch "unstable" \
  --days 14 \
  --token "$GITHUB_TOKEN" \
  --history-dir "bot-data/dashboard-history/daily-health" \
  --output daily-health-report.html \
  --output-json daily-health-report.json
```

## Demo Bundle

Workflow at `.github/workflows/demo-valkey-agent.yml`.

This is the "one click, no tab spelunking" demo path for the repo. It
dispatches the real child workflows you actually want to show, waits for them,
and produces a polished packet with the important links already stitched
together:

- the public GitHub Pages control room
- a fresh replay lab run
- a fresh dashboard refresh
- optional Daily and Fuzzer monitor probes
- an optional live external PR review on a fork
- the latest proofed Daily-fix example pulled from `bot-data` when available

Outputs:

- `demo-report.md` — a short presenter-friendly walkthrough
- `demo-report.json` — structured link and status payload
- `demo-report.html` — a polished single-page demo packet
- `demo-site/index.html` — the same packet packaged as a shareable mini site

The workflow is safe by default: Daily and Fuzzer runs stay in dry-run mode,
and live PR review is skipped unless you pass both `review_target_repo` and
`review_pr_number`.

Typical usage:

1. Run `Demo Valkey CI Agent`
2. Leave the defaults on for dashboard, replay, Daily dry-run, and Fuzzer dry-run
3. Optionally add a fork PR for `review_target_repo` plus `review_pr_number`
4. Open the uploaded `valkey-ci-demo-bundle-*` artifact and start with `demo-site/index.html`

## Valkey-Native Context

For Valkey repositories, the agent now loads live upstream context instead of
depending only on static local prompts.

At runtime it fetches:

- repository-wide `.github/copilot-instructions.md`
- path-targeted `.github/instructions/*.md`
- the current `.github/workflows/*.yml` inventory
- important maintainer labels such as `run-extra-tests`, `needs-doc-pr`, and `pending-missing-dco`

That context is injected into:

- PR review prompts, including release-branch guidance and label-aware policy notes
- failure analysis and fix generation, including workflow-sensitive remediation hints
- runtime validation defaults, so new Valkey jobs can inherit CI-exact build and test commands directly from live workflow YAML

This keeps the bot aligned with current Valkey maintainer practice as the
upstream repository evolves.

## Valkey Acceptance Harness

The checked-in `examples/valkey-acceptance.yml` manifest is generated from live
Valkey state instead of being a static placeholder. It includes:

- recent PRs chosen to exercise DCO, docs, `@core-team`, `run-extra-tests`, and clean-review paths
- latest failed runs from key Valkey workflows such as `Daily`, `CI`, `External`, and `Weekly` when available
- a recent merged PR plus the newest release branch for backport replay
- workflow contract checks for this repo's own automation surface

Refresh it with:

```bash
python3 -m scripts.build_valkey_acceptance_manifest \
  --token "$GITHUB_TOKEN" \
  --output examples/valkey-acceptance.yml
```

## CI Failure Agent

The core feature. Reusable workflow at `.github/workflows/analyze-failure.yml`.

When a CI workflow fails in `valkey-io/valkey`, the agent:

1. detects and classifies the failure (build error, test failure, flaky test)
2. retrieves and parses logs using format-specific parsers (gtest, tcl, build errors, sentinel/cluster)
3. analyzes root cause using Amazon Bedrock
4. generates a candidate fix
5. validates the fix with a configured CI-exact validation profile
6. queues the validated fix for human approval before opening a PR

For flaky failures, the agent now switches to a campaign mode: it stores prior
failed ideas in the failure store, repeats validation multiple times before
trusting a fix, and reuses that backlog on later attempts so it does not keep
trying the same weak patch.

Validation fails closed when no validation profile matches. Set
`validation.require_profile: false` only when you intentionally want the legacy
build-only fallback; otherwise unmatched jobs are routed away from automatic
PR creation rather than being treated as CI-validated.

Required GitHub configuration:

- caller workflow permission: `id-token: write`
- secret: `AWS_ROLE_ARN`
- either secret: `VALKEY_GITHUB_TOKEN`
- or variable: `VALKEY_GITHUB_APP_ID` plus secret: `VALKEY_GITHUB_APP_PRIVATE_KEY`
- optional variable: `CI_BOT_COMMIT_NAME`
- optional variable: `CI_BOT_COMMIT_EMAIL`
- optional variable: `CI_BOT_REQUIRE_DCO_SIGNOFF` (set to `true` for repositories such as Valkey that require DCO-signed commits)

## PR Review Agent

Reusable workflow at `.github/workflows/review-pr.yml`.

Reviews pull requests through the GitHub API without checking out PR head code in the privileged workflow. The reviewer uses direct Bedrock runtime calls, and can optionally inject explicit Bedrock KB retrieval into prompts. It can:

- post or update a PR summary comment
- generate optional release notes
- publish focused review comments
- answer follow-up `/reviewbot` questions in PR comments and review threads

The reviewer is intentionally defect-oriented and conservative. It prefers a
small number of high-confidence findings, generates structured candidate
findings, runs a skeptic verification pass before publishing, ranks surviving
findings by severity and confidence, and submits them as a single batched
review with a short top-level summary. By default, a no-findings pass posts a
neutral review note instead of approving the PR; set
`reviewer.approve_on_no_findings: true` only after the bot is passing your
review eval set.

Maintainer-policy reminders are kept separate from inline defect findings. The
summary comment can include a deterministic checklist for DCO, docs follow-up,
security-sensitive PRs, governance changes, and likely `@core-team` review
routing. This is controlled by `reviewer.post_policy_notes`.

When deeper context is needed, the reviewer can fetch changed files at the PR
head SHA, inspect the full pre-change file at the base SHA, search code at the
pinned revision, and locate likely related tests. Test discovery uses the
project metadata in the reviewer config, including optional
`project.test_to_source_patterns` mappings.

Detailed review skips only deterministic trivial file changes by default. Set
`reviewer.model_file_triage: true` if you want the light model to skip files
before the heavy review pass.

Example consumer-repo files:

- `examples/pr-review-caller-workflow.yml`
- `examples/pr-review-config.yml`

For fork or cross-repo testing, `.github/workflows/review-external-pr.yml` lets you dispatch a one-off review against any `owner/repo#PR` reachable by your GitHub token or GitHub App installation. It does not require adding workflow files or config files to the target repository. The reviewer posts comments on the target PR, but its incremental state is stored on this agent repo's `bot-data` branch.

Config loading checks the target repo first (`.github/pr-review-bot.yml`), then falls back to this agent repo's checked-in config.

Useful reviewer config fields:

- `reviewer.custom_instructions` for project-specific invariants and review guidance
- `reviewer.approve_on_no_findings` to opt into automated approvals after no findings
- `reviewer.chat_collaborator_only` to restrict `/reviewbot` follow-up chat to collaborator-equivalent actors
- `reviewer.model_file_triage` to opt into light-model file skipping before heavy review
- `reviewer.post_policy_notes` to include deterministic maintainer-policy notes in the summary
- `reviewer.project.source_dirs` and `reviewer.project.test_dirs` for source/test discovery
- `reviewer.project.test_to_source_patterns` to map changed source files to likely regression tests
- `reviewer.retrieval.*` to enable optional Bedrock Knowledge Base context

Required GitHub configuration:

- caller workflow permission: `id-token: write`
- secret: `AWS_ROLE_ARN`
- either secret: `VALKEY_GITHUB_TOKEN`
- or variable: `VALKEY_GITHUB_APP_ID` plus secret: `VALKEY_GITHUB_APP_PRIVATE_KEY`

### Specialist Review Mode

When `specialist_mode: true` is set in the reviewer config, the PR Review Agent runs 9 specialist reviewers in parallel alongside the standard review pass. Each specialist focuses on one concern (test coverage, security, performance, style, etc.) and makes a single Bedrock call. Findings are deduplicated, ranked by severity, and synthesized into a verdict:

- **Ready to Merge** — no critical or high-severity findings
- **Needs Attention** — medium-severity findings only
- **Needs Work** — critical or high-severity findings present

## Backport Agent

Reusable workflow at `.github/workflows/backport.yml`.

When a maintainer adds a `backport <branch>` label to a merged PR in `valkey-io/valkey`, a caller workflow in that repo triggers this agent. The agent cherry-picks the merged PR's commits onto the target release branch and opens a backport PR. If the cherry-pick produces merge conflicts, the agent uses Amazon Bedrock to resolve them, applying the original PR's intent to the diverged codebase.

The pipeline:

1. validates the target branch exists, the source PR is merged, and no duplicate backport PR is open
2. checks the daily rate limit (default 10 PRs per 24 hours)
3. clones the repo, cherry-picks onto a `backport/<pr>-to-<branch>` branch
4. if conflicts arise, resolves them file-by-file using Bedrock (whitespace-only conflicts are resolved without LLM calls)
5. pushes the branch and opens a backport PR with labels, conflict details, and per-file resolution summaries
6. posts a summary comment on the source PR

The agent applies a `backport` label to every backport PR, and an `llm-resolved-conflicts` label when any file was resolved by the LLM, signaling that extra review attention is needed.

Generated backport PR bodies are verdict-first. They include a short backport
summary, a compact facts table, a reviewer checklist, the cherry-picked commit
list, conflict details, and a human-review warning when any file was
LLM-resolved.

Configuration is loaded from `.github/backport-agent.yml` in the consumer repo. When the file is missing, sensible defaults are used. Configurable settings include the Bedrock model ID, max conflict retries, max conflicting files, daily PR limit, per-backport token budget, and labels applied to generated backport PRs.

The example caller maps consumer-repo credentials into the reusable workflow explicitly. Pin the reusable workflow reference to a trusted release tag or full commit SHA in production instead of tracking a moving branch.

Example consumer-repo files:

- `examples/backport-caller-workflow.yml` — caller workflow triggered on `pull_request_target` `labeled` events
- `examples/backport-config.yml` — configuration with all available settings and defaults

Required GitHub configuration in the consumer repo:

- caller workflow permission: `id-token: write`
- secret: `CI_BOT_AWS_ROLE_ARN` — IAM role assumable via GitHub OIDC for Bedrock access
- variable: `CI_BOT_AWS_REGION` (optional, defaults to `us-east-1`)
- optional variable: `CI_BOT_COMMIT_NAME`
- optional variable: `CI_BOT_COMMIT_EMAIL`
- optional variable: `CI_BOT_REQUIRE_DCO_SIGNOFF`

## Central Valkey Monitor

Workflow at `.github/workflows/monitor-valkey-daily.yml`.

Runs from this repo, watches the live Valkey CI surface in `valkey-io/valkey`, analyzes new failures, records per-job pass/fail history, validates candidate fixes, and reconciles any queue-worthy fixes into draft PRs in `valkey-io/valkey` or the configured `VALKEY_FORK_REPO` using the local config at `.github/valkey-daily-bot.yml`. When a draft fix survives proof, the proof job now opens or updates the real upstream PR in `valkey-io/valkey` automatically.

The flow is intentionally simple:

- monitor new `CI`, `Daily`, `External`, and `Weekly` runs
- queue only the fixes that validate and pass the normal safety heuristics
- verify the target branches exist in the configured PR repository
- open draft PRs automatically in the configured PR repository
- proof successful draft fixes and hand them off upstream automatically

Runner-specific duplicates are collapsed at a canonical incident level, so the same underlying test failure across multiple runners produces one queued fix / one draft PR while still preserving the per-runner observations in bot state.

The monitor now understands multiple workflow events per surface, so the same workflow file can be scanned across `pull_request`, `push`, and `schedule` traffic without needing separate wrappers or separate persisted watermarks.

The validated-fix queue now lives in `FailureStore`, so reconciliation, preflight checks, and dashboarding all read the same authoritative queue state. Rate limiting still lives in `RateLimiter`, but it no longer owns queue membership.

Approval context is written into the workflow summary so you can review the root-cause rationale, files the agent wants to change, observed failure streak, and last known good / first bad commits when history exists.

Manual dispatch defaults to `dry_run=true` so you can inspect candidate runs without advancing state or queueing fixes. Dispatch with `dry_run=false` for a real automated pass; any queued fixes will be reconciled automatically into draft PRs in the configured PR repository. You can also narrow a manual run to one workflow surface with the `workflow_scope` input.

Required GitHub configuration:

- secret: `AWS_ROLE_ARN`
- either secret: `VALKEY_GITHUB_TOKEN`
- or variable: `VALKEY_GITHUB_APP_ID` plus secret: `VALKEY_GITHUB_APP_PRIVATE_KEY`
- optional variable: `VALKEY_FORK_REPO` (defaults to `valkey-io/valkey`; set it to a fork if you want branches opened there)
- secret: `VALKEY_FORK_GITHUB_TOKEN` (or reuse `VALKEY_GITHUB_TOKEN` if it also has write access to the fork)
- optional variable: `CI_BOT_COMMIT_NAME`
- optional variable: `CI_BOT_COMMIT_EMAIL`
- optional variable: `CI_BOT_REQUIRE_DCO_SIGNOFF`

## Acceptance Harness

Use `python -m scripts.valkey_acceptance` with
`examples/valkey-acceptance.yml` to evaluate whether the current bot behavior
matches Valkey's acceptance bar before enabling it on `valkey-io/valkey`.

The harness:

- runs deterministic policy checks such as DCO, docs follow-up, security handling, and `@core-team` escalation
- can execute report-only PR summary and review passes against real Valkey PRs
- emits a readiness scorecard for review cases plus CI/backport replay coverage
- renders exact replay commands for CI failure and backport cases to run against a fork

Manual replay workflow: `.github/workflows/agent-replay-lab.yml`.

See `docs/valkey-acceptance.md` for usage.

## Central Valkey Fuzzer Monitor

Workflow at `.github/workflows/monitor-valkey-fuzzer.yml`.

Runs from this repo, watches new scheduled `fuzzer-run.yml` executions in `valkey-io/valkey-fuzzer`, downloads the structured artifact bundle from each run when available, falls back to job logs when it is not, and writes a per-run anomaly summary using the local config at `.github/valkey-fuzzer-bot.yml`.

The fuzzer monitor is analysis-only:

- it does not open pull requests
- it distinguishes expected chaos behavior from anomalous behavior
- it automatically creates or updates a GitHub issue in `valkey-io/valkey-fuzzer` when a run is classified as `anomalous`
- it writes the analysis to the workflow summary
- it uploads the raw `fuzzer-monitor-result.json` payload as a workflow artifact

Generated anomaly issues are also verdict-first. Each issue includes a concise
status line, metadata table, action-needed section, reproduction command when
available, concrete findings grouped by severity, and collapsible normal
signals for lower-noise triage.

Required GitHub configuration:

- secret: `AWS_ROLE_ARN`
- either secret: `VALKEY_GITHUB_TOKEN`
- or variable: `VALKEY_GITHUB_APP_ID` plus secret: `VALKEY_GITHUB_APP_PRIVATE_KEY`

## Retrieval KB Refresh

Workflow at `.github/workflows/refresh-bedrock-kb.yml`.

Refreshes the existing Valkey code and docs knowledge bases used by retrieval:

- code KB: `OHQMPN9RCG`
- docs KB: `NAKLE24DH9`

The workflow is OIDC-only and uses:

- GitHub secret: `AWS_ROLE_ARN`
- GitHub variable: `AWS_REGION`

Manual runs default to `dry_run=true` so you can verify corpus prep and data-source discovery before mutating Bedrock.

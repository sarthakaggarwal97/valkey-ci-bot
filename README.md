# valkey-ci-agent

An AI agent for Valkey CI failure remediation, PR review, and automated backports.

## Features

- **CI Failure Agent** — analyzes workflow failures, generates and validates fixes, opens PRs with approval gating
- **Flaky Failure Campaigns** — persists experiment history for flaky failures, repeats validation runs, and feeds failed hypotheses back into later attempts
- **PR Review Agent** — reviews pull requests via the GitHub API, posts summaries, publishes review comments, answers follow-up questions
- **Backport Agent** — cherry-picks merged PRs onto release branches with LLM-based conflict resolution
- **Fuzzer Monitor** — watches fuzzer runs, detects anomalies, creates GitHub issues
- **Central Valkey Monitor** — watches scheduled CI runs, tracks failure history, queues validated fixes

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

## CI Failure Agent

The core feature. Reusable workflow at `.github/workflows/analyze-failure.yml`.

When a CI workflow fails in `valkey-io/valkey`, the agent:

1. detects and classifies the failure (build error, test failure, flaky test)
2. retrieves and parses logs using format-specific parsers (gtest, tcl, build errors, sentinel/cluster)
3. analyzes root cause using Amazon Bedrock
4. generates a candidate fix
5. validates the fix by rebuilding and re-running the failing job locally
6. queues the validated fix for human approval before opening a PR

For flaky failures, the agent now switches to a campaign mode: it stores prior
failed ideas in the failure store, repeats validation multiple times before
trusting a fix, and reuses that backlog on later attempts so it does not keep
trying the same weak patch.

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

The example caller maps consumer-repo credentials into the reusable workflow explicitly. Pin the reusable workflow reference to a trusted release tag or full commit SHA in production instead of tracking a moving branch, and set the `agent_ref` input to the same trusted ref so the checked-out agent code matches the workflow you invoked.

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

Runs from this repo, watches new scheduled `Daily` runs in `valkey-io/valkey`, analyzes new failures, records per-job pass/fail history, validates candidate fixes, and then, in the same workflow job, reconciles any queue-worthy fixes into draft PRs in `sarthakaggarwal97/valkey` using the local config at `.github/valkey-daily-bot.yml`.

The flow is intentionally simple:

- monitor new `Daily` runs
- queue only the fixes that validate and pass the normal safety heuristics
- verify the target branches exist in your fork
- open draft PRs automatically in your fork

Runner-specific duplicates are collapsed at a canonical incident level, so the same underlying test failure across multiple runners produces one queued fix / one draft PR while still preserving the per-runner observations in bot state.

The internal queue is still used for persistence and rate limiting, but there is no separate approval job or manual gate in the workflow anymore.

Approval context is written into the workflow summary so you can review the root-cause rationale, files the agent wants to change, observed failure streak, and last known good / first bad commits when history exists.

Manual dispatch defaults to `dry_run=true` so you can inspect candidate runs without advancing state or queueing fixes. Dispatch with `dry_run=false` for a real automated pass; any queued fixes will be reconciled automatically into draft PRs in your fork.

Required GitHub configuration:

- secret: `AWS_ROLE_ARN`
- either secret: `VALKEY_GITHUB_TOKEN`
- or variable: `VALKEY_GITHUB_APP_ID` plus secret: `VALKEY_GITHUB_APP_PRIVATE_KEY`
- optional variable: `VALKEY_FORK_REPO` (defaults to `sarthakaggarwal97/valkey`)
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
- renders exact replay commands for CI failure and backport cases to run against a fork

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

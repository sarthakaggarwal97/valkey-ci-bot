# valkey-ci-bot
An AI bot for Valkey CI failure remediation and PR review

## Setup

Model selection is configured in YAML, not in secrets:

- `examples/config.yml` controls the CI failure bot model through `bedrock.model_id`
- `examples/pr-review-config.yml` controls the PR reviewer model through `reviewer.models.*`
- both configs also support optional `retrieval` settings for explicit Bedrock Knowledge Base lookup

AWS authentication is wired for GitHub Actions OIDC by default:

- GitHub Actions secret: `CI_BOT_AWS_ROLE_ARN`
- GitHub Actions variable: `CI_BOT_AWS_REGION`

Local development:

- copy [`.env.example`](/Users/sarthagg/IdeaProjects/valkey-ci-bot/.env.example) to `.env.local`
- fill in your own `GITHUB_TOKEN`, `AWS_REGION`, and `AWS_PROFILE`
- source `.env.local` manually before running scripts

## Retrieval KB Refresh

The retrieval-backed setup also includes [`.github/workflows/refresh-bedrock-kb.yml`](/Users/sarthagg/IdeaProjects/valkey-ci-bot/.github/workflows/refresh-bedrock-kb.yml).

It refreshes the existing Valkey code and docs knowledge bases used by retrieval:

- code KB: `OHQMPN9RCG`
- docs KB: `NAKLE24DH9`

The workflow is OIDC-only and uses:

- GitHub secret: `AWS_ROLE_ARN`
- GitHub variable: `AWS_REGION`

Manual runs default to `dry_run=true` so you can verify corpus prep and data-source discovery before mutating Bedrock.

## Central Valkey Monitor

This repo also includes a centralized monitor workflow at [`.github/workflows/monitor-valkey-daily.yml`](/Users/sarthagg/IdeaProjects/valkey-ci-bot/.github/workflows/monitor-valkey-daily.yml).

It runs from this repo, watches new scheduled `Daily` runs in `valkey-io/valkey`, analyzes new failures, records per-job pass/fail history, validates candidate fixes, and queues approved-ready patches using the local config at [`.github/valkey-daily-bot.yml`](/Users/sarthagg/IdeaProjects/valkey-ci-bot/.github/valkey-daily-bot.yml).

Required GitHub configuration for this repo:

- secret: `AWS_ROLE_ARN`
- either secret: `VALKEY_GITHUB_TOKEN`
- or variable: `VALKEY_GITHUB_APP_ID` plus secret: `VALKEY_GITHUB_APP_PRIVATE_KEY`

The workflow has two stages:

- `monitor`: runs automatically on schedule, analyzes failures, applies history-based queue gating, and queues validated fixes in this bot repo
- `create-approved-prs`: runs only after approval on the protected environment `valkey-pr-approval`

No PR is opened against `valkey-io/valkey` until that environment approval is granted.

Approval context is written into the workflow summary so you can review:

- the root-cause rationale
- files the bot wants to change
- observed failure streak
- last known good commit and first bad commit when history exists

Manual dispatch still defaults to `dry_run=true` so you can inspect candidate runs without advancing state or queueing fixes. When you want a real automated pass, dispatch with `dry_run=false`; the workflow will still stop at the approval gate before opening any PRs.

## PR review bot

The repository also includes a reusable PR reviewer workflow at `.github/workflows/review-pr.yml`.

It reviews pull requests through the GitHub API without checking out PR head code in the privileged workflow. The reviewer uses direct Bedrock runtime calls, and can optionally inject explicit Bedrock KB retrieval into prompts. The reviewer can:

- post or update a PR summary comment
- generate optional release notes
- publish focused review comments
- answer follow-up `/reviewbot` questions in PR comments and review threads

Example consumer-repo files:

- `examples/pr-review-caller-workflow.yml`
- `examples/pr-review-config.yml`

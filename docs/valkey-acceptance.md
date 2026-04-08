# Valkey Acceptance Harness

This harness is meant to answer one question before rollout:

Can this repo behave in a way that is acceptable to Valkey maintainers?

It does that in two layers:

1. Deterministic policy checks
- DCO trailers present or missing
- `@core-team` escalation needed or not
- docs follow-up likely needed or not
- security-sensitive handling likely needed or not

2. Report-only model execution
- PR summary generation
- PR review findings
- review coverage accounting

The harness does not post comments or open PRs. It is safe to run against
real Valkey pull requests as long as you provide read-capable GitHub access
and Bedrock credentials only when you want to execute the model passes.

## Manifest

Start from `examples/valkey-acceptance.yml`.

Use:
- `target_repo` for the upstream repo you want to inspect
- `execution_repo` for your fork when replaying CI or backport flows
- `review_cases` for automated policy and report-only PR checks
- `ci_cases` for exact `scripts.main` replay commands
- `backport_cases` for exact `scripts.backport_main` replay commands

## Usage

Policy-only report:

```bash
python -m scripts.valkey_acceptance \
  --manifest examples/valkey-acceptance.yml \
  --token "$GITHUB_TOKEN"
```

Policy plus model execution:

```bash
python -m scripts.valkey_acceptance \
  --manifest examples/valkey-acceptance.yml \
  --token "$GITHUB_TOKEN" \
  --aws-region us-east-1 \
  --run-models \
  --output acceptance-report.md \
  --json-output acceptance-report.json
```

## DCO setup

For repositories that require DCO-signed commits, set:

```bash
export CI_BOT_COMMIT_NAME="Your Known Identity"
export CI_BOT_COMMIT_EMAIL="you@example.com"
export CI_BOT_REQUIRE_DCO_SIGNOFF=true
```

The acceptance report will include the exact replay commands for CI-failure
and backport cases, using those environment variables.

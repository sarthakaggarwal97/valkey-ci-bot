# valkey-ci-bot
An AI bot for Valkey CI failure remediation and PR review

## Setup

Model selection is configured in YAML, not in secrets:

- `examples/config.yml` controls the CI failure bot model through `bedrock.model_id`
- `examples/pr-review-config.yml` controls the PR reviewer Bedrock Agent through `reviewer.agent.*`, with `reviewer.models.*` as the direct-runtime fallback

AWS authentication is wired for GitHub Actions OIDC by default:

- GitHub Actions secret: `CI_BOT_AWS_ROLE_ARN`
- GitHub Actions variable: `CI_BOT_AWS_REGION`

Static access-key fallback is still supported if you need it:

- `CI_BOT_AWS_ACCESS_KEY_ID`
- `CI_BOT_AWS_SECRET_ACCESS_KEY`
- `CI_BOT_AWS_SESSION_TOKEN` when using session credentials

Local development:

- copy [`.env.example`](/Users/sarthagg/IdeaProjects/valkey-ci-bot/.env.example) to `.env.local`
- fill in your own `GITHUB_TOKEN`, `AWS_REGION`, and `AWS_PROFILE`
- source `.env.local` manually before running scripts

## PR review bot

The repository also includes a reusable PR reviewer workflow at `.github/workflows/review-pr.yml`.

It reviews pull requests through the GitHub API without checking out PR head code in the privileged workflow. The reviewer can:

- post or update a PR summary comment
- generate optional release notes
- publish focused review comments
- answer follow-up `/reviewbot` questions in PR comments and review threads

The reviewer can run either through direct `bedrock-runtime` model calls or through a pre-configured Bedrock Agent alias with attached knowledge bases and prompt overrides.

Example consumer-repo files:

- `examples/pr-review-caller-workflow.yml`
- `examples/pr-review-config.yml`

## Bedrock KB refresh

The repository includes a scheduled GitHub Actions workflow at `.github/workflows/refresh-bedrock-kb.yml`.

It runs a full Bedrock knowledge-base refresh every 6 hours and can also be started manually with `workflow_dispatch`.

Required repository secrets:

- `AWS_ROLE_ARN` for OIDC role assumption, or:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_SESSION_TOKEN` if your AWS credentials are session-based

Recommended repository variable:

- `AWS_REGION`

What the workflow does:

- refreshes the Valkey code KB from the current `unstable` branch using Bedrock custom-document ingestion
- deletes stale code documents that disappeared from the source branch
- refreshes the Valkey documentation KB through the dedicated web crawler data source

Manual run options:

- `missing_only=true` resumes a partial code refresh without resending already-present document IDs. This is for recovery, not normal freshness, because a full refresh is what updates changed file contents.
- `skip_web_sync=true` refreshes code only.
- `verbose=true` enables debug logging.

Local command:

```bash
python scripts/bedrock_kb_refresh.py --region us-east-1
```

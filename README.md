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

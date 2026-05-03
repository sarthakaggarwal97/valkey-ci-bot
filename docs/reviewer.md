# PR Reviewer

A deep-dive on the PR review pipeline for maintainers evaluating integration with `valkey-io/valkey`. This document is scoped to the reviewer — see [architecture.md](architecture.md) for broader system design.

## Overview

The reviewer reads a pull request, inspects the source tree with read-only tools, posts inline review comments plus a summary comment, and persists incremental state on the `bot-data` branch so subsequent pushes only re-examine new commits.

| | |
|---|---|
| Entry (external) | `.github/workflows/review-external-pr.yml` — `workflow_dispatch` with `target_repo` and `pr_number` inputs |
| Entry (reusable) | `.github/workflows/review-pr.yml` — `workflow_call`, used by the dispatch workflow and embeddable in host repos |
| Entry (code) | `scripts/pr_review_main.py` |
| Model | `us.anthropic.claude-opus-4-7` via Amazon Bedrock |
| Tools available to the model | `Read`, `Grep`, `Glob` (no `Bash`, no network) |
| State branch | `bot-data` — `review_state_store.py` |

## Two-stage architecture

The review is always two sequential LLM calls against the same repo checkout.

### Stage 1 — Deep review

`scripts/claude_reviewer.py:review_pr` invokes `run_agent("review_readonly", ...)` from `scripts/agent_runtime.py`. The `review_readonly` profile pins `allowed_tools=Read,Grep,Glob` and `max_turns=240`.

Prompt structure:

1. Role ("senior Valkey maintainer").
2. PR metadata (title, body, author, base/head SHAs).
3. Changed files + unified diff.
4. **Valkey divergence block** (see below) — loaded lazily per subsystem.
5. What-to-flag rubric + severity definitions.
6. Output schema: a single JSON array of finding objects.

Stage 1 produces *candidate* findings.

### Stage 2 — Skeptic pass

`claude_reviewer.py:_skeptic_pass` runs a second `run_agent("review_readonly", ...)` call against the same checkout so it can independently verify claims in the Stage 1 JSON. The skeptic receives the candidate findings plus the changed files and applies a drop rubric:

- Speculative or "could this also be a problem?"-style
- Duplicate of another finding
- Pure style / nit
- Not supported by the actual code referenced
- Relies on the *absence* of behavior ("I don't see a check for X")
- A maintainer would close as out-of-scope

The skeptic may also adjust severity. It **fails open**: if the skeptic call errors, returns empty output, or produces unparseable JSON, all Stage 1 findings are kept (`logger.warning(...); return findings` in three separate branches).

The design is adapted from the specialist-reviewer idea in [PR #8](https://github.com/sarthakaggarwal97/valkey-ci-agent/pull/8), collapsed to a single Stage 1 plus skeptic to cut latency.

## Robustness layers

The LLM often emits JSON with fenced code blocks, trailing commas, `...` elisions, or stray prose. The reviewer is built to survive that.

### JSON parse pipeline (`_parse_findings_json_strict`)

| Pass | What it does |
|---|---|
| 1 | `json.loads` on the raw text |
| 2 | `_repair_json` — strips ``` fences, `//` and `/* */` comments, `...` elisions, trailing commas; retries `json.loads` |
| 3 | Regex-extracts the first `[...]` span, runs `_repair_json`, retries |
| 4 | Brace-depth object-by-object scan; each parsable `{...}` is collected |

### Retries wrapping the parse pipeline

- **Empty-result retry** — if Claude emits no `result` event, retry once with a direct "return the JSON array" prompt.
- **Prose-to-JSON retry** — if parsing still fails, retry once with "return ONLY the JSON array, no prose".
- **max_turns graceful salvage** — if Claude hits `max_turns=240` and exits non-zero, `claude_reviewer.py` scans the assistant transcript for the largest JSON-array-shaped substring. On failure, it returns `[]` rather than raising, so a long-running review never hard-fails the workflow.

## Valkey knowledge (`scripts/valkey_knowledge.py`)

The prompt injects Valkey-specific context so the model does not review as if it were Redis.

**Divergence block** — always included:

- Renamed symbols: `redisCommand` → `serverCommand`, `RedisModule_*` → `ValkeyModule_*`
- Renamed configs: `slaveof` → `replicaof` (and peers)
- Structural changes: hashtable bucket chaining, 128-byte embedded-value budget, Ignition/Cooldown I/O threading model
- Deprecated configs

**Per-subsystem context** — loaded lazily based on changed paths:

- `src/cluster.c` → cluster bus / slot migration notes
- `src/io_threads.c`, `src/networking.c` → threading model notes
- `src/replication.c` → PSYNC / replication-stream caveats
- …and so on

This keeps the prompt budget spent on context the PR actually touches.

## Style guards

The prompt bans forensic patterns that make review comments read as machine-generated:

- No `wc -l`, `git cat-file`, byte counts, diff line counts
- No "the diff shows…", "I ran…", "according to the output…"
- Comments should read in a human-maintainer voice, with "Good" / "Bad" examples shown in-prompt
- One-line fixes are formatted as GitHub `suggestion` blocks

On 30 shadow PRs the style score was **1.00** (zero forensic-pattern leakage) — see [eval/RESULTS.md](../eval/RESULTS.md).

## Output validation (`scripts/review_diff.py`)

Every finding is re-validated after Stage 2, *before* it reaches GitHub:

| Check | Behavior |
|---|---|
| Line-in-diff | The finding must target a file + line that exists in the PR's unified diff. Hallucinated paths or out-of-range lines are dropped. |
| Methodology leak | Pattern match against `_METHODOLOGY_PATTERNS` ("I ran", "the diff shows", byte counts). Match → drop. |
| Secret-like content | Pattern match against `_SECRET_PATTERNS` (`gh[pousr]_` tokens, AWS access keys). Match → drop. |
| Severity-without-rationale | `severity ∈ {high, critical}` and body < 20 characters → drop. |

The file-diff map parser explicitly skips `\ No newline at end of file` markers — a regression flagged during v3 evaluation where those markers were being counted as line positions and shifting every following finding off by one.

## Cost and latency

| | |
|---|---|
| LLM calls per review | 2 (Stage 1 + skeptic) |
| Typical cost | USD 3 – 8 |
| Typical wall time | 5 – 25 min |
| Hard timeout | `timeout-minutes: 120` on the workflow job |
| Hard turn bound | `max_turns: 240` per call |
| Evidence artifact | `agent-evidence/` uploaded to Actions, 30-day retention |

Cost varies with PR size and how often Claude needs to `Read` supporting files outside the diff.

## State

`scripts/review_state_store.py` persists two keys per PR on the `bot-data` branch:

- `last_reviewed_head_sha` — the commit SHA the reviewer last completed against
- `review_completed_for_head` — sentinel to avoid re-posting if the same SHA is re-triggered

On the next push, if `last_reviewed_head_sha` is set, the prompt asks Claude to focus on changes since that SHA. Note: with `Read,Grep,Glob` only, the model cannot run `git log` itself — it relies on the diff block the pipeline constructs. Incremental mode therefore tightens the prompt's attention, it does not grant new tool access.

## Evaluation and quality

A 30-PR shadow evaluation against merged `valkey-io/valkey` PRs is checked in at [eval/RESULTS.md](../eval/RESULTS.md). Headline numbers:

| Metric | Value |
|---|---|
| PRs where the agent posted findings | 29 / 30 (97 %) |
| Strict F1 (path + line ±10) | 0.248 |
| Loose F1 (file match = 0.5 credit) | 0.387 |
| PRs with Loose F1 ≥ 0.3 | 19 / 30 (63 %) |
| Style score | 1.00 across all runs |

Methodology for the scorer is documented in `docs/eval.md`. The gap between Strict and Loose F1 reflects that the agent tends to find real issues in the *right file* but at a different line than the maintainer commented on — often a related symptom of the same bug.

## Integration plan (recommended)

**Phase 1 — shadow mode.** Run on merged PRs in a mirror repo. Do not post to upstream. Compare against maintainer review history using the eval harness.

**Phase 2 — opt-in label.** Add an `ai-review` label to `valkey-io/valkey`. A maintainer applying the label triggers a single review. Nothing posts unsolicited.

**Phase 3 — default-on for first-time contributors.** Auto-label `ai-review` when `github.event.pull_request.author_association == 'FIRST_TIME_CONTRIBUTOR'`.

Phase 2 integration is a single 15-line workflow in `valkey-io/valkey`:

```yaml
name: AI Review on Label
on:
  pull_request_target:
    types: [labeled]
permissions:
  pull-requests: write
  contents: read
jobs:
  dispatch:
    if: github.event.label.name == 'ai-review'
    runs-on: ubuntu-latest
    steps:
      - name: Dispatch CI agent review
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh workflow run review-external-pr.yml \
            --repo sarthakaggarwal97/valkey-ci-agent \
            --field target_repo=${{ github.repository }} \
            --field pr_number=${{ github.event.pull_request.number }}
```

No secrets from `valkey-io/valkey` cross into the agent repo — Bedrock credentials live entirely in the agent repo's OIDC role.

## Configuration

Reviewer-specific configuration lives in three places, in override order:

| Layer | File / variable | Purpose |
|---|---|---|
| Defaults | `scripts/config.py` — `ReviewerConfig` | Base values (`max_review_comments=5`, `daily_token_budget=0`, retrieval settings) |
| Repo config | `.github/pr-review-bot.yml` | Per-repo overrides, custom instructions, Bedrock KB IDs |
| Environment | `CI_AGENT_CLAUDE_MODEL`, `CI_AGENT_CLAUDE_BEDROCK_OPUS_MODEL` | Model ID overrides at workflow dispatch time |

Key knobs in `.github/pr-review-bot.yml`:

- `reviewer.max_review_comments` — cap on inline comments per review (default 5)
- `reviewer.daily_token_budget` — 0 = unlimited; enforced by `rate_limiter.py`
- `reviewer.retrieval.code_knowledge_base_id` / `docs_knowledge_base_id` — Bedrock KB IDs for retrieval-augmented context
- `reviewer.custom_instructions` — free-form text appended to the system prompt (used today to encode Valkey governance reminders and security-disclosure policy)

For a full field list see `ReviewerConfig` in `scripts/config.py`.

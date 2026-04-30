# AI Pipeline Operations Guide

This is the operator guide for the valkey-ci-agent's evidence-first AI pipeline.
It covers common workflows for maintainers dealing with output from the agent.

## Pipeline overview

Every CI failure (and PR review) goes through a staged flow:

```
CI failure
    │
    ▼
Stage 0: EvidenceBuilder
    │  builds EvidencePack (parsed failures, log excerpts, inspected files,
    │  recent commits, unknowns)
    ▼
Stage 1: RootCauseAnalyst — proposes up to N hypotheses
    ▼
Stage 2: RootCauseCritic
    │  deterministic pre-check (evidence refs must match the pack)
    │  → model critic if multiple pass
    │  → confidence gate (reject if < min_confidence_for_fix)
    ▼
Stage 3: FixTournament
    │  generate diverse candidates (minimal/root_cause_deep/defensive_guard)
    │  → validate concurrently (global semaphore)
    │  → rank (pass > smallest > minimal variant)
    ▼
Stage 4: RubricGate
    │  9 deterministic checks (patch size, timeout, test, security, DCO, …)
    │  → 1 model check (does_not_mask_failure) if deterministic passes
    ▼
Stage 5: PR Publisher
```

Any stage can reject the failure and route it to **`needs-human`**
with a structured `RejectionReason`:

| Reason | Meaning |
|---|---|
| `THIN_EVIDENCE` | Root cause hypotheses did not cite any real evidence from the pack |
| `LOW_CONFIDENCE_ROOT_CAUSE` | Accepted hypothesis was below the configured confidence floor |
| `CRITIC_REJECTED` | Model critic explicitly rejected all hypotheses |
| `TOURNAMENT_EMPTY` | No fix candidate validated — all generation or validation failed |
| `VALIDATION_FAILED` | (Reserved) Validation infra failed outside candidates |
| `RUBRIC_FAILED` | Rubric gate blocked the winning candidate; see `blocking_checks` |

## Common operator questions

### "I see a `needs-human` entry with `LOW_CONFIDENCE_ROOT_CAUSE` — what do I do?"

1. Look up the failure in the dashboard or via:
   ```
   python -m scripts.ai_eval.report --needs-human --store failure-store.json
   ```
2. Read the `EvidencePack` attached to the entry:
   - If `unknowns` is large (log truncated, no parser matched), the agent
     genuinely couldn't see enough. File an issue on the log collection
     tool, or manually inspect the run.
   - If `parsed_failures` is empty, the log parsers didn't recognize the
     format. Check whether a new parser is needed in `scripts/parsers/`.
3. If the failure is real and actionable, fix it manually and mark the
   store entry as handled. The agent will not retry `needs-human` entries.

### "A review finding was wrong — how do I mark it so the eval catches it next time?"

1. Identify the PR and the specific bad finding.
2. Add the finding to the gold fixture for that PR (or create a new one):
   ```json
   {
     "fixture_id": "pr-1234-false-positive",
     "expected_no_findings": [
       {"path": "src/server.c", "line": 42, "title": "..."}
     ]
   }
   ```
3. Run:
   ```
   python -m scripts.ai_eval.harness --mode deterministic \
     --fixtures scripts/ai_eval/fixtures/gold
   ```
4. If the harness still passes with this fixture present, tighten the
   rubric or specialist reviewer prompts until it catches the false
   positive.

### "Token usage spiked — how do I find the regression?"

1. Tail the structured stage logs:
   ```
   python -m scripts.ai_eval.report --token-cost --since 7d \
     --logs /path/to/stage-logs.ndjson
   ```
2. Look at per-stage averages. The analyst and fix generator are normally
   the heaviest; if a non-obvious stage spiked, inspect its prompt or
   evidence size.
3. Correlate with `--stage-latencies` to see if duration also grew.

### "The pipeline is creating bad PRs. How do I pause it?"

1. Disable the relevant GitHub Actions workflow (Settings → Actions →
   Workflows) OR push a config change setting
   `ai.stages.fixes.candidate_count: 0` via `.github/valkey-daily-bot.yml`
   (or the appropriate repository config). A zero candidate count means
   no tournament can produce a winner, so every failure routes to
   `needs-human`.
2. Root-cause the regression with:
   ```
   python -m scripts.ai_eval.harness --mode deterministic \
     --fixtures scripts/ai_eval/fixtures/gold
   ```
   to see which gold fixtures changed behavior.

### "I want to add a new deterministic rubric check."

1. Add the check function to `scripts/stages/rubric.py` with a
   `RubricCheck` return value.
2. Add it to `run_deterministic_rubric()`.
3. Add a unit test in `tests/test_stages_rubric.py` covering pass and
   fail cases.
4. Run `pytest tests/test_stages_rubric.py`.

## Structured logging

Every stage emits a JSON-line log record on completion:

```json
{"stage": "root_cause", "failure_id": "fp-abc", "duration_ms": 4200,
 "tokens_in": 8000, "tokens_out": 600, "outcome": "accepted",
 "rejection_reason": ""}
```

Consumers:

- `scripts/ai_eval/report.py --stage-latencies` for p50/p90/p99
- `scripts/ai_eval/report.py --token-cost` for per-stage totals
- Grep / `jq` for custom queries

## Integration with the existing infrastructure

The new pipeline is a thin orchestrator over the existing
`pr_manager`, `code_reviewer`, and `failure_store` modules. The
bridge lives in `scripts/pipeline_adapter.py`:

- `evidence_to_failure_report` — converts an `EvidencePack` to the
  legacy `FailureReport` shape expected by `PRManager.create_pr`
- `hypothesis_to_root_cause_report` — converts a `RootCauseHypothesis`
  to the legacy `RootCauseReport` shape
- `evidence_to_pr_review_context` — converts PR-review evidence to
  the `(PullRequestContext, DiffScope)` pair expected by
  `CodeReviewer.review`
- `update_failure_store_entry` — applies pipeline outcome state
  (evidence pack, rejection reason, pr_url, status) to an existing
  `FailureStoreEntry`
- `create_pr_via_legacy_manager` — convenience wrapper that does the
  adapter translation and invokes `PRManager.create_pr`

### State persistence

When `pipeline.process_failure` is called with a `failure_store`
argument, the pipeline automatically updates the store entry after
every run with:

- `evidence_pack` (the full EvidencePack dict)
- `rejection_reason` (if the failure was routed to needs-human)
- `pr_url` (if a PR was created)
- `status` (`"processing"` on PR creation, `"needs-human"` on rejection,
  `"error"` on unexpected exception)

The entry must already exist in the store; the pipeline never fabricates
new entries (that's the monitor's responsibility).

## Live Bedrock smoke test

`scripts/ai_eval/live_smoke.py` exercises the real Bedrock API
contract against a realistic `EvidencePack`. This is the "does the
pipeline actually work with a real model" check — separate from the
unit tests that use mocks.

```bash
# Evidence stage only (no Bedrock)
python -m scripts.ai_eval.live_smoke --stages evidence

# Full pipeline smoke against a gold fixture
python -m scripts.ai_eval.live_smoke \
  --fixture scripts/ai_eval/fixtures/gold/ci_failures/happy-hash-race-fix.json

# Specific stages only
python -m scripts.ai_eval.live_smoke --stages evidence,root_cause
```

The script forces `VALKEY_CI_AGENT_DRY_RUN=1` so it can never
accidentally publish. Exit code is 0 only if every exercised stage
completes without exception (a clean rejection is considered a
success — the pipeline behaved as designed).

Run this manually after every significant change to prompt templates
or model configuration. The unit tests catch logic errors; the smoke
test catches API-contract errors.

## Replay eval

CI runs `.github/workflows/ai-eval.yml` on every PR that touches
pipeline or stage code. The harness:

1. Loads every fixture under `scripts/ai_eval/fixtures/gold/`
2. Runs the pipeline against the fixture's `EvidencePack`
   (deterministic mode skips live model calls — uses stored outputs)
3. Scores each fixture against its expected annotations
4. Exits non-zero if any scorer threshold is breached

Live mode (`--mode live`) makes real Bedrock calls and regenerates
stage outputs. Run it manually via `workflow_dispatch`.

### Adding a new gold fixture

Use the capture CLI on a real failure:

```
python -m scripts.ai_eval.capture_gold \
  --fixture-id my-new-case \
  --evidence /tmp/evidence.json \
  --expected-keywords "race,mutex" \
  --expected-rejection-reason "" \
  --output-dir scripts/ai_eval/fixtures/gold/ci_failures
```

Then edit the written JSON to add `expected_fix_properties` if you know
what a good fix should look like.

## Config knobs

| Config | Default | Meaning |
|---|---|---|
| `ai.min_confidence_for_fix` | `"medium"` | Reject hypotheses below this confidence |
| `ai.fixes.candidate_count` | `1` | Number of diverse fix candidates per tournament |
| `ai.fixes.max_parallel_candidates` | `2` | Per-failure parallel generation cap |
| `ai.fixes.global_validation_concurrency` | `3` | Global semaphore across all tournaments |
| `ai.fixes.require_passing_validation` | `true` | Reject candidates that don't validate |
| `ai.stages.<stage>.model` | `""` | Per-stage model override (empty = use `bedrock.model_id`) |

## Emergency stop

If the agent is producing actively harmful PRs and you need to stop
everything:

1. Disable the agent workflows in GitHub Actions.
2. Close any open agent PRs.
3. Revert any suspect merged PRs.
4. Inspect the `EvidencePack` of the failing cases — that's the
   attached context. It should show which guardrail failed.

If the rubric gate let something through that it shouldn't have:
reproduce with a unit test first, then strengthen the rubric
(`scripts/stages/rubric.py`). Do not ship the fix without the test.

## The publish kill-switch

Every GitHub write (PR creation, issue creation, comment posting,
workflow dispatch, mark-ready-for-review) goes through
`scripts/publish_guard.py`. The guard is **default-deny**: if none of
the enabling flags are set, any write raises `PublishBlocked`.

### Environment variables

| Variable | Effect |
|---|---|
| `VALKEY_CI_AGENT_DRY_RUN` | If truthy, **blocks all writes** regardless of other flags. Use this as an emergency brake. |
| `VALKEY_CI_AGENT_ALLOW_PUBLISH` | Required opt-in. Without this, the guard blocks. |
| `VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH` | Additional opt-in required for writes to `valkey-io/valkey` or `valkey-io/valkey-fuzzer`. |
| `VALKEY_CI_AGENT_ALLOWED_REPOS` | Optional comma-separated allow-list (e.g. `sarthakaggarwal97/valkey,test-org/fork`). Empty = no restriction beyond the flags above. |

### Typical configurations

**Dev / testing** — block everything:
```
# no env vars set; default-deny
```

**Fork-only publishing** (safe default for active development):
```
export VALKEY_CI_AGENT_ALLOW_PUBLISH=1
export VALKEY_CI_AGENT_ALLOWED_REPOS="sarthakaggarwal97/valkey"
# valkey-io writes blocked by the two-layer guard
```

**Full production** (explicit opt-in for upstream):
```
export VALKEY_CI_AGENT_ALLOW_PUBLISH=1
export VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH=1
```

**Emergency stop** (immediately block writes without touching workflows):
```
export VALKEY_CI_AGENT_DRY_RUN=1
# DRY_RUN beats everything else
```

### What the guard covers

- `pr_manager.PRManager.create_pr` → via the gated `_upsert_pull_request`
- `pr_manager.PRManager.post_summary_comment` → gated before `create_issue_comment`
- `backport_pr_creator.BackportPRCreator.create_backport_pr` → gated before `create_pull`
- `fuzzer_issue_publisher.FuzzerIssuePublisher.upsert_issue` → gated before `create_issue`, `edit`, and `create_comment`
- `comment_publisher.CommentPublisher` (all 5 public methods: `upsert_summary`, `approve_pr`, `publish_review_comments`, `publish_review_note`, `publish_chat_reply`)
- `backport_main._post_comment` → gated before `create_issue_comment`
- `prove_pr_fix._upsert_proof_comment` → gated before `create_comment`/`comment.edit`
- `prove_pr_fix._mark_ready_for_review` → gated before the ready-for-review REST call
- `main._dispatch_workflow` and `demo_bundle._dispatch_workflow` → gated before workflow dispatch

### Test configuration

In `tests/conftest.py`, the autouse fixture `allow_publish_in_tests`
sets `ALLOW_PUBLISH=1` and `ALLOW_VALKEY_IO_PUBLISH=1` so tests that
mock GitHub writes can run. Tests in `test_publish_guard.py` opt out
via `@pytest.mark.disable_publish_autouse` to verify the default-deny
behavior.

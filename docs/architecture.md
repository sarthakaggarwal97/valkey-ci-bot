# Architecture Overview

## System Design

The valkey-ci-agent is a collection of Python modules invoked by GitHub
Actions workflows. It has no long-running process: every pipeline is a
single `python -m scripts.<entry>` invocation that reads durable state from
a `bot-data` branch, does work, and writes state back.

The LLM runtime is the **Claude Code CLI**, configured to call Claude Opus
4 via Amazon Bedrock (`CLAUDE_CODE_USE_BEDROCK=1`). Every AI call is a
`claude --print` subprocess routed through a named **AgentProfile** that
declares the allowed tool set, timeout, and turn budget. There is no
direct Bedrock client path in any production flow.

```
          ┌────────────── GitHub Actions Triggers ───────────────┐
          │  schedule · pull_request · workflow_dispatch · push  │
          └───┬───────────┬──────────┬────────────┬──────────────┘
              │           │          │            │
        ┌─────▼─────┐ ┌───▼───┐ ┌────▼────┐ ┌────▼───────┐
        │ PR        │ │ Daily │ │ Fuzzer  │ │ Backport   │
        │ Reviewer  │ │ CI    │ │ Triage  │ │ + Sweep    │
        │           │ │ Fixer │ │         │ │            │
        └─────┬─────┘ └───┬───┘ └────┬────┘ └─────┬──────┘
              │           │          │             │
              └────┬──────┴────┬─────┴──────┬──────┘
                   │           │            │
              ┌────▼───────────▼────────────▼────┐
              │       Claude Code (Opus 4)       │
              │   via agent_runtime.run_agent()  │
              └────┬─────────────────────────────┘
                   │
              ┌────▼──────────────────────────────┐
              │   Durable state on bot-data       │
              │   failure-store / rate-state /    │
              │   review-state / monitor-state /  │
              │   agent-events.jsonl              │
              └───────────────────────────────────┘
                   │
              ┌────▼──────────────────────────────┐
              │   Dashboards (static HTML)        │
              │   published via GitHub Pages      │
              └───────────────────────────────────┘
```

## Five Production Flows

### 1. PR Reviewer

Entry points: `scripts/pr_review_main.py`, orchestrating `scripts/claude_reviewer.py`.
Workflows: `.github/workflows/review-pr.yml` (trusted PRs), `review-external-pr.yml` (fork PRs).

Two-stage review, both stages running under the `review_readonly`
profile (Read/Grep/Glob only, Opus, max effort):

1. **Stage 1 — deep review.** Claude reads the checked-out PR head and
   produces candidate findings as JSON. Prose-to-JSON retry handles
   non-strict output.
2. **Stage 2 — skeptic pass.** A second Claude run inspects every
   candidate finding by re-reading the cited file and drops
   speculative, duplicate, style-only, or weakly supported findings.
   Silent/empty skeptic output is a conservative keep.

Supporting modules:

- `scripts/review_diff.py` — diff-map construction and per-finding
  commentability validation (line-in-diff, path is commentable).
- `scripts/pr_context_fetcher.py` — builds `PullRequestContext` /
  `DiffScope` from the GitHub API (files, commits, existing review
  threads) with incremental reviewing in mind.
- `scripts/valkey_knowledge.py` — compact Valkey-vs-Redis divergence
  block injected into prompts so Opus does not flag Valkey-current
  symbols as wrong.
- `scripts/review_state_store.py` — persists per-PR review state
  (`review-state.json` on `bot-data`) so re-reviews only look at new
  commits.
- `scripts/review_policy.py` — renders the policy note the agent posts
  alongside its findings.
- `scripts/permission_gate.py` — enforces collaborator / fork-safety
  checks before a review is allowed to run.
- `scripts/comment_publisher.py` — batched review submission.

### 2. Daily CI Fix Generator

Entry point: `run_pipeline` in `scripts/main.py`.
Workflow: `.github/workflows/monitor-valkey-daily.yml` (with
`analyze-failure.yml`, `validate-fix.yml`, `prove-daily-fix.yml` as
subordinate workflows).

Fix generation is Claude Code-only; there is no heuristic or Bedrock
fallback path. Structured log parsers produce metadata; the raw log is
still passed to Claude as the source of truth.

```
 Workflow run failure
   → FailureDetector.detect()              # skip infra/cancelled runs
   → LogRetriever.retrieve()               # download job logs
   → LogParserRouter.parse()               # 9 deterministic parsers
   → FailureStore.record()                 # dedup + history on bot-data
   → RootCauseAnalyzer.analyze()           # Claude Code (review_readonly)
   → FixGenerator.generate()               # Claude Code (fix_generate_patch)
   → ValidationRunner.validate()           # CI-exact build/test
   → fix_loop.run()                        # push, dispatch daily.yml, retry
   → PRManager.create_pr()                 # open PR with approval gate
```

Supporting modules:

- `scripts/claude_fix.py` — one-shot log-to-fix Claude Code call used
  when the direct publisher path is enabled
  (`CI_AGENT_ENABLE_DIRECT_CLAUDE_FIX`).
- `scripts/fix_loop.py` — generate, push, dispatch daily.yml on the
  fork, poll, and retry with the new failure as extra context.
- `scripts/validation_runner.py` — builds and runs the failing
  test locally against the agent-applied patch.
- `scripts/ci_validator.py` — dispatches workflow runs and polls for
  completion.
- `scripts/preflight_reconciliation.py` — reconciles state with the
  upstream default branch before spending Claude budget on stale
  failures.
- `scripts/publish_guard.py` — last-line check that blocks writes to
  the upstream repo unless the env flag is set.

### 3. Fuzzer Triage

Entry point: `scripts/monitor_fuzzer_runs.py`.
Workflow: `.github/workflows/monitor-valkey-fuzzer.yml`.

```
 Fuzzer run (daily-anti-flaky / fuzzer workflow) completes
   → MonitorStateStore watermark advances
   → FuzzerRunAnalyzer.analyze()           # Claude Code (fuzzer_analysis_readonly)
         • downloads artifacts via workflow_artifact_client
         • reads failed check output and crashes
         • emits FuzzerRunAnalysis JSON
   → fuzzer_incidents.compute_fingerprint  # stable dedup key
         • hex/addr/nodes/numbers normalized to placeholders
   → FuzzerIssuePublisher.publish()        # create/update GitHub issue
   → EventLedger.append()                  # audit trail on bot-data
```

Supporting modules:

- `scripts/workflow_artifact_client.py` — artifact zip discovery and
  extraction.
- `scripts/fuzzer_incidents.py` — fingerprint computation that strips
  volatile substrings so the same underlying bug dedups across runs.
- `scripts/fuzzer_issue_publisher.py` — issue creation/updating keyed
  by fingerprint.

### 4. Backports

Entry points: `scripts/backport_main.py` (single-PR), `scripts/backport_sweep.py` (weekly Projects-v2 batch).
Workflows: `.github/workflows/backport.yml`, `manual-backport.yml`, `weekly-backport-sweep.yml`.

```
 Trigger: "backport <branch>" label | weekly Projects v2 board sweep
   → CherryPickExecutor.execute()          # git cherry-pick with retry
   → resolve_conflicts_with_claude()       # Claude (conflict_resolve_edit_only)
   → assess_backport_risk()                # deterministic risk score
   → BackportPRCreator.create_or_update()  # one open PR per release branch
```

Risk scoring (`scripts/backport_risk.py`) is purely deterministic:

- Touches cluster / replication / aof / rdb / acl / cli / networking: **+2**
- Conflicts resolved during cherry-pick: **+2**
- Target branch major version is older than the current dev line: **+1**
- Docs-only change: **−1**

Supporting modules:

- `scripts/cherry_pick.py` — per-commit cherry-pick with conflict
  capture.
- `scripts/claude_conflict_resolver.py` — thin wrapper that invokes
  `run_agent("conflict_resolve_edit_only", ...)` with the conflicted
  worktree as cwd.
- `scripts/backport_config.py`, `scripts/backport_models.py`,
  `scripts/backport_pr_creator.py`, `scripts/backport_utils.py` —
  config, dataclasses, PR creation, branch naming.
- `scripts/git_auth.py` — `GIT_ASKPASS`-based token handling; tokens
  never end up in remote URLs or `.git/config`.
- `scripts/commit_signoff.py` — DCO sign-off enforcement on generated
  commits.

### 5. Dashboards

Entry points: `scripts/agent_dashboard.py` (data generation),
`scripts/agent_dashboard_site.py` (static site publishing),
`scripts/daily_health_report.py` (daily CI health subreport),
`scripts/agent_dashboard_site_public.py` (public filtered view).
Workflows: `.github/workflows/agent-dashboard.yml`, `publish-dashboard-site.yml`.

The dashboard reads durable state from the `bot-data` branch, produces a
single `dashboard.json` payload validated by
`scripts/validate_dashboard_schema.py`, and copies the checked-in
`dashboard-app/` SPA alongside it. Rendering happens in the browser —
Python only writes JSON. Multi-page views (Overview / Daily CI / PRs /
Fuzzer / Ops) are routes inside the SPA.

## Shared Primitives

| Module | Purpose |
|--------|---------|
| `claude_code.py` | `run_claude_code()` subprocess wrapper. Builds a minimal env (passthrough allowlist strips `GITHUB_TOKEN`, `GH_TOKEN`, `*_SECRET`), forces `CLAUDE_CODE_USE_BEDROCK=1`, streams JSONL stdout, and logs summarized events. |
| `agent_runtime.py` | `AgentProfile` registry + `run_agent()` wrapper. Adds audit metadata (prompt SHA-256, model, started/finished at) and optional SHA-hashed evidence files (`CI_AGENT_EVIDENCE_DIR` / `agent-evidence/`). |
| `claude_workspace.py` | Context manager for the recurring pattern: clone a repo with `GitAuth`, optionally checkout a ref, yield a `WorkspaceContext`, clean up the tempdir. Used by every flow that runs Claude against a live worktree. |
| `git_auth.py` | `GitAuth` writes a short-lived `GIT_ASKPASS` helper to the OS temp dir. Tokens are never embedded in URLs, `.git/config`, or worktrees AI tools can read. |
| `event_ledger.py` | Append-only JSONL event log (`agent-events.jsonl` on `bot-data`). Primary source of truth for the dashboard. |
| `failure_store.py` | Structured failure persistence (`failure-store.json` on `bot-data`), plus flaky-campaign state and history. |
| `rate_limiter.py` | Daily PR caps, open-PR caps, and token budget tracking (`rate-state.json` on `bot-data`). |
| `review_state_store.py` | Incremental review state per PR (`review-state.json` on `bot-data`). |
| `monitor_state_store.py` | Watermarks for the daily and fuzzer monitors (`monitor-state.json` on `bot-data`). |
| `pr_context_fetcher.py` | PR diff/file/review-thread fetching from the GitHub API. |
| `log_parser.py` + `scripts/parsers/` | Deterministic log parsing. No AI fallback. |
| `log_retriever.py` | Workflow-run log zip download. |
| `workflow_artifact_client.py` | Workflow-run artifact zip download. |
| `github_client.py` | `retry_github_call` helper around PyGithub operations. |
| `publish_guard.py` | Env-flag guard that blocks writes to the real `valkey-io` repo unless explicitly allowed. |
| `json_helpers.py` | Safe coercion helpers (`safe_int`, `safe_str`, `mapping`, ...) used across dashboard and state code. |

## Agent Profiles

Every Claude Code invocation goes through one of the profiles below.
The profile sets the tool allowlist, timeout, turn limit, and effort
passed to the CLI. All profiles run Opus and use the max-effort setting.

| Profile | Allowed tools | Timeout | max_turns | Writes | Used by |
|---------|---------------|---------|-----------|--------|---------|
| `review_readonly` | Read, Grep, Glob | 3600s | 240 | no | PR reviewer (both stages), root cause analyzer |
| `summary_readonly` | Read, Grep, Glob | 1800s | 160 | no | PR summary replies |
| `chat_readonly` | Read, Grep, Glob | 1800s | 160 | no | Review-comment chat replies |
| `conflict_resolve_edit_only` | Read, Edit, MultiEdit, Grep, Glob | 3600s | 240 | yes | Backport conflict resolver |
| `fix_generate_patch` | Read, Edit, MultiEdit, Write, Grep, Glob | 3600s | 240 | yes | Daily CI fix generator / `fix_loop` |
| `fuzzer_analysis_readonly` | Read, Grep, Glob | 3600s | 240 | no | Fuzzer run analyzer |

Profiles are immutable dataclasses in `agent_runtime.py`. Adding a new
kind of AI task means adding a profile, not changing individual call
sites.

## Module Map

### Orchestrators / CLI entry points

| Module | Purpose |
|--------|---------|
| `main.py` | Daily CI fix pipeline orchestrator (`run_pipeline`) + CLI. |
| `pr_review_main.py` | PR reviewer entry point. |
| `backport_main.py` | Single-PR backport orchestrator. |
| `backport_sweep.py` | Weekly Projects-v2 backport sweep. |
| `monitor_fuzzer_runs.py` | Centralized fuzzer-run monitor. |
| `monitor_workflow_runs.py` | Generic workflow-run monitor utilities. |
| `prove_pr_fix.py` | End-to-end proof dispatch for agent-generated PRs. |
| `daily_health_report.py` | Daily CI health report generator. |
| `agent_dashboard.py` | Dashboard payload generator. |
| `agent_dashboard_site.py` | Dashboard static-site publisher. |
| `agent_dashboard_site_public.py` | Public-filtered dashboard publisher. |
| `valkey_acceptance.py` | Report-only Valkey readiness harness (standalone). |

### Claude Code integrations

| Module | Purpose |
|--------|---------|
| `claude_code.py` | `run_claude_code()` subprocess wrapper. |
| `agent_runtime.py` | Profiles + `run_agent()` + evidence. |
| `claude_workspace.py` | Clone-and-run-Claude context manager. |
| `claude_reviewer.py` | Two-stage PR review, summary, and chat-reply entry functions. |
| `claude_fix.py` | Direct log-to-fix Claude call. |
| `claude_conflict_resolver.py` | Cherry-pick conflict resolver via Claude. |
| `fix_generator.py` | Worktree-diff fix generator under `fix_generate_patch`. |
| `fix_loop.py` | Fix / push / dispatch daily.yml / retry loop. |
| `root_cause_analyzer.py` | RCA under `review_readonly`. |
| `fuzzer_run_analyzer.py` | Fuzzer artifact analysis under `fuzzer_analysis_readonly`. |

### Log parsing (`scripts/parsers/`)

| Parser | Covers |
|--------|--------|
| `valkey_crash_parser.py` | Valkey-specific crash signatures (priority 5). |
| `sanitizer_parser.py` | ASAN / UBSan / LeakSanitizer (priority 10). |
| `valgrind_parser.py` | Valgrind memory errors and leaks (priority 20). |
| `build_error_parser.py` | gcc / clang compile errors (priority 30). |
| `gtest_parser.py` | Google Test failures (priority 40). |
| `module_api_parser.py` | Module API test failures (priority 50). |
| `rdma_parser.py` | RDMA test failures (priority 60). |
| `sentinel_cluster_parser.py` | Sentinel / cluster test failures (priority 70). |
| `tcl_parser.py` | `runtest` Tcl failures (priority 80). |

### State on `bot-data`

| Module | File | Holds |
|--------|------|-------|
| `event_ledger.py` | `agent-events.jsonl` | Append-only audit log. |
| `failure_store.py` | `failure-store.json` | Failure dedup + flaky-campaign state. |
| `rate_limiter.py` | `rate-state.json` | Daily PR / token budgets. |
| `review_state_store.py` | `review-state.json` | Incremental PR review state. |
| `monitor_state_store.py` | `monitor-state.json` | Daily / fuzzer monitor watermarks. |

### Eval framework (`scripts/eval/`)

| Module | Purpose |
|--------|---------|
| `eval_fixtures.py` | `EvalFixture` dataclass + `load_fixtures` from `eval/fixtures/*.json`. |
| `eval_scorer.py` | `score_style` — forensic-pattern detector that penalizes methodology leakage ("I ran", "git cat-file", ...). |
| `flow_scorer.py` | Per-flow scorers: strict `score_review_flow` (path + line within 10), loose `score_review_flow_loose` (file-match partial credit 0.5), and `score_daily_flow` / `score_backport_flow` / `score_fuzzer_flow`. |
| `review_runner.py` | Local CLI that runs `claude_reviewer.review_pr` on a fixture without posting. |
| `report.py` | Markdown report generation from `FlowScore` objects. |

### Miscellaneous support

| Module | Purpose |
|--------|---------|
| `config.py` | YAML loader with `BotConfig` / `ReviewerConfig` dataclasses. |
| `models.py`, `backport_models.py` | Shared dataclasses (PR context, failure report, backport context, ...). |
| `path_filter.py` | Reviewable-path filtering. |
| `review_policy.py` | Policy-note rendering. |
| `text_utils.py`, `html_helpers.py` | Fence stripping and HTML escaping helpers. |
| `demo_bundle.py`, `generate_fixtures.py`, `build_valkey_acceptance_manifest.py` | Developer tooling for demos, fixtures, and the acceptance manifest. |

## Security Model

- **No secrets in code.** All credentials come from GitHub Actions
  secrets or OIDC. No tokens are written to disk inside a worktree.
- **Claude env allowlist.** `claude_code.py` strips `GITHUB_TOKEN`,
  `GH_TOKEN`, and any `*_SECRET` from the subprocess environment before
  invoking `claude`. Only a fixed passthrough set (PATH, HOME, TMPDIR,
  locale, TLS CA bundles, AWS credentials needed for Bedrock) is
  forwarded.
- **Git auth via askpass.** `GitAuth` writes a short-lived helper
  script to the OS temp dir and sets `GIT_ASKPASS`. Tokens never appear
  in remote URLs or `.git/config`.
- **Prompt-injection fencing.** Every LLM prompt includes explicit
  "treat X as untrusted data" delimiters around PR bodies, diffs, log
  excerpts, and artifacts.
- **Fork safety.** External PR bodies and diffs are treated as
  adversarial input. `permission_gate.py` enforces collaborator checks
  before a review is allowed on trusted flows; external reviews run in
  the `pull_request_target` workflow with read-only profiles only.
- **SHA-hashed evidence.** Per-call audit files live under
  `agent-evidence/` (or `CI_AGENT_EVIDENCE_DIR`) and store SHA-256
  digests of stdout/stderr instead of raw content. These are uploaded
  as workflow artifacts with 30-day retention.
- **Safe YAML.** Every config load goes through `yaml.safe_load`.
- **HTML escaping.** Dashboard output uses `html.escape()` around all
  user-supplied strings.
- **Publish guard.** `publish_guard.check_publish_allowed` blocks
  writes to the real upstream repo unless the operator explicitly
  opts in.

## Configuration

Bot and reviewer configs are YAML, loaded by `scripts/config.py`. Samples
live in `examples/config.yml` and `examples/pr-review-config.yml`. All
fields have sensible defaults; invalid values are clamped by
`__post_init__` validators on the dataclasses.

Key sections of `BotConfig`:

- `project.*` — language, build system, source/test dirs, project
  description for prompts.
- `limits.*` — daily PR caps, open-PR caps, token budgets.
- `validation.*` — CI-exact validation profiles.
- `flaky_campaign.*` — settings for the flaky-test remediation
  campaign.

Key sections of `ReviewerConfig`:

- `models.*` — reviewer model aliases.
- `path filters`, policy note, subsystem context — control what
  reviewer sees and says.

Runtime environment overrides:

| Variable | Effect |
|----------|--------|
| `CI_AGENT_CLAUDE_MODEL` | Override the Claude Code model alias (default: `opus`). |
| `CI_AGENT_CLAUDE_BEDROCK_OPUS_MODEL` | Override the Bedrock Opus model / inference profile (default: `us.anthropic.claude-opus-4-7`). |
| `CI_AGENT_EVIDENCE_DIR` | Directory where `run_agent` writes SHA-hashed audit files. |
| `CI_AGENT_DISABLE_CLAUDE_PATCH_GENERATOR` | Disable the Claude Code fix generator (defensive kill-switch). |
| `CI_AGENT_ENABLE_DIRECT_CLAUDE_FIX` | Opt in to the direct log-to-fix publisher path. |
| `VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH` | Operator opt-in required by `publish_guard` before writes to the real upstream repo. |

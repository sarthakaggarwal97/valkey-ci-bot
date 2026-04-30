# Architecture Overview

## System Design

The valkey-ci-agent is a collection of Python modules orchestrated by GitHub Actions workflows. It operates as a stateless pipeline with durable state stored on a `bot-data` branch.

```
┌─────────────────────────────────────────────────────────────┐
│                    GitHub Actions Triggers                    │
│  (CI failure, PR opened, schedule, manual dispatch)          │
└──────────┬──────────┬──────────┬──────────┬────────────────┘
           │          │          │          │
     ┌─────▼──┐ ┌────▼───┐ ┌───▼────┐ ┌──▼──────┐
     │  CI    │ │  PR    │ │Backport│ │ Monitor │
     │Failure │ │ Review │ │ Agent  │ │ (Daily/ │
     │ Agent  │ │ Agent  │ │        │ │ Fuzzer) │
     └───┬────┘ └───┬────┘ └───┬────┘ └───┬─────┘
         │          │          │           │
    ┌────▼──────────▼──────────▼───────────▼────┐
    │           Shared Infrastructure            │
    │  ┌──────────┐ ┌──────────┐ ┌───────────┐  │
    │  │ Bedrock  │ │ GitHub   │ │ Failure   │  │
    │  │ Client   │ │ Client   │ │ Store     │  │
    │  └──────────┘ └──────────┘ └───────────┘  │
    │  ┌──────────┐ ┌──────────┐ ┌───────────┐  │
    │  │ Rate     │ │ Event    │ │ Config    │  │
    │  │ Limiter  │ │ Ledger   │ │ Loader    │  │
    │  └──────────┘ └──────────┘ └───────────┘  │
    └───────────────────────────────────────────┘
```

## Data Flow

### CI Failure Pipeline
```
Workflow Failure
  → FailureDetector.detect()        # filter infra failures
  → LogRetriever.retrieve()         # download job logs
  → LogParserRouter.parse()         # structured extraction (8 parsers)
  → CorrelationEngine.correlate()   # cluster related failures
  → RootCauseAnalyzer.analyze()     # Bedrock-powered RCA
  → FixGenerator.generate()         # generate + validate patch
  → ValidationRunner.validate()     # CI-exact build/test
  → PRManager.create_pr()           # open PR with approval gate
```

### PR Review Pipeline
```
PR Opened/Updated
  → PRContextFetcher.build_scope()  # diff, files, incremental state
  → CodeReviewer.review()           # agentic tool-use review loop
  → SkepticVerifier.verify()        # second-pass false-positive filter
  → CommentPublisher.publish()      # batched review submission
  → ReviewStateStore.save()         # persist incremental state
```

#### Specialist Review Mode

When `specialist_mode: true` is set in the reviewer config, the PR Review Pipeline runs 9 specialist reviewers in parallel (via `ThreadPoolExecutor`) alongside the standard `CodeReviewer`. Each specialist makes a single Bedrock call focused on one concern:

| Specialist | Focus |
|------------|-------|
| Test Runner | Test coverage of changed code paths |
| Linter & Static Analysis | Compiler warnings, macro hygiene, naming |
| Code Reviewer | Correctness bugs, logic errors, concurrency hazards |
| Security Reviewer | Injection, auth bypass, buffer overflows, use-after-free |
| Quality & Style | Complexity, dead code, convention violations |
| Test Quality | Flakiness risks, assertion quality, edge cases |
| Performance Reviewer | Hot-path allocations, algorithmic complexity, memory leaks |
| Dependency & Deployment Safety | Breaking changes, migration safety, observability gaps |
| Simplification & Maintainability | Unnecessary abstractions, change atomicity |

Findings are deduplicated, ranked by severity, and synthesized into a verdict:

- **Ready to Merge** — no critical or high-severity findings
- **Needs Attention** — medium-severity findings only
- **Needs Work** — critical or high-severity findings present

### Backport Pipeline
```
Label Added: "backport <branch>"
  → CherryPick.execute()           # git cherry-pick with retry
  → ConflictResolver.resolve()     # agentic LLM conflict resolution
  → BackportPRCreator.create()     # open backport PR
```

## Module Map

### Core Pipeline (`scripts/`)
| Module | Purpose |
|--------|---------|
| `main.py` | CI failure pipeline orchestrator + CLI |
| `config.py` | YAML config loading with validation |
| `models.py` | Shared dataclasses (WorkflowRun, FailureReport, etc.) |
| `exceptions.py` | Custom exception hierarchy |

### Log Parsing (`scripts/parsers/`)
| Parser | Priority | Covers |
|--------|----------|--------|
| `sanitizer_parser.py` | 10 | ASAN, UBSan, LeakSanitizer |
| `valgrind_parser.py` | 20 | Valgrind memory errors + leaks |
| `build_error_parser.py` | 30 | gcc/clang compile errors |
| `gtest_parser.py` | 40 | Google Test failures |
| `module_api_parser.py` | 50 | Module API test failures |
| `rdma_parser.py` | 60 | RDMA test failures |
| `sentinel_cluster_parser.py` | 70 | Sentinel/cluster test failures |
| `tcl_parser.py` | 80 | Tcl runtest failures |

### Analysis & Intelligence
| Module | Purpose |
|--------|---------|
| `root_cause_analyzer.py` | Bedrock-powered RCA with agentic tool-use |
| `correlation_engine.py` | Cross-failure clustering before RCA |
| `fix_generator.py` | Patch generation + build validation |
| `failure_detector.py` | Infrastructure failure filtering |
| `specialist_reviewer.py` | 9-specialist parallel review with verdict synthesis |
| `review_feedback.py` | PR review accuracy tracking |
| `fuzzer_trends.py` | Per-scenario failure rate trends |

### Safety & Rate Limiting
| Module | Purpose |
|--------|---------|
| `rate_limiter.py` | Daily PR limits, token budgets, queue management |
| `permission_gate.py` | Collaborator permission checks |
| `bedrock_client.py` | Bedrock API with retry + backoff |
| `alerting.py` | Webhook/Slack notifications |
| `sla_metrics.py` | Operation timing + cost tracking |

### State Persistence
| Module | Storage |
|--------|---------|
| `failure_store.py` | `bot-data` branch: failure-store.json |
| `rate_limiter.py` | `bot-data` branch: rate-state.json |
| `review_state_store.py` | `bot-data` branch: review-state.json |
| `monitor_state_store.py` | `bot-data` branch: monitor-state.json |
| `event_ledger.py` | `bot-data` branch: agent-events.jsonl |

## Security Model

- **No secrets in code** — all credentials via GitHub Actions secrets/OIDC
- **Prompt injection defense** — all system prompts include untrusted-data fencing
- **Fork safety** — untrusted fork PRs are gated by `PermissionGate`
- **Rate limiting** — daily PR caps, token budgets, open PR limits
- **Safe YAML** — `yaml.safe_load` used everywhere, never `yaml.load`
- **HTML escaping** — all dashboard output uses `html.escape()` wrappers

## Configuration

Config is loaded from YAML files (see `examples/config.yml` and `examples/pr-review-config.yml`). All fields have sensible defaults. Invalid values are clamped to valid ranges by `__post_init__` validators.

Key config sections:
- `bedrock.*` — model ID, token limits, thinking budget, max retries
- `limits.*` — PR caps, failure limits, token budgets
- `validation.*` — require_profile, soak settings
- `flaky_campaign.*` — campaign settings for flaky test remediation
- `project.*` — language, build system, source/test dirs
- `retrieval.*` — Bedrock Knowledge Base settings

# Implementation Plan: CI Failure Bot

## Overview

Phased implementation of the CI failure bot. Each phase is independently testable: Phase 1 with mocked GitHub API, Phase 2 with mocked Bedrock, Phase 3 with mocked external services, Phase 4 as GitHub Actions workflows, and Phase 5 for observability. All code is Python 3.11+ in the `valkey-ci-bot` repository.

## Tasks

- [x] 1. Phase 1: Core Infrastructure
  - [x] 1.1 Set up project structure and dependencies
    - Create the repository layout: `scripts/`, `scripts/parsers/`, `tests/`
    - Create `requirements.txt` (boto3, PyGithub, PyYAML) and `requirements-dev.txt` (pytest, hypothesis, mypy, pytest-mock, pytest-cov)
    - Create `scripts/__init__.py`, `scripts/parsers/__init__.py`, `tests/__init__.py`, `tests/conftest.py`
    - _Requirements: 7.1, 8.1_

  - [x] 1.2 Implement data models (`scripts/models.py`)
    - Define dataclasses: `WorkflowRun`, `FailedJob`, `ParsedFailure`, `FailureReport`, `RootCauseReport`, `ValidationResult`, `FailureStoreEntry`
    - Include all fields from the design document's Data Models section
    - _Requirements: 2.6, 3.4_

  - [x] 1.3 Write property test for FailureReport completeness
    - **Property 5: FailureReport contains all required fields**
    - **Validates: Requirements 2.6**

  - [x] 1.4 Implement configuration loader (`scripts/config.py`)
    - Define `BotConfig` and `ProjectContext` dataclasses with defaults
    - Load YAML from path, merge with defaults, handle missing file and invalid YAML gracefully
    - _Requirements: 8.1, 8.2, 8.3, 8.4_

  - [x] 1.5 Write property tests for configuration
    - **Property 17: Configuration round-trip**
    - **Validates: Requirements 8.2, 8.3**

  - [x] 1.6 Write property test for invalid config fallback
    - **Property 18: Invalid config falls back to defaults**
    - **Validates: Requirements 8.4**

  - [x] 1.7 Implement failure detector (`scripts/failure_detector.py`)
    - `FailureDetector.detect()`: given a `WorkflowRun`, use GitHub API (mocked in tests) to list failed jobs, filter out infrastructure failures
    - `FailureDetector.is_infrastructure_failure()`: heuristic matching known patterns (runner timeout, network error, rate limit)
    - _Requirements: 1.1, 1.3, 1.4_

  - [x] 1.8 Write property test for infrastructure failure classification
    - **Property 1: Infrastructure failure classification**
    - **Validates: Requirements 1.4**

  - [x] 1.9 Implement log retriever and log parser (`scripts/log_retriever.py`, `scripts/log_parser.py`, `scripts/parsers/`)
    - `LogRetriever`: fetch job logs via GitHub API
    - `LogParserRouter`: try registered parsers in order, fall back to raw excerpt (last 200 lines) if none match
    - `GTestParser`: parse Google Test `[  FAILED  ]` patterns, extract test name, file, line, assertion
    - `TclTestParser`: parse Tcl `[err]:` patterns from runtest output
    - `BuildErrorParser`: parse gcc/clang `file:line:col: error:` patterns
    - `SentinelClusterParser`: parse sentinel/cluster test failure patterns
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 1.10 Write property tests for log parsers
    - **Property 3: Log parser extracts structured fields from all supported formats**
    - **Validates: Requirements 2.2, 2.3, 2.4**

  - [x] 1.11 Write property test for unparseable logs
    - **Property 4: Unparseable logs produce raw excerpt**
    - **Validates: Requirements 2.5**

  - [x] 1.12 Implement failure store (`scripts/failure_store.py`)
    - `FailureStore.compute_fingerprint()`: SHA-256 of (failure_identifier, error_signature, file_path)
    - `has_open_pr()`, `record()`, `mark_abandoned()`, `reconcile_pr_states()`, `load()`, `save()`: JSON file persistence on a dedicated branch
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_

  - [x] 1.13 Write property test for fingerprint determinism
    - **Property 19: Fingerprint determinism**
    - **Validates: Requirements 9.1**

  - [x] 1.14 Write property test for failure store serialization round-trip
    - **Property 20: Failure store serialization round-trip**
    - **Validates: Requirements 9.5**

  - [x] 1.15 Write property test for deduplication logic
    - **Property 2: Deduplication skips known failures and allows reprocessing of abandoned**
    - **Validates: Requirements 1.5, 9.2, 9.3, 9.4**

  - [x] 1.16 Implement pipeline orchestrator (`scripts/main.py`)
    - CLI entry point that wires Detect → Parse → (placeholder for Analyze → Fix → Validate → PR)
    - Accept workflow run ID and config path as arguments
    - Implement per-stage error handling: catch exceptions, record status, continue or abort per design's error table
    - Enforce `max_failures_per_run` limit with alphabetical job name ordering
    - _Requirements: 1.1, 1.2, 1.3, 10.2, 10.3_

  - [x] 1.17 Write property test for per-run failure processing limit
    - **Property 21: Per-run failure processing limit with ordering**
    - **Validates: Requirements 10.2, 10.3**

- [x] 2. Phase 1 Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Phase 2: Bedrock Integration
  - [x] 3.1 Implement Bedrock client (`scripts/bedrock_client.py`)
    - `BedrockClient.invoke()`: call Bedrock Converse API with configured model
    - Include project context (language, build system, test frameworks) in system prompt
    - Enforce input/output token limits from config
    - Implement exponential backoff with jitter for throttling errors (up to 3 retries)
    - Propagate non-retryable errors immediately
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

  - [x] 3.2 Write property test for Bedrock error handling
    - **Property 15: Bedrock error handling**
    - **Validates: Requirements 7.5, 7.6**

  - [x] 3.3 Write property test for system prompt project context
    - **Property 16: System prompt includes project context**
    - **Validates: Requirements 7.7**

  - [x] 3.4 Implement root cause analyzer (`scripts/root_cause_analyzer.py`)
    - `RootCauseAnalyzer.analyze()`: identify relevant source files, retrieve contents at commit SHA, send structured prompt to Bedrock, parse response into `RootCauseReport`
    - `identify_relevant_files()`: map test files to source files using error messages, stack traces, and configurable patterns
    - Handle Bedrock errors: mark as "analysis-failed" and stop processing
    - Detect flaky tests and label accordingly
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 3.5 Write property test for relevant file identification
    - **Property 6: Relevant file identification from failure data**
    - **Validates: Requirements 3.1**

  - [x] 3.6 Write property test for root cause analysis error propagation
    - **Property 7: Root cause analysis error propagation**
    - **Validates: Requirements 3.6**

  - [x] 3.7 Implement fix generator (`scripts/fix_generator.py`)
    - `FixGenerator.generate()`: send root cause + source files to Bedrock requesting unified diff
    - Validate patch applies cleanly with `git apply --check`
    - Retry up to `max_retries_fix` times on apply failure
    - Reject patches modifying more than `max_patch_files` files
    - Skip generation when confidence is "low"
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x] 3.8 Write property test for confidence gating
    - **Property 8: Confidence gating for fix generation**
    - **Validates: Requirements 4.1, 4.6**

  - [x] 3.9 Write property test for patch scope validation
    - **Property 9: Patch scope validation**
    - **Validates: Requirements 4.5**

  - [x] 3.10 Write property test for fix generation retry limit
    - **Property 10: Fix generation retry limit**
    - **Validates: Requirements 4.4**

  - [x] 3.11 Wire Bedrock components into pipeline orchestrator
    - Extend `scripts/main.py` to call root cause analyzer and fix generator after parsing
    - Pass config's project context through the pipeline
    - _Requirements: 3.3, 4.1_

- [x] 4. Phase 2 Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Phase 3: Validation and PR Creation
  - [x] 5.1 Implement validation runner (`scripts/validation_runner.py`)
    - `ValidationRunner.validate()`: check out consumer repo at target SHA, apply patch, build with matching config, run failing tests — all within the bot's own workflow environment
    - Map failing job matrix parameters to build flags (SANITIZER, BUILD_TLS, MALLOC, architecture) using ValidationProfile from config
    - Skip validation for untrusted fork failures (record as "untrusted-fork")
    - Return `ValidationResult` with pass/fail and output
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [x] 5.2 Write property test for validation build configuration mapping
    - **Property 11: Validation build configuration mapping**
    - **Validates: Requirements 5.2, 5.3**

  - [x] 5.3 Write property test for validation retry limit
    - **Property 12: Validation retry limit**
    - **Validates: Requirements 5.8**

  - [x] 5.4 Implement PR manager (`scripts/pr_manager.py`)
    - `PRManager.create_pr()`: create branch `bot/fix/<fingerprint>`, apply patch, commit with descriptive message, open PR with full context body, apply `bot-fix` label
    - Record PR in failure store
    - Skip PR creation for fork PR failures (no write access to fork) — log as "fork-pr-no-write-access"
    - Handle GitHub API rejections gracefully
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

  - [x] 5.5 Write property test for PR content completeness
    - **Property 13: PR content completeness**
    - **Validates: Requirements 6.1, 6.2, 6.4, 6.5**

  - [x] 5.6 Write property test for PR creation records in failure store
    - **Property 14: PR creation records in failure store**
    - **Validates: Requirements 6.6**

  - [x] 5.7 Implement rate limiting and safety limits
    - Daily PR limit tracking (`max_prs_per_day`): queue excess failures
    - Open bot PR limit (`max_open_bot_prs`): pause new PR creation when exceeded
    - Daily token budget tracking: stop Bedrock calls when exhausted
    - Integrate limits into pipeline orchestrator
    - _Requirements: 10.1, 10.4, 10.5_

  - [x] 5.8 Write property test for daily PR rate limit
    - **Property 22: Daily PR rate limit**
    - **Validates: Requirements 10.1**

  - [x] 5.9 Write property test for open bot PR limit
    - **Property 23: Open bot PR limit**
    - **Validates: Requirements 10.5**

  - [x] 5.10 Write property test for token budget enforcement
    - **Property 24: Token budget enforcement**
    - **Validates: Requirements 10.4**

  - [x] 5.11 Write property test for queued failure reconciliation drain
    - **Property 26: Queued failures are drained by reconciliation runs**
    - **Validates: Requirements 10.1, 10.6**

  - [x] 5.12 Write property test for untrusted fork trust gating
    - **Property 27: Untrusted fork failures never execute privileged stages**
    - **Validates: Requirements 1.6, 5.5, 6.3**

  - [x] 5.13 Wire validation, PR creation, and rate limiting into pipeline orchestrator
    - Extend `scripts/main.py` with the full pipeline: Detect → Parse → Analyze → Fix → Validate → PR
    - Implement validation-failure retry loop (retry fix generation with validation output, up to `max_validation_retries`)
    - Implement scheduled reconciliation drain: process queued failures when rate limits reset
    - _Requirements: 5.8, 6.1, 10.6_

- [x] 6. Phase 3 Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Phase 4: GitHub Actions Workflows
  - [x] 7.1 Create the reusable analysis workflow (`.github/workflows/analyze-failure.yml`)
    - Define workflow inputs: `config_path`, secrets for AWS credentials and GitHub token
    - Set up Python 3.11, install dependencies, run `scripts/main.py`
    - Pass workflow run context (run ID, repo, SHA) to the script
    - _Requirements: 1.1, 7.1_

  - [x] 7.2 Create the reusable validation workflow (`.github/workflows/validate-fix.yml`)
    - Define inputs: consumer repo, commit SHA, patch content, build config, test command
    - Check out consumer repo at SHA, apply patch, build, run tests
    - Report results back as workflow outputs
    - _Requirements: 5.1, 5.2, 5.3_

  - [x] 7.3 Create the bot repo CI workflow (`.github/workflows/ci.yml`)
    - Run pytest, mypy, and linting on the bot repo's own code
    - _Requirements: (internal quality)_

  - [x] 7.4 Create example caller workflow template and config template
    - Provide a documented example `.github/workflows/ci-failure-bot.yml` caller workflow for consumer repos
    - Include note about `workflow_run` trigger using display names vs filenames
    - Provide example `.github/ci-failure-bot.yml` config file
    - _Requirements: 8.1, 8.2, 1.2_

- [x] 8. Phase 4 Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Phase 5: Observability
  - [x] 9.1 Implement structured logging throughout the pipeline
    - Add timestamped logging at each processing step (detection, parsing, analysis, generation, validation, PR creation)
    - Log skip reasons with failure fingerprints (duplicate, rate limit, low confidence, unparseable)
    - _Requirements: 11.1, 11.3_

  - [x] 9.2 Implement GitHub Actions workflow summary
    - Emit a job summary at end of each run listing all failures processed, outcomes, and errors
    - _Requirements: 11.4_

  - [x] 9.3 Write property test for workflow summary completeness
    - **Property 25: Workflow summary completeness**
    - **Validates: Requirements 11.4**

  - [x] 9.4 Implement PR summary comments
    - Add a comment on each created PR listing processing steps, time taken, and retries
    - _Requirements: 11.2_

- [x] 10. Final Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each phase is independently testable with mocked external services
- Property tests use Hypothesis with `@settings(max_examples=100)`
- Phase 1 is fully testable with mocked GitHub API responses (no real API calls)
- Phase 2 is testable with mocked Bedrock responses
- The failure store uses a JSON file on a dedicated branch (not artifacts) for reliable persistence
- The validation runner operates within the bot's own workflow, checking out consumer repo code — it does not dispatch workflows to the consumer repo
- Fork PR failures are skipped for PR creation (bot has no write access to forks)

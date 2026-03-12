# Requirements Document

## Introduction

This feature is an open-source GitHub bot that monitors CI test failures in the Valkey repository, analyzes their root causes using Amazon Bedrock as the LLM backend, and automatically raises pull requests with proposed fixes. The bot operates as a GitHub Actions workflow triggered by CI failure events, and interacts with the repository through the GitHub API.

The Valkey CI suite includes unit tests (Google Test), Tcl-based integration tests (`runtest`), module API tests (`runtest-moduleapi`), sentinel tests (`runtest-sentinel`), cluster tests (`runtest-cluster`), and build-only jobs across multiple platforms (Ubuntu, macOS, Debian, AlmaLinux, 32-bit, sanitizers, TLS, RDMA). The bot must parse failure logs from any of these test types, identify the relevant source files, generate a fix, validate it, and submit a PR for human review.

## Glossary

- **Bot**: The CI failure bot system, implemented as a set of GitHub Actions workflows and supporting scripts.
- **CI_Run**: A GitHub Actions workflow run triggered by a push or pull request on the Valkey repository.
- **Failure_Event**: A completed CI_Run where one or more jobs have a failed status.
- **Failure_Log**: The raw log output from a failed GitHub Actions job step, retrieved via the GitHub API.
- **Log_Parser**: The component that extracts structured failure information (failure identifier, error message, file, line number, stack trace) from a Failure_Log.
- **Root_Cause_Analyzer**: The component that uses Amazon Bedrock to analyze parsed failure data and relevant source code to determine the likely root cause.
- **Fix_Generator**: The component that uses Amazon Bedrock to produce a code patch that addresses the identified root cause.
- **Bedrock_Client**: The component that communicates with the Amazon Bedrock API to invoke foundation models for analysis and code generation.
- **PR_Manager**: The component that creates branches, commits patches, and opens pull requests via the GitHub API.
- **Failure_Store**: A persistent record (JSON file committed to a dedicated branch in the consumer repository, or GitHub Actions artifacts as fallback) of previously seen failures to avoid duplicate processing.
- **Validation_Runner**: The component that reruns the specific failing test(s), or the equivalent build validation for build-only failures, against the proposed fix before submitting a PR.

## Requirements

### Requirement 1: Monitor CI Failures

**User Story:** As a Valkey maintainer, I want the bot to automatically detect CI test failures, so that failures are addressed without manual triage.

#### Acceptance Criteria

1. WHEN a CI_Run completes with one or more failed jobs, THE Bot SHALL detect the Failure_Event via a GitHub Actions `workflow_run` trigger with the `completed` conclusion.
2. THE Bot SHALL monitor failures from all CI workflow files in the repository (`ci.yml`, `daily.yml`, `weekly.yml`, `external.yml`).
3. WHEN a Failure_Event is detected, THE Bot SHALL retrieve the list of failed jobs and their step names from the CI_Run using the GitHub Actions API.
4. THE Bot SHALL ignore CI_Run failures that are caused by infrastructure issues (runner timeouts, network errors, GitHub API rate limits) rather than test or build failures.
5. WHEN a Failure_Event matches a failure already recorded in the Failure_Store with an open or merged PR, THE Bot SHALL skip processing and log the duplicate detection.
6. THE Bot SHALL classify each Failure_Event as trusted same-repository work or untrusted fork-originated work before invoking privileged stages. Untrusted fork-originated failures SHALL be skipped before Bedrock-backed analysis/fix generation, validation, or PR creation and logged as "untrusted-fork".

### Requirement 2: Retrieve and Parse Failure Logs

**User Story:** As a Valkey maintainer, I want the bot to extract structured failure information from CI logs, so that the root cause analysis has clean input data.

#### Acceptance Criteria

1. WHEN a failed job is identified, THE Log_Parser SHALL retrieve the full job log via the GitHub Actions API (`GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs`).
2. THE Log_Parser SHALL extract the following fields from each failure: a stable failure identifier (test name for test failures, or a build-scoped identifier for build failures), file path, error message, assertion details, line number, and stack trace (when available).
3. THE Log_Parser SHALL support parsing failure output from Google Test (unit tests), Tcl test framework (`runtest` output), and build errors (compiler warnings/errors with `-Werror`).
4. THE Log_Parser SHALL support parsing failure output from sentinel tests (`runtest-sentinel`), cluster tests (`runtest-cluster`), and module API tests (`runtest-moduleapi`).
5. IF the Log_Parser cannot extract structured failure information from a Failure_Log, THEN THE Log_Parser SHALL record the raw log excerpt (last 200 lines of the failed step) and flag the failure as "unparseable" for manual review.
6. THE Log_Parser SHALL produce a structured Failure_Report containing: workflow name, job name, job matrix parameters (OS, build flags), parsed failure fields, and the commit SHA that triggered the CI_Run.

### Requirement 3: Analyze Root Cause

**User Story:** As a Valkey maintainer, I want the bot to identify the likely root cause of a test failure, so that the generated fix targets the correct code.

#### Acceptance Criteria

1. WHEN a Failure_Report is produced, THE Root_Cause_Analyzer SHALL identify the source files relevant to the failure by examining file paths, error messages, and stack traces.
2. THE Root_Cause_Analyzer SHALL retrieve the content of relevant source files from the repository at the commit SHA that triggered the failing CI_Run.
3. THE Root_Cause_Analyzer SHALL send the Failure_Report and relevant source code to the Bedrock_Client for analysis, using a structured prompt that includes the failure context, source code, and instructions to identify the root cause.
4. THE Root_Cause_Analyzer SHALL produce a Root_Cause_Report containing: the identified root cause description, the list of files that need changes, the confidence level (high, medium, low), and a concise rationale.
5. IF the Root_Cause_Analyzer determines the failure is a flaky test (non-deterministic, timing-dependent), THEN THE Root_Cause_Analyzer SHALL label the Root_Cause_Report as "flaky" and include the flakiness indicators.
6. IF the Bedrock_Client returns an error or the model response is not parseable, THEN THE Root_Cause_Analyzer SHALL record the failure as "analysis-failed" and stop processing for that failure.

### Requirement 4: Generate Fix

**User Story:** As a Valkey maintainer, I want the bot to generate a code patch that fixes the identified root cause, so that I can review and merge a ready-made solution.

#### Acceptance Criteria

1. WHEN a Root_Cause_Report with confidence level "high" or "medium" is produced, THE Fix_Generator SHALL send the Root_Cause_Report and relevant source files to the Bedrock_Client with a structured prompt requesting a unified diff patch.
2. THE Fix_Generator SHALL produce a patch in unified diff format that can be applied with `git apply`.
3. THE Fix_Generator SHALL validate that the generated patch applies cleanly to the repository at the target commit SHA.
4. IF the generated patch does not apply cleanly, THEN THE Fix_Generator SHALL retry generation up to 2 additional times with feedback about the apply failure before marking the fix as "generation-failed".
5. THE Fix_Generator SHALL limit the scope of generated patches to the files identified in the Root_Cause_Report. The Fix_Generator SHALL reject patches that modify more than 10 files.
6. WHEN the Root_Cause_Report confidence level is "low", THE Fix_Generator SHALL skip fix generation and record the failure for manual review.

### Requirement 5: Validate Fix

**User Story:** As a Valkey maintainer, I want the bot to verify that its proposed fix actually resolves the failing test, so that I receive high-quality PRs.

#### Acceptance Criteria

1. WHEN a patch is generated for a trusted same-repository failure, THE Validation_Runner SHALL apply the patch to a clean checkout of the repository at the target commit SHA.
2. THE Validation_Runner SHALL build the project using the same build configuration as the failing CI job (matching compiler flags, build options like `BUILD_TLS`, `SANITIZER`, `MALLOC`, and architecture).
3. THE Validation_Runner SHALL derive its build and test commands from repository configuration that maps CI job names and matrix parameters to validation commands.
4. THE Validation_Runner SHALL run the specific failing test(s) identified in the Failure_Report against the patched build, or rerun the equivalent build validation when the failure is build-only.
5. IF the failure originated from an untrusted fork pull request, THEN THE Validation_Runner SHALL NOT check out or execute the fork's head commit in a secrets-bearing context and SHALL record the failure as "untrusted-fork".
6. WHEN all previously failing tests pass with the patch applied, THE Validation_Runner SHALL mark the fix as "validated".
7. IF the build fails or any of the previously failing tests still fail after applying the patch, THEN THE Validation_Runner SHALL mark the fix as "validation-failed" and include the new failure output.
8. IF validation fails, THEN THE Fix_Generator SHALL retry fix generation up to 1 additional time with the validation failure output as additional context before abandoning the fix.

### Requirement 6: Create Pull Request

**User Story:** As a Valkey maintainer, I want the bot to submit validated fixes as pull requests, so that I can review and merge them through the normal contribution workflow.

#### Acceptance Criteria

1. WHEN a fix is marked as "validated", THE PR_Manager SHALL create a new branch named `bot/fix/<failure-identifier>` from the target commit SHA.
2. THE PR_Manager SHALL apply the validated patch to the branch and create a commit with a descriptive message that includes the stable failure identifier (test name when available), job name, and a summary of the root cause.
3. THE PR_Manager SHALL open a pull request against the branch that the original CI_Run was targeting (e.g., `unstable`). **Note:** The bot SHALL only create PRs for failures on branches where it has write access (typically the main repository's branches such as `unstable`). Failures originating from fork PRs cannot have bot-created fix PRs pushed to the fork, and the bot SHALL skip PR creation for such failures, logging the reason as "fork-pr-no-write-access".
4. THE PR_Manager SHALL include in the PR body: a link to the failing CI_Run, the parsed failure summary, the root cause analysis, the confidence level, and a disclaimer that the fix was generated by an AI bot and requires human review.
5. THE PR_Manager SHALL apply the label `bot-fix` to the created pull request.
6. THE PR_Manager SHALL record the PR URL and failure identifier in the Failure_Store to prevent duplicate processing.
7. IF the GitHub API rejects the PR creation (e.g., branch protection, permissions), THEN THE PR_Manager SHALL log the error and record the failure as "pr-creation-failed".

### Requirement 7: Amazon Bedrock Integration

**User Story:** As a Valkey maintainer, I want the bot to use Amazon Bedrock for LLM capabilities, so that the analysis and fix generation leverage managed AI infrastructure.

#### Acceptance Criteria

1. THE Bedrock_Client SHALL authenticate with Amazon Bedrock using IAM credentials provided via GitHub Actions secrets (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`).
2. THE Bedrock_Client SHALL invoke the configured foundation model via the Bedrock `InvokeModel` or `Converse` API.
3. THE Bedrock_Client SHALL support configuring the model ID (e.g., `anthropic.claude-sonnet-4-20250514`, `amazon.nova-pro-v1:0`) via a repository-level configuration file.
4. THE Bedrock_Client SHALL enforce a maximum token limit for both input and output to control costs, with configurable limits in the configuration file.
5. IF a Bedrock API call fails with a throttling error, THEN THE Bedrock_Client SHALL retry with exponential backoff up to 3 times before reporting the error.
6. IF a Bedrock API call fails with a non-retryable error, THEN THE Bedrock_Client SHALL log the error details and propagate the failure to the calling component.
7. THE Bedrock_Client SHALL include the Valkey project context (C codebase, CMake build system, test frameworks used) in the system prompt for all model invocations.

### Requirement 8: Configuration

**User Story:** As a Valkey maintainer, I want to configure the bot's behavior through a repository file, so that settings can be reviewed and changed through normal PR workflows.

#### Acceptance Criteria

1. THE Bot SHALL read its configuration from a YAML file at `.github/ci-failure-bot.yml` in the repository root.
2. THE configuration file SHALL support the following settings: Bedrock model ID, maximum input/output tokens, maximum patch file count, confidence threshold for fix generation, list of monitored workflow files, retry limits, and validation profiles that map CI job names and matrix parameters to build/test commands.
3. WHEN the configuration file is missing, THE Bot SHALL use default values for all settings.
4. IF the configuration file contains invalid YAML or unrecognized fields, THEN THE Bot SHALL log a warning and fall back to default values for the invalid fields.

### Requirement 9: Failure Deduplication and Tracking

**User Story:** As a Valkey maintainer, I want the bot to avoid creating duplicate PRs for the same failure, so that the repository is not flooded with redundant fix attempts.

#### Acceptance Criteria

1. THE Failure_Store SHALL maintain a record of each processed failure keyed by a fingerprint derived from: a stable failure identifier (test name for test failures, or a build-scoped identifier for build failures), the error message signature, and the failing file path.
2. WHEN a new Failure_Event is detected, THE Bot SHALL compute the failure fingerprint and check the Failure_Store for an existing entry.
3. WHILE a failure fingerprint has an associated open PR, THE Bot SHALL skip processing new occurrences of the same failure.
4. WHEN a PR associated with a failure fingerprint is closed without merging, THE Bot SHALL mark the Failure_Store entry as "abandoned" and allow reprocessing on the next occurrence.
5. THE Failure_Store SHALL be persisted as a JSON file committed to a dedicated branch in the consumer repository (preferred for MVP) or stored as a GitHub Actions artifact (fallback, subject to retention limits), surviving across workflow runs.
6. THE Bot SHALL reconcile Failure_Store entries against pull request state on pull request close events and on scheduled reconciliation runs, marking entries as "merged" or "abandoned" as appropriate.

### Requirement 10: Rate Limiting and Safety

**User Story:** As a Valkey maintainer, I want the bot to have safety limits, so that it cannot flood the repository with PRs or consume excessive LLM resources.

#### Acceptance Criteria

1. THE Bot SHALL create no more than 5 pull requests per 24-hour period. WHEN the limit is reached, THE Bot SHALL queue remaining failures for the next period.
2. THE Bot SHALL process no more than 10 failures per CI_Run to avoid excessive API usage on catastrophic CI breakages.
3. WHEN a CI_Run has more than 10 failed jobs, THE Bot SHALL process the first 10 failures (ordered by job name) and log the remaining as "skipped-rate-limit".
4. THE Bot SHALL track cumulative Bedrock API token usage per 24-hour period and stop processing new failures when a configurable token budget is exhausted.
5. IF the Bot detects that the `unstable` branch has more than 3 open bot-generated PRs, THEN THE Bot SHALL pause all new PR creation until existing PRs are resolved.
6. THE Bot SHALL include a scheduled reconciliation run that drains queued failures after rate limits reset, even if no new CI failure occurs.

### Requirement 11: Logging and Observability

**User Story:** As a Valkey maintainer, I want visibility into the bot's actions and decisions, so that I can debug issues and understand its behavior.

#### Acceptance Criteria

1. THE Bot SHALL log each processing step (failure detection, log parsing, root cause analysis, fix generation, validation, PR creation) with timestamps and outcome status to the GitHub Actions workflow log.
2. THE Bot SHALL produce a summary comment on each created PR listing the processing steps, time taken for each step, and any retries that occurred.
3. WHEN the Bot skips a failure (duplicate, rate limit, low confidence, unparseable, untrusted-fork), THE Bot SHALL log the skip reason and the failure fingerprint.
4. THE Bot SHALL emit a workflow summary (GitHub Actions job summary) at the end of each run listing all failures processed, their outcomes, and any errors encountered.

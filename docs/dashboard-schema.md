# Dashboard JSON Schema (v1)

The `dashboard.json` file is the contract between the Python data builder
(`scripts/agent_dashboard.py`) and the static JavaScript frontend
(`dashboard-app/`). The frontend builds against fixture files that conform to
this schema — never against live Python output directly.

## Top-level fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `schema_version` | int | ✅ | — | Always `1` for this version |
| `generated_at` | string | ✅ | — | ISO 8601 timestamp of generation |
| `snapshot` | object | ✅ | — | Summary counters for the sidebar |
| `ci_failures` | object | ✅ | — | CI failure incident data |
| `flaky_tests` | object | ✅ | — | Flaky test campaign data |
| `pr_reviews` | object | ✅ | — | PR review tracking state |
| `acceptance` | object | ✅ | — | Replay acceptance scorecard |
| `fuzzer` | object | ✅ | — | Fuzzer analysis results |
| `agent_outcomes` | object | ✅ | — | Event ledger metrics |
| `ai_reliability` | object | ✅ | — | AI/Bedrock reliability counters |
| `state_health` | object | ✅ | — | Monitor watermarks and warnings |
| `trends` | object | ✅ | — | 7-day trend series |
| `daily_health` | object | ✅ | — | Daily CI health report data |
| `wow_trends` | object | ✅ | — | Week-over-week comparison |

---

## Page mapping

- **Daily CI**: `daily_health`, `wow_trends`, `flaky_tests`, `ci_failures`
- **PRs**: `pr_reviews`, `acceptance`
- **Fuzzer**: `fuzzer`
- **Diagnostics**: `ci_failures`, `agent_outcomes`, `state_health`, `ai_reliability`
- **Cross-cutting**: `snapshot`, `trends`, `generated_at`, `schema_version`

---

## `snapshot`

Summary counters shown in the sidebar and overview cards.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `failure_incidents` | int | `0` | Total failure store entries |
| `queued_failures` | int | `0` | Failures awaiting next action |
| `active_flaky_campaigns` | int | `0` | Non-terminal campaigns |
| `tracked_review_prs` | int | `0` | PRs with durable review state |
| `review_comments` | int | `0` | Persisted review comment IDs |
| `fuzzer_runs_analyzed` | int | `0` | Fuzzer runs with classifier output |
| `fuzzer_anomalous_runs` | int | `0` | Non-normal analyzed runs |
| `daily_runs_seen` | int | `0` | Daily monitor run observations |
| `ai_token_usage` | int | `0` | Bedrock token consumption |
| `agent_events` | int | `0` | Total event ledger entries |
| `instrumentation_gaps` | int | `0` | Missing AI instrumentation |

## `ci_failures`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `failure_incidents` | int | `0` | Total entries |
| `entry_status_counts` | object | `{}` | `{status: count}` |
| `history_entries` | int | `0` | Failure history entries |
| `history_observations` | int | `0` | Total observations |
| `history_failures` | int | `0` | Fail observations |
| `history_passes` | int | `0` | Pass observations |
| `queued_failures` | int | `0` | Queued count |
| `queued_failure_fingerprints` | array | `[]` | Up to 20 fingerprints |
| `daily_result_files` | int | `0` | Result file count |
| `daily_runs_seen` | int | `0` | Runs observed |
| `daily_action_counts` | object | `{}` | `{action: count}` |
| `daily_conclusion_counts` | object | `{}` | `{conclusion: count}` |
| `daily_job_outcome_counts` | object | `{}` | `{outcome: count}` |
| `recent_incidents` | array | `[]` | Up to 10 recent entries |

### `recent_incidents[]` item

| Field | Type | Description |
|-------|------|-------------|
| `failure_identifier` | string | Failure name |
| `status` | string | Current status |
| `file_path` | string | Affected file |
| `pr_url` | string | Associated PR URL |
| `updated_at` | string | ISO timestamp |

## `flaky_tests`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `campaigns` | int | `0` | Total campaigns |
| `active_campaigns` | int | `0` | Non-terminal campaigns |
| `status_counts` | object | `{}` | `{status: count}` |
| `proof_counts` | object | `{}` | `{proof_status: count}` |
| `subsystem_counts` | object | `{}` | `{subsystem: count}` |
| `total_attempts` | int | `0` | Sum of all attempts |
| `failed_hypotheses` | int | `0` | Sum of failed hypotheses |
| `consecutive_full_passes` | int | `0` | Sum of pass streaks |
| `recent_campaigns` | array | `[]` | Up to 12 recent campaigns |

### `recent_campaigns[]` item

| Field | Type | Description |
|-------|------|-------------|
| `failure_identifier` | string | Failure name |
| `subsystem` | string | Inferred subsystem |
| `status` | string | Campaign status |
| `proof_status` | string | Proof status |
| `proof_url` | string | Proof run URL |
| `pr_url` | string | Associated PR URL |
| `job_name` | string | CI job name |
| `branch` | string | Target branch |
| `total_attempts` | int | Attempt count |
| `consecutive_full_passes` | int | Pass streak |
| `queued_pr_payload` | object\|null | Queued PR data if present |
| `updated_at` | string | ISO timestamp |

## `pr_reviews`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tracked_prs` | int | `0` | PRs with review state |
| `summary_comments` | int | `0` | Summary comment count |
| `review_comments` | int | `0` | Review comment count |
| `recent_reviews` | array | `[]` | Up to 10 recent reviews |
| `acceptance_cases` | int | `0` | Acceptance case count |
| `acceptance_passed` | int | `0` | Passed cases |
| `acceptance_failed` | int | `0` | Failed cases |
| `acceptance_findings` | int | `0` | Total findings |
| `coverage_incomplete_cases` | int | `0` | Incomplete coverage |
| `model_followup_counts` | object | `{}` | `{followup: count}` |

### `recent_reviews[]` item

| Field | Type | Description |
|-------|------|-------------|
| `repo` | string | Repository full name |
| `pr_number` | int | PR number |
| `last_reviewed_head_sha` | string | Reviewed commit SHA |
| `summary_comment_id` | int\|null | Summary comment ID |
| `review_comment_ids` | array | Comment ID list |
| `updated_at` | string | ISO timestamp |

## `acceptance`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `payloads_seen` | int | `0` | Payload count |
| `readiness` | string | `"unknown"` | Readiness verdict |
| `review_cases` | int | `0` | Review case count |
| `review_passed` | int | `0` | Passed review cases |
| `review_failed` | int | `0` | Failed review cases |
| `workflow_cases` | int | `0` | Workflow case count |
| `workflow_passed` | int | `0` | Passed workflow cases |
| `workflow_failed` | int | `0` | Failed workflow cases |
| `finding_count` | int | `0` | Total findings |
| `recent_review_results` | array | `[]` | Up to 12 review results |
| `recent_workflow_results` | array | `[]` | Up to 12 workflow results |

## `fuzzer`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `result_files` | int | `0` | Result file count |
| `runs_seen` | int | `0` | Total runs |
| `runs_analyzed` | int | `0` | Analyzed runs |
| `status_counts` | object | `{}` | `{status: count}` |
| `scenario_counts` | object | `{}` | `{scenario: count}` |
| `root_cause_counts` | object | `{}` | `{category: count}` |
| `issue_action_counts` | object | `{}` | `{action: count}` |
| `raw_log_fallbacks` | int | `0` | Raw log fallback count |
| `recent_anomalies` | array | `[]` | Up to 10 anomalies |

### `recent_anomalies[]` item

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | string | Run identifier |
| `run_url` | string | GitHub run URL |
| `status` | string | Analysis status |
| `triage_verdict` | string | Triage result |
| `scenario_id` | string | Scenario identifier |
| `seed` | string | Fuzzer seed |
| `root_cause_category` | string | Root cause bucket |
| `summary` | string | Analysis summary |
| `issue_url` | string | GitHub issue URL |
| `issue_action` | string | Issue action taken |

## `agent_outcomes`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `events` | int | `0` | Total events |
| `event_type_counts` | object | `{}` | `{type: count}` |
| `subjects` | int | `0` | Unique subjects |
| `validation_passed` | int | `0` | Passed validations |
| `validation_failed` | int | `0` | Failed validations |
| `proof_dispatched` | int | `0` | Proof campaigns started |
| `proof_passed` | int | `0` | Proof passed |
| `proof_failed` | int | `0` | Proof failed |
| `prs_created` | int | `0` | PRs opened |
| `prs_merged` | int | `0` | PRs merged |
| `prs_closed_without_merge` | int | `0` | PRs closed unmerged |
| `dead_lettered` | int | `0` | Dead-lettered fixes |
| `recent_events` | array | `[]` | Up to 15 recent events |

### `recent_events[]` item

| Field | Type | Description |
|-------|------|-------------|
| `created_at` | string | ISO timestamp |
| `event_type` | string | Event type |
| `subject` | string | Event subject |
| `attributes` | object | Event metadata |

## `ai_reliability`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `token_usage` | int | `0` | Total tokens consumed |
| `token_window_start` | string | `"unknown"` | Window start |
| `ai_metrics` | object | `{}` | Raw AI event counters |
| `schema_calls` | int | `0` | Schema invoke calls |
| `schema_successes` | int | `0` | Schema successes |
| `tool_loop_calls` | int | `0` | Tool loop calls |
| `terminal_validation_rejections` | int | `0` | Terminal rejections |
| `bedrock_retries` | int | `0` | Retry count |
| `retry_exhaustions` | int | `0` | Exhausted retries |
| `prompt_safety_checked` | int | `0` | Safety checks |
| `prompt_safety_present` | int | `0` | Guards present |
| `prompt_safety_missing` | int | `0` | Guards missing |
| `prompt_safety_coverage` | float | `0.0` | Coverage ratio |
| `instrumentation_gaps` | array | `[]` | Gap descriptions |

## `state_health`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `monitor_watermarks` | int | `0` | Watermark count |
| `recent_watermarks` | array | `[]` | Up to 10 watermarks |
| `input_warnings` | array | `[]` | Loader warning strings |

### `recent_watermarks[]` item

| Field | Type | Description |
|-------|------|-------------|
| `key` | string | Watermark key |
| `last_seen_run_id` | string | Last run ID |
| `target_repo` | string | Target repository |
| `workflow_file` | string | Workflow filename |
| `updated_at` | string | ISO timestamp |

## `trends`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `labels` | array | `[]` | Date labels (MM-DD) |
| `window_days` | int | `7` | Trend window size |
| `failure_rate` | object | — | Failure rate series |
| `review_health` | object | — | Review health series |
| `flaky_subsystems` | object | — | Subsystem pressure |

### `trends.failure_rate`

| Field | Type | Description |
|-------|------|-------------|
| `rates` | array | Float rates per day |
| `average_rate` | float | Average failure rate |

### `trends.review_health`

| Field | Type | Description |
|-------|------|-------------|
| `scores` | array | Health scores per day |
| `degraded_reviews` | array | Degraded count per day |
| `average_score` | float | Average health score |

### `trends.flaky_subsystems`

| Field | Type | Description |
|-------|------|-------------|
| `top_subsystems` | array | Top 3 subsystem names |
| `series` | object | `{name: [counts]}` per day |

## `daily_health`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `repo` | string | `""` | Repository full name |
| `workflow` | string | `""` | Workflow file(s) |
| `branch` | string | `""` | Target branch |
| `dates` | array | `[]` | Date strings (YYYY-MM-DD) |
| `total_runs` | int | `0` | Total monitored runs |
| `failed_runs` | int | `0` | Failed runs |
| `unique_failures` | int | `0` | Distinct failure names |
| `days_with_runs` | int | `0` | Dates with run data |
| `workflows` | array | `[]` | Workflow file list |
| `workflow_reports` | array | `[]` | Per-workflow reports |
| `heatmap` | array | `[]` | Failure heatmap rows |
| `runs` | array | `[]` | Detailed run records |
| `tests` | object | `{}` | Per-failure timeline |
| `failure_jobs` | object | `{}` | Per-failure job IDs |

### `daily_health.heatmap[]` item

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Failure name |
| `days_failed` | int | Days with failures |
| `total_days` | int | Total tracked days |
| `cells` | array | Per-date cell data |

### `daily_health.heatmap[].cells[]` item

| Field | Type | Description |
|-------|------|-------------|
| `date` | string | Date (YYYY-MM-DD) |
| `count` | int | Failure count |
| `has_run` | bool | Whether run data exists |
| `job_ids` | array | Job IDs for linking |
| `run_id` | string | Run ID for linking |

### `daily_health.runs[]` item

| Field | Type | Description |
|-------|------|-------------|
| `date` | string | Date (YYYY-MM-DD) |
| `workflow` | string | Workflow filename |
| `status` | string | Run conclusion |
| `commit_sha` | string | Short SHA |
| `full_sha` | string | Full commit SHA |
| `commit_message` | string | Commit message |
| `unique_failures` | int | Failure count |
| `failed_jobs` | int | Failed job count |
| `failed_job_names` | array | Job name list |
| `run_id` | string | Run identifier |
| `run_url` | string | GitHub run URL |
| `commits_since_prev` | array | Commits since last run |

## `wow_trends`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `has_data` | bool | `false` | Whether comparison data exists |
| `this_week` | object | `{}` | Current week stats |
| `last_week` | object | `{}` | Previous week stats |
| `delta` | int | `0` | Failure hit delta |
| `pct_change` | float | `0.0` | Percentage change |
| `new_failures` | array | `[]` | New this week |
| `resolved_failures` | array | `[]` | Resolved this week |
| `top_movers` | array | `[]` | Biggest changes |

### `wow_trends.this_week` / `last_week`

| Field | Type | Description |
|-------|------|-------------|
| `total_failure_hits` | int | Total failure occurrences |
| `unique_failures` | int | Distinct failure names |
| `failed_runs` | int | Failed run count |
| `total_runs` | int | Total run count |

### `wow_trends.top_movers[]` item

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Failure name |
| `this_week` | int | This week count |
| `last_week` | int | Last week count |
| `change` | int | Delta |

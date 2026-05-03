# Evaluation Framework

The eval framework measures CI agent quality against historical maintainer
decisions on public GitHub ground truth. It scores agent output for the four
production flows — PR review, daily-CI fix, backport, and fuzzer triage —
against what maintainers actually merged, commented, or rejected.

## Layout

```
scripts/eval/
├── __init__.py
├── eval_fixtures.py    # EvalFixture dataclass + load_fixtures()
├── eval_scorer.py      # Path-level precision/recall + forensic style scorer
├── flow_scorer.py      # Per-flow F1 scorers (strict + loose) for review/daily/backport/fuzzer
├── report.py           # Markdown report generator
└── review_runner.py    # Local CLI runner (invokes claude_reviewer directly)

eval/
├── fixtures/           # One JSON per fixture (30 review, 5 backport, 5 daily, 1 pr-117)
│   ├── review-3591.json
│   └── ...
└── RESULTS.md          # Latest run summary
```

## Fixture format

Each fixture is a JSON file loaded by `load_fixtures()` into an `EvalFixture`.
The required fields are `name`, `repo`, `flow`; everything else is per-flow.

### Review fixture

```json
{
  "name": "review-3591",
  "flow": "review",
  "repo": "valkey-io/valkey",
  "pr_number": 3591,
  "description": "Handle NULL pointer in streamTrim",
  "base_ref": "unstable",
  "head_sha": "…",
  "changed_files": ["src/t_stream.c"],
  "ground_truth": {
    "maintainer_comments": [
      {"author": "enjoy-binbin", "path": "src/t_stream.c", "line": 829, "body": "…"}
    ],
    "additions": 15,
    "deletions": 3
  },
  "tags": ["review", "simple"]
}
```

### Backport fixture

`ground_truth.files_changed` is the source-of-truth file set. Optional
`had_conflicts` and `was_rejected` record whether the human backport needed
manual conflict resolution.

### Daily fixture

`ground_truth.fix_files_changed` (list) plus `ground_truth.is_flaky` (bool).

### Fuzzer fixture

`ground_truth.root_cause_category` (string) plus `fix_files_changed`.

## Scoring

### Review — strict F1 (`flow_scorer.score_review_flow`)

Matches each agent finding to an unused maintainer comment by `(path, line)`
within `line_tolerance` (default **5**; the live runner passes **10**). One
match per maintainer comment. Returns F1 of precision and recall over those
matches. Use when the agent must hit the exact issue the maintainer flagged.

### Review — loose F1 (`flow_scorer.score_review_flow_loose`)

Two-pass matcher with `line_tolerance=10`:

1. **Exact match** — same `path` and line within tolerance → weight **1.0**.
2. **File-only match** — same `path`, line outside tolerance → weight **0.5**
   (counts as half a TP in the numerator and half an unmatched item in each
   denominator).

Loose F1 ≥ strict F1 on the same inputs. It rewards "found a real issue in
the right file, off by more than 10 lines."

### Review — style (`eval_scorer.score_style`)

Regex detector over finding bodies. Forensic patterns scanned:

- `wc -l`
- `git cat-file`
- `the diff shows`
- `I ran`
- `<N> bytes on disk`
- `diff +<N>/-<N>`

Returns `1.0 − (findings_with_any_leak / total_findings)`. Target is **1.0**
(zero forensic leakage). `1.0` for empty finding lists.

### Daily (`score_daily_flow`)

```
jaccard        = |agent.files_to_change ∩ ground_truth.fix_files_changed|
                 ÷ |agent ∪ ground_truth|
flaky_match    = agent.is_flaky == ground_truth.is_flaky
correctness    = 0.7 * jaccard + (0.3 if flaky_match else 0.0)
```

Max is 1.0 (perfect file set + flaky agreement). Empty ground-truth file set
returns 0.5 with a note.

### Backport (`score_backport_flow`)

```
file_match = |agent ∩ truth| / |truth|              # recall over truth set
```

**Zero-byte guard.** If any entry in `agent_result.file_details` has
`size == 0`, the score is forced to **0.0**. This guards against the PR #117
regression where the backporter produced empty files (see
`eval/fixtures/pr-117-empty-files.json`). Style is always 1.0.

### Fuzzer (`score_fuzzer_flow`)

```
cat_match      = ac == tc  or  tc in ac  or  ac in tc    # substring-tolerant
file_overlap   = |agent.files_involved ∩ truth.fix_files_changed|
                 ÷ |truth.fix_files_changed|      (0.5 if truth empty)
correctness    = (0.5 if cat_match else 0.0) + 0.5 * file_overlap
```

## Running a single fixture locally

```bash
export GITHUB_TOKEN=$(gh auth token)
export AWS_REGION=us-east-1
# Requires Bedrock permissions for Claude Code
python -m scripts.eval.review_runner \
  --fixture eval/fixtures/review-3591.json \
  --output /tmp/result.json
```

`review_runner.py` fetches the PR diff, clones the repo, checks out the PR
head, invokes `claude_reviewer.review_pr` directly (no comments posted), then
scores against `ground_truth.maintainer_comments` with `line_tolerance=10`.

## Running the full live review eval

Live eval uses real GitHub Actions runs on a mirror repo, so the agent's
behavior is identical to production.

1. **Mirror the PRs.** Push each fixture's upstream PR branch to
   `sarthakaggarwal97/valkey` and open draft PRs with base = `eval-base`
   (a branch pinned at `upstream/unstable`, so the mirror diff is exactly
   the upstream PR diff).
2. **Reset review state.** Remove each mirror PR's entry from
   `review-state.json` on the `bot-data` branch so the agent does an
   initial (not incremental) review.
3. **Trigger each review:**
   ```bash
   gh workflow run review-external-pr.yml \
     --field target_repo=sarthakaggarwal97/valkey \
     --field pr_number=<N>
   ```
4. **Wait for completion.** Reviews run in parallel under GitHub Actions
   concurrency limits — roughly 1.5–3 hours for 30 PRs.
5. **Score.** Fetch posted comments from each mirror PR via the API and run
   `score_review_flow` / `score_review_flow_loose` per fixture. Summarize
   with `report.render_report(scores)`.

### Cost and wall-clock

| Scope | Cost | Time |
|-------|------|------|
| One review fixture | ~$3–8 | 5–25 min (two Claude Opus calls) |
| Full 30-PR review eval | ~$100–200 | 2–3 hours |
| Daily / backport / fuzzer | Not yet automated in live workflows |

## Building new fixtures

### Review fixtures

The 30 review fixtures were built from merged `valkey-io/valkey` PRs with
two or more inline comments from the maintainer set. To find candidates:

```bash
MAINT='"madolson","zuiderkwast","enjoy-binbin","hpatro","soloestoy","ranshid"'
for pr in $(seq 3600 -1 3400); do
  n=$(gh api /repos/valkey-io/valkey/pulls/$pr/comments \
    --jq "[.[] | select(.user.login | IN(${MAINT}))] | length" 2>/dev/null)
  if [ "${n:-0}" -ge 2 ]; then
    echo "PR #$pr: $n maintainer comments"
  fi
done
```

Then for each chosen PR, fetch metadata, inline review comments, and the
changed-files list, and serialize to `eval/fixtures/review-<N>.json`
matching the schema above. Dedup comments by `(path, line)` bucket and
drop approval-only comments before storing.

### Backport / daily / fuzzer

Harvest from merged backport PRs, daily-CI fix PRs, and fuzzer root-cause
PRs respectively. Keep `ground_truth.files_changed` / `fix_files_changed`
minimal to what the human fix actually touched.

## Reading `eval/RESULTS.md`

The results file holds the latest full-run summary:

- Headline metrics — strict F1, loose F1, style score, hard failure rate
- Per-PR table with both scores, agent and maintainer comment counts
- Version history (v1 → v2 → shadow → v3 → v4) showing regression / gains
- Breakdown sections — top performers, agent-vs-maintainer disagreements,
  real agent failures

The `report.render_report(scores)` helper emits the per-flow summary table
and sorted divergence list used there.

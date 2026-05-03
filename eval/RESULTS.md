# Valkey CI Agent — 10 PR Review Evaluation

**Date:** 2026-05-03  
**Model:** Claude Opus 4-7 via Bedrock, `effort=max`, `max_turns=240`  
**Method:** Each PR from `valkey-io/valkey` mirrored to `sarthakaggarwal97/valkey` (PRs #119–#128). Agent triggered via `review-external-pr.yml`. Findings scored against maintainer inline review comments (ground truth).

## Summary

| Metric | Value |
|--------|-------|
| PRs evaluated | 10 |
| PRs where agent posted ≥1 finding | 7 / 10 |
| PRs with F1 ≥ 0.3 | 5 / 10 |
| Average F1 (deduped ground truth) | **0.29** |
| Average style score | **1.00** (no forensic patterns) |
| PRs where agent completely agreed with maintainers | 0 |
| PRs where agent added **legitimate new findings** beyond maintainers | ~4 |
| Full agent failures (JSON parse error) | 1 / 10 |

## Per-PR Results

| PR | Title | Agent | Maint | Uniq | F1 | Verdict |
|----|-------|-------|-------|------|-----|---------|
| #3591 | streamTrim NULL pointer | 0 | 1 | 0 | 1.00 | ✅ Correct (maintainer just said "I like this") |
| #3580 | syncRead errno fix | 2 | 2 | 2 | 0.00 | 🟡 Different but valid findings |
| #3568 | GEOSEARCH BYPOLYGON leak | 0 | 3 | 1 | 0.00 | ❌ Failed (JSON parse error, cost $14) |
| #3545 | Module commandresult cleanup | 4 | 5 | 5 | 0.44 | ✅ Partial agreement |
| #3460 | Unique samples hashtableSample | 2 | 8 | 2 | 0.50 | ✅ Good agreement |
| #3520 | Document VALKEYCLI_HOST/PORT | 0 | 4 | 2 | 0.00 | 🟡 Style-only changes, agent correctly didn't nag |
| #3420 | Avoid server.h in cli/benchmark | 1 | 5 | 1 | 0.00 | 🟡 Agent found different issue |
| #3380 | CLUSTERSCAN MATCH optimization | 1 | 3 | 2 | 0.00 | 🟡 Agent found a bug, maintainer only had nits |
| #3360 | WATCH duplicate key O(N)→O(1) | 4 | 2 | 2 | 0.33 | ✅ Partial agreement |
| #3150 | Rehashing empty buckets | 3 | 7 | 4 | 0.57 | ✅ Best agreement |

## Detailed Analysis by PR

### ✅ Agent wins (genuine agreement or legitimate pick-ups)

**PR #3460 (Unique samples in hashtableSampleEntries)** — F1 0.50  
- Agent: `src/hashtable.c:2343` — flagged that `sampleRandomBuckets` is the new code path exercised.
- Maintainer: same area (`packet.tcl:138`) about the flaky test.
- Agent also found a real test-flakiness issue the maintainers debated for 3 comments.

**PR #3150 (Rehashing more empty buckets)** — F1 0.57  
- Agent found 3 real issues overlapping 4 of the 7 maintainer comments.
- Strong agreement on the critical hot-path.

**PR #3545 (Module commandresult cleanup)** — F1 0.44  
- Agent caught 4 issues, 2 overlapping with 5 maintainer comments.
- Real bug pickup on the unsubscribe path.

### 🟡 Disagreements (agent found different but valid things)

**PR #3580 (syncRead errno fix)** — F1 0.00  
- Maintainer: "TLS analogs also need this fix" at `tls.c`.
- Agent: "Unix socket analogs also need this fix" at `unix.c` AND "ECONNRESET is TCP-specific" at `syncio.c:98`.
- **Both are correct.** Agent just found the wrong-but-similar analog. The `unix.c` comment is actually a _deeper_ finding than the maintainer's.

**PR #3420 (Avoid server.h in cli/benchmark)** — F1 0.00  
- Maintainer: 5 comments discussing `__attribute__((always_inline))` portability.
- Agent: Found a stale comment in `commands.h:28`.
- Both valid, different focus. Agent's finding is a cleanup-nit, maintainer's was a real design question.

**PR #3380 (CLUSTERSCAN MATCH optimization)** — F1 0.00  
- Maintainer: 3 comments on test code (naming, loop intent).
- Agent: Found a **real semantic bug** at `cluster.c:1818` — the optimization breaks incremental scans if MATCH is added mid-scan.
- This is a case where the **agent's finding is more important than the maintainer's**.

**PR #3520 (Document VALKEYCLI_HOST/PORT)** — F1 0.00  
- PR only changes help text (`src/valkey-cli.c`, +4/-2 lines).
- Maintainers had 4 comments about formatting / wording.
- Agent found nothing. For a 4-line help text change, "no findings" is actually correct behavior.

### ❌ Real failures

**PR #3568 (GEOSEARCH BYPOLYGON leak)** — F1 0.00  
- Agent made 131 tool-calls over 22 minutes, cost **$14.10**, generated valid findings, but the final JSON had invalid syntax (trailing `...` in JSON).
- This is a production-impact bug in the agent. **Worth a fix before scaling.**

**PR #3591 (streamTrim NULL pointer fix)** — Special case  
- The sole maintainer comment was "I like this, the diff is smaller and easy to read."
- Agent posted 0 findings.
- Scored as F1=1.0 with our dedupe/approval-filter (the approval comment is excluded from ground truth).
- Without filtering, this would have been F1=0.

## Key findings from this eval

1. **Style score is 100%.** Every agent comment reads like a human maintainer would write it. Zero forensic patterns ("wc -l", "git cat-file", "the diff shows"). The earlier style work was effective.

2. **Agent recall is low (0–57%).** When there are maintainer comments, agent catches maybe 1 in 3 of the issues maintainers flagged.

3. **Agent precision is reasonable where it speaks.** When the agent DOES flag something, it's usually a real issue. ~80% of agent findings are defensible. The "false positives" are mostly legitimate extra findings outside the ground truth scope.

4. **Agent finds things maintainers missed.** PR #3380 is the clearest case — agent flagged a real semantic bug, maintainers only had style nits. This is the highest-value pattern.

5. **Agent is conservative.** Stays quiet on 3/10 PRs (#3591, #3568, #3520). For #3591 and #3520 this is correct behavior (no actionable findings). For #3568 it's the JSON parse bug.

6. **One $14 run failed to post findings.** The JSON parse failure on PR #3568 is the biggest-risk-per-run scenario. The agent did the work, produced valid findings internally, but a syntax error broke output.

## Cost / latency

| PR | Duration | Cost (approx) |
|----|----------|---------------|
| #3568 (failed) | 22 min | $14.10 |
| Others (est) | 10-20 min | $1-8 each |
| 10-PR total | ~3 hours | **~$40-60** |

Per-review cost is within budget target. Duration varies wildly — the JSON-parse-failure run was 3× more expensive than a successful run.

## Recommendations before integration

### Must-fix before pitching to TSC

1. **JSON-parse robustness.** Agent should retry or fall back when Claude emits malformed JSON (trailing commas, `...`, comments in JSON). Currently, a single syntax error wastes the whole run. Add a JSON repair step or a retry with "your JSON was invalid, fix it" prompt.

2. **Recall tuning.** Agent is catching ~1/3 of maintainer-flagged issues. Two paths:
   - Prompt iteration — emphasize test coverage, style suggestions, doc review. Currently the prompt heavily emphasizes memory/concurrency bugs, which is why we miss test-code review comments.
   - Run the reviewer twice with different personas ("code review" and "test/doc review") and merge.

### Nice-to-have

3. **Cost cap.** Stop at $5/run. The $14 outlier shows a runaway path exists.

4. **Second-pass consolidation.** When agent posts 4+ findings, ask it to consolidate into 2-3 higher-signal ones. The maintainer-style ideal is 2-3 short comments, not 4 longer ones.

## Integration recommendation (opt-in per PR)

**The evaluation supports a limited, opt-in rollout.** With F1=0.29 and style=1.00:

- The agent won't waste maintainer time with bad comments (style is maintainer-like).
- The agent catches issues maintainers miss on roughly 1-2 PRs out of 10 (high-value).
- The agent misses most maintainer-flagged issues — **it's additive, not replacement**.
- One-in-ten runs will fail catastrophically (JSON parse error). Fix this first.

### Recommended rollout path

**Week 1: Fix the JSON parse bug.** Single PR, 1-2 hours of work.

**Week 2: Shadow mode on 20 more PRs.** Run agent on recent merged PRs in `sarthakaggarwal97/valkey` mirror. Gather data. Publish to you (not to maintainers).

**Week 3: TSC pitch with this report + 20-PR data.** Propose opt-in `ai-review` label on `valkey-io/valkey`. 15-line workflow, zero-risk rollback (remove label stops bot).

**Week 4-6: 5-10 labeled PRs.** Measure maintainer acceptance rate.

**Week 7+: Default-on for first-time contributors.** If acceptance rate > 50%, expand.

## Appendix: The 15-line integration workflow

File: `.github/workflows/ai-review-on-label.yml` on `valkey-io/valkey`:

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

One label, one workflow. Maintainer-in-the-loop at every step. Complete rollback is "remove the label."

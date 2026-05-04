# Valkey CI Agent — PR Review Evaluation

**Latest run:** v5 (2026-05-04)
**Model:** Claude Opus 4-7 via Bedrock, `effort=max`, `max_turns=240`
**Method:** Each PR from `valkey-io/valkey` mirrored to `sarthakaggarwal97/valkey` with a clean base (`eval-base` branch pointing at upstream unstable). Agent triggered via `review-external-pr.yml`. Findings scored against maintainer inline review comments.

## Latest: v5 (rollback + skeptic + JSON robustness)

| Metric | Value |
|--------|-------|
| Hard failures | **0 / 30** |
| PRs with findings | **28 / 30** (93%) |
| Avg findings/PR | **2.5** |
| **Strict F1** | **0.297** |
| **Loose F1** | **0.400** (file-match partial credit) |
| Style score | **1.00** (zero forensic patterns) |
| PRs with Strict ≥ 0.3 | 14 / 30 |
| PRs with Loose ≥ 0.3 | 19 / 30 |

## Version history

| Version | Stage 1 approach | Skeptic pass | Strict F1 | Loose F1 | Failures | Notes |
|---------|------------------|--------------|-----------|----------|----------|-------|
| v1 | v2-era broad prompt | none | 0.285 | — | 1/30 | Original shadow (10 PRs). Chatty. |
| v2 shadow | v2 broad + JSON robustness | none | 0.248 | 0.387 | 1/30 | 30-PR expanded. **@sumitk163 accepted 7/10 findings on PR #143** |
| v3 | "I'd rather you post nothing" | none (confidence gate) | 0.078 | 0.093 | 1/30 | Too silent. 25/30 PRs got zero findings. |
| v4.1 | "broad coverage" softened | skeptic pass added | 0.134 | 0.186 | 0/30 | Better than v3, lost depth. |
| v4.2 | "coverage not precision" | softer skeptic | 0.264 | 0.380 | 0/30 | F1 looked good, **but regressed on #3565 (0 findings vs v2's 10).** |
| **v5** | **v2 broad + read carefully** | **skeptic v4.2** | **0.297** | **0.400** | **0/30** | **Current. Restored depth without losing skeptic's precision.** |

## Key learnings

### What worked (kept in v5)

1. **JSON parse robustness** (v3): 4-pass parser (strict → repair → regex → object-scan) handles `...` placeholders, trailing commas, comments. Eliminated JSON failures at source.

2. **Prose-to-JSON retry** (v4.1 → v4.2): when stage 1 emits prose instead of JSON, extract findings from the prose. v4.2 preserves 8000 chars of analysis (vs v3's 2000) and asks for extraction, not judgment.

3. **max_turns=240 + salvage** (v4.1): when Claude hits turn limit, scan assistant messages for salvageable JSON. Graceful fail-to-empty instead of hard failure.

4. **Skeptic pass** (v4.2, inspired by PR #8): separate Opus call that independently verifies each candidate finding by reading code. Drops speculative/duplicate/style-only. Fails open (keeps all findings on skeptic error). Critical prompt language: "keep anything with concrete evidence; drop only clearly speculative or duplicate."

5. **No-Bash in prompt** (v4.1): explicit "Tools: Read, Grep, Glob only. No Bash." Stopped Claude from wasting turns on denied `git log` attempts.

6. **Style guards** (v2): "Bad examples" in prompt + deterministic forensic-pattern detector on output. Style score held at 1.00 across all 180+ findings in v5.

### What didn't work (removed)

1. **v3 "silent is usually correct" framing.** Prompt said *"I'd rather you post nothing than post a nit"* and *"Returning `[]` is the correct answer most of the time"*. Result: 25/30 PRs silent, missed real issues.

2. **v4.2 "coverage not precision" framing.** Prompt said stage 1's job was coverage and skeptic would filter. Claude interpreted as "enumerate quickly without deep reading." Result: shortcut runs (3 turns, $0.31 instead of 40+ turns deep reading), regressed on PR #3565 where v2 caught 10 real issues.

3. **essential:bool schema flag** (v3): tried to have Claude self-label findings as essential. Combined with a confidence filter, this over-filtered. Skeptic pass does the same work better.

4. **Default max_review_comments=5** (v3): too aggressive. v5 allows up to 8 pre-skeptic, typically emits 2-4 after skeptic.

## External validation

On [PR #143](https://github.com/sarthakaggarwal97/valkey/pull/143) (v2-shadow mirror of upstream #3565 — AOF data integrity), upstream author @sumitk163 came through the next day and commented on the bot's findings:

| Agent finding | @sumitk163 reply |
|---------------|------------------|
| `src/aof.c:1680` — `strstr` matching `#INTEGRITY_OFF` is too loose | **"Fixed."** |
| `src/aof.c:1546` — CRC64=0 ambiguity | **"Fixed."** |
| `src/server.c:3225` — silently zeroing user config | Acknowledged, addressing later |
| `src/aof.c:1488` — `%llx` vs decimal checksum format | **"Updated"** (twice) |
| `tests/integration/aof-integrity.tcl:63` — missing refuse-to-serve assertion | **"Updated"** |
| `valkey.conf:1731` — help text doesn't distinguish tamper vs bit-rot | **"Updated"** |
| `src/aof.c:1675` — ignored `fseek` return value | **"Updated"** |

**7 of 10 bot-posted findings were accepted and acted on by the upstream author.** This is the strongest external validation we have.

v5 re-running on the same PR (mirror #203) found 5 concrete findings including the exact `strstr` matching issue — so v5 also catches the problems that real humans validated.

## Top performers (v5)

| PR | Loose F1 | Strict F1 | Notes |
|----|----------|-----------|-------|
| #3150 | 0.75 | 0.50 | rehashing empty buckets |
| #3419 | 0.75 | 0.50 | listpack threshold |
| #3516 | 0.67 | 0.67 | HPERSIST RESP fix |
| #3520 | 0.67 | 0.67 | VALKEYCLI doc |
| #3416 | 0.67 | 0.67 | allocator size |
| #3460 | 0.67 | 0.67 | hashtableSampleEntries |
| #3413 | 0.60 | 0.40 | infoCommand SDS |
| #3561 | 0.67 | 0.44 | dict abstraction refactor |
| #3402 | 0.57 | 0.29 | VALKEYCLI env |

## Cost per eval

| Metric | v5 value |
|--------|----------|
| Per-PR cost (stage 1 + skeptic) | $3-8 typical, $15 max |
| 30-PR total run cost | ~$120-180 |
| Wall-clock time | 1.5-3 hours (GitHub Actions concurrency-bound) |

## Integration recommendation

v5 is ready for shadow → opt-in rollout. Metrics support pitching opt-in labeled reviews on `valkey-io/valkey`:

- 0/30 hard failures on the latest run
- 100% style score — no risk of embarrassing comments
- 93% of PRs produce findings (19/30 with Loose F1 ≥ 0.3)
- Maintainer-validated on real PR (@sumitk163's 7/10 acceptance rate)
- Dismissable with one click (opt-in via label)

See [docs/reviewer.md](../docs/reviewer.md) for the integration plan and the 15-line maintainer-side workflow YAML.

## Appendix: The evaluation framework

See [docs/eval.md](../docs/eval.md) for:
- Fixture format per flow (review/daily/backport/fuzzer)
- Strict vs Loose F1 scoring details
- How to build new fixtures from valkey-io/valkey PRs
- How to run live reviews via mirror PRs on `sarthakaggarwal97/valkey`
- Cost/time per fixture and per 30-PR run

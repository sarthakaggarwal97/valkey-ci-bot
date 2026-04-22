# AGENTS.md

Behavioral guidelines to reduce common LLM coding mistakes.
Source: Adapted from [Andrej Karpathy's guidelines](https://github.com/forrestchang/andrej-karpathy-skills/blob/main/CLAUDE.md).

These rules apply to all LLM-generated code in this project — fix generation,
code review, conflict resolution, and root cause analysis.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

- State assumptions explicitly. If uncertain, say so.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so.
- If something is unclear, stop. Name what's confusing.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

**The test: Every changed line should trace directly to the root cause.**

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Fix the test" → "Write a patch that makes the failing test pass"
- "Resolve the conflict" → "Produce a merged file that compiles and preserves both sides' intent"

---

**These guidelines are working if:** generated patches are minimal, diffs contain
no unrelated changes, and fixes address exactly the identified root cause.

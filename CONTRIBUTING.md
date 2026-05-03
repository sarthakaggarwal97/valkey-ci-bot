# Contributing to valkey-ci-agent

Thanks for your interest in contributing. This repo is an AI agent that
remediates Valkey CI failures, reviews PRs, backports fixes, and monitors
fuzzer and Daily runs — all driven from GitHub Actions with durable state on
a `bot-data` branch. Contributions of every size are welcome: bug fixes, new
log parsers, new eval fixtures, prompt tweaks, docs.

Before you start, skim [`README.md`](README.md) for the feature tour and
[`docs/architecture.md`](docs/architecture.md) for the module map and pipeline
diagrams. [`AGENTS.md`](AGENTS.md) captures the behavioral rules all
LLM-generated code in the repo is expected to follow — worth reading even if
you only touch Python directly.

## Development environment

- **Python 3.11+** is required. The codebase still advertises 3.9 in
  `pyproject.toml` for consumers, but tests, mypy, and CI all target 3.11.
  Use [`mise`](https://mise.jdx.dev/) or [`pyenv`](https://github.com/pyenv/pyenv)
  if your system Python is older.
- Install dependencies:
  ```bash
  pip install -r requirements.txt -r requirements-dev.txt
  ```
- **Claude Code CLI** is required for anything that touches live review or
  fix generation end-to-end:
  ```bash
  npm install -g @anthropic-ai/claude-code
  ```
- **AWS credentials for Bedrock** are needed when running the agent against
  real models. Claude Code is wired through Bedrock:
  ```bash
  export CLAUDE_CODE_USE_BEDROCK=1
  export AWS_REGION=us-west-2      # or your region
  export AWS_PROFILE=your-profile
  ```
  Copy `.env.example` to `.env.local` and fill in `GITHUB_TOKEN`, `AWS_REGION`,
  and `AWS_PROFILE` for local runs. Source it manually before invoking
  scripts.

Most unit tests mock the model layer, so you don't need AWS or Claude Code
installed just to run `pytest`.

## Code style

- **Type hints everywhere.** Python 3.11+ syntax (`list[str]`, `X | None`) is
  fine. mypy is configured in `pyproject.toml` under `[tool.mypy]` —
  `--ignore-missing-imports` is set.
- **Ruff** handles lint and import ordering:
  ```bash
  ruff check scripts/ tests/
  ```
  Only the `E`, `F`, `W`, `I` rule sets are enabled today. Don't silently
  expand the selection in unrelated PRs.
- **Prefer dataclasses** over plain dicts for structured data. See
  `scripts/agent_runtime.py` (`AgentProfile`) or `scripts/models.py` for
  examples.
- **Logging conventions:** use `logger = logging.getLogger(__name__)` at
  module top, pass structured data as keyword args where the helper supports
  it, and avoid f-strings inside log calls unless you've already formatted
  the message. Match the style of nearby code.

## Testing

- Run the suite with either form:
  ```bash
  pytest
  python -m pytest tests/ -v
  ```
- **Coverage threshold is 72%.** CI enforces it with
  `--cov-fail-under=72`. Run it locally the same way:
  ```bash
  pytest --cov=scripts --cov-fail-under=72 -q
  ```
- Every bug fix should ship with a regression test.
- **Mock the model layer.** For LLM-backed code paths, patch `run_agent`,
  `run_claude_code`, or `BedrockClient` so the test runs offline.
  [`tests/test_claude_reviewer.py`](tests/test_claude_reviewer.py) is a good
  reference — it fakes `run_claude_code` output via JSONL strings and patches
  the subprocess call at the seam.

## Local verification before push

Run all three before pushing — CI runs the same checks and rejecting late is
wasteful:

```bash
ruff check scripts/ tests/                   # lint clean
mypy scripts/ --ignore-missing-imports       # type check
pytest --cov=scripts --cov-fail-under=72 -q  # tests + coverage
```

## Commit style

- **DCO sign-off is required**:
  ```bash
  git commit -s -m "reviewer: tighten prompt fencing for untrusted diffs"
  ```
- Use a **lowercase area prefix**: `reviewer:`, `eval:`, `ci:`, `docs:`,
  `parsers:`, `backport:`, `fuzzer:`, `dashboard:`, …
- Keep commits focused. Don't mix reviewer changes with unrelated refactors.
- Describe the **why** in the body, not just what.

## Pull requests

- Target `main`.
- Run the full local verification block above before pushing.
- Wait for CI to go green before requesting review.
- Reference related issues or eval run artifacts when applicable.

## Adding a new log parser (`scripts/parsers/`)

1. Create `scripts/parsers/my_parser.py`.
2. Implement a class with:
   - a `priority: int` class attribute (lower fires first — see the priority
     table in [`docs/architecture.md`](docs/architecture.md#log-parsing-scriptsparsers))
   - a `parse(self, log_text: str, job_name: str) -> list[ParsedFailure]`
     method
3. Register the parser in `scripts/log_parser.py`'s `LogParserRouter`.
4. Add `tests/test_my_parser.py` with representative log fixtures.
5. Update the parser priority table in `docs/architecture.md`.

## Adding a new agent profile (`scripts/agent_runtime.py`)

1. Add the profile name to the `AgentProfileName` `Literal`.
2. Add an entry in `AGENT_PROFILES` with an explicit:
   - `allowed_tools` (tool allowlist string)
   - `timeout`
   - `max_turns`
   - `effort`
   - `writes_allowed`
   - `output_schema`
3. **Prefer the fewest tools necessary.** `Read/Grep/Glob` is the most
   secure baseline; only add `Edit/Write` when the task genuinely needs to
   modify files.
4. Use `failure_policy = "fail-closed"` by default. Use `"fail-open"` only
   when errors must not break a downstream step (rare).

## Adding a new eval fixture (`eval/fixtures/`)

Full schema lives in `docs/eval.md`. Quick version for PR review fixtures:

1. Pick a real `valkey-io/valkey` PR with maintainer review comments.
2. Fetch metadata, comments, and files via `gh api`.
3. Write a JSON file under `eval/fixtures/` following the schema in
   `docs/eval.md`. See `eval/fixtures/review-3561.json` for a concrete example.
4. Validate it loads:
   ```bash
   python -c "from scripts.eval.eval_fixtures import load_fixtures; print(load_fixtures('eval/fixtures/'))"
   ```

## Working with the `bot-data` branch

All durable state lives on the orphan `bot-data` branch:

- `failure-store.json`
- `review-state.json`
- `rate-state.json`
- `agent-events.jsonl`
- `monitor-state.json`

**Never commit state files to `main`.** CI workflows commit to `bot-data`
through a dedicated process.

If you need to reset state to re-run a review or replay a fix locally:

```bash
# Clone just bot-data
git clone -b bot-data --single-branch \
  git@github.com:sarthakaggarwal97/valkey-ci-agent.git /tmp/bot-data
cd /tmp/bot-data

# Example: drop a review entry so the reviewer retries PR #117
python -c 'import json; \
d = json.load(open("review-state.json")); \
d.pop("sarthakaggarwal97/valkey#117", None); \
json.dump(d, open("review-state.json","w"), indent=2)'

git commit -am "Reset state for PR #117 re-test" --signoff
git push origin bot-data
```

Use this sparingly — it edits production bot memory.

## Claude Code CLI integration notes

- The CLI is invoked via `subprocess` in `scripts/claude_code.py`.
- Output format is `--output-format stream-json` (JSONL events).
- Final text is extracted from the `{"type": "result", "result": "..."}`
  event. On `max_turns` or error, a fallback scanner pulls JSON arrays out
  of assistant messages.
- The env allowlist strips `GITHUB_TOKEN`, `GH_TOKEN`, and `*_SECRET` before
  invoking Claude. Do not bypass this.
- Evidence files are auto-written to `CI_AGENT_EVIDENCE_DIR` (defaults to
  `agent-evidence/` under GitHub Actions).

## Safety / security gotchas

- **Never** pass untrusted input directly to shell commands. Use
  `subprocess.run([...])` with list args; never `shell=True` with
  interpolated user data.
- **Never** commit secrets. Local `.env*` files are in `.gitignore` —
  keep them there.
- **Every new LLM prompt** must include the untrusted-data fencing
  template. Read `scripts/claude_reviewer.py` and `scripts/claude_fix.py`
  for the current pattern before inventing a new one.
- Use `yaml.safe_load()`. Never `yaml.load()`.
- Escape any user-supplied data rendered into dashboard HTML with
  `html.escape()` (helpers live in `scripts/html_helpers.py`).

## Reporting bugs

Open an issue at
<https://github.com/sarthakaggarwal97/valkey-ci-agent/issues> with:

- the command or workflow you ran
- what you expected
- what actually happened
- any `agent-evidence/` artifacts from the failed run
- relevant log excerpts (trim to the failing section)

The more we can reproduce locally, the faster we can land a fix.

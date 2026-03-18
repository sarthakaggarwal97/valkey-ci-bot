"""Bedrock-backed detailed PR code review."""

from __future__ import annotations

import json
import logging
import re
from pathlib import PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

from scripts.bedrock_client import BedrockClient, PromptClient
from scripts.bedrock_retriever import BedrockRetriever
from scripts.config import RetrievalConfig, ReviewerConfig
from scripts.github_client import retry_github_call
from scripts.models import ChangedFile, DiffScope, PullRequestContext, ReviewFinding

_SYSTEM_PROMPT = """You are a highly experienced software engineer performing a thorough code review.
Your purpose is to find substantive defects — correctness bugs, regressions,
security vulnerabilities, performance risks, or missing validation.

Rules:
- Only report issues that are directly supported by the provided patch/content.
- The provided excerpts may be truncated; never treat missing context as a bug.
- Do not speculate about symbols, methods, fields, workflows, or files that are
  not shown, and do not ask maintainers to verify whether something exists.
- Do NOT provide general feedback, summaries, explanations of changes, or praises
  for making good additions.
- Focus solely on offering specific, objective insights based on the given context.
- Avoid duplicate or overlapping findings for the same root cause.
- If your finding depends on the ABSENCE of a guard, check, cleanup, or
  validation in code that is NOT fully visible in the diff, do NOT report it.
  Assume that unseen code is correct and that invariants documented in comments
  are maintained.
- You MAY report concurrency issues, memory leaks, or use-after-free bugs when
  the faulty code path is visible in the provided excerpts. Trace the path
  through the shown code and explain the concrete scenario.
- Prefer precision over recall: it is far better to miss a real bug than to
  report a false positive. Only report when you are highly confident.
- YAML workflow files (.yml/.yaml) often embed shell or Python scripts inside
  ``run: |`` heredoc blocks. The YAML-level indentation (the leading spaces
  that align the code with the YAML key) is STRIPPED at runtime. Do NOT report
  indentation errors based on the absolute column count in the file — only the
  relative indentation within the embedded script matters. This is a very
  common source of false positives.
- Return valid JSON only."""

_SPECULATIVE_SUBSTRINGS = (
    "not shown in the diff",
    "not shown in diff",
    "there is no evidence",
    "appears to be cut off",
    "truncated in the review",
    "verify whether",
    "verify that",
    "verify the full file",
    "older callers",
    "not visible in the provided",
    "not visible in the diff",
    "cannot determine from the",
    "without seeing the full",
    "without the full context",
    "rest of the file",
    "outside the provided",
    "beyond the scope of",
    "may or may not",
    "it is unclear whether",
    "it's unclear whether",
    "consider checking",
    "consider verifying",
    "might want to check",
    "should verify that",
    "ensure that the caller",
    "ensure that callers",
)

_SPECULATIVE_PATTERNS = (
    re.compile(r"\bif this method does not exist\b"),
    re.compile(r"\bif the model does not define\b"),
    re.compile(r"\bif the model doesn't define\b"),
    re.compile(r"\bassuming (?:that |this )?\w+ (?:is|does|has)\b"),
    re.compile(r"\bif [`_a-zA-Z0-9.()'-]+ returns a\b"),
)


def _extract_json_payload(text: str) -> Any:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()

    start_object = candidate.find("{")
    start_array = candidate.find("[")
    if start_array != -1 and (start_object == -1 or start_array < start_object):
        end_array = candidate.rfind("]")
        if end_array == -1:
            raise ValueError("No JSON array found.")
        return json.loads(candidate[start_array : end_array + 1])

    if start_object == -1:
        raise ValueError("No JSON object found.")
    end_object = candidate.rfind("}")
    if end_object == -1:
        raise ValueError("No JSON object found.")
    return json.loads(candidate[start_object : end_object + 1])


def _looks_like_code(path: str) -> bool:
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in {
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx",
        ".py", ".pyi",
        ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
        ".go",
        ".java", ".kt", ".kts", ".scala",
        ".rb",
        ".rs",
        ".sh", ".bash", ".zsh",
        ".cs",
        ".swift",
        ".m", ".mm",
        ".lua",
        ".pl", ".pm",
        ".php",
        ".r",
        ".tcl",
        ".zig",
        ".v",
        ".hs",
        ".ex", ".exs",
        ".erl",
        ".clj", ".cljs",
        ".ml", ".mli",
        ".dart",
        ".sol",
        ".vue", ".svelte",
    }


def _serialize_scope(scope: DiffScope, *, max_chars: int = 500_000) -> str:
    chunks: list[str] = []
    used = 0
    for changed_file in scope.files:
        chunk = [
            f"Path: {changed_file.path}",
            f"Status: {changed_file.status}",
            f"Additions: {changed_file.additions}",
            f"Deletions: {changed_file.deletions}",
        ]
        if changed_file.patch:
            new_hunk, old_hunk = _split_hunks(changed_file.patch)
            chunk.append("<new_hunk>")
            chunk.append(new_hunk)
            chunk.append("</new_hunk>")
            if old_hunk.strip():
                chunk.append("<old_hunk>")
                chunk.append(old_hunk)
                chunk.append("</old_hunk>")
        if changed_file.contents:
            chunk.append("Full file contents (may be truncated):")
            chunk.append(changed_file.contents[:60_000])
        rendered = "\n".join(chunk)
        if used + len(rendered) > max_chars:
            # Try to fit a truncated version instead of skipping entirely
            budget = max_chars - used
            if budget > 500:
                truncated_patch = changed_file.patch[:budget - 200] if changed_file.patch else ""
                chunk_trunc = [
                    f"Path: {changed_file.path}",
                    f"Status: {changed_file.status}",
                    f"Additions: {changed_file.additions}",
                    f"Deletions: {changed_file.deletions}",
                ]
                if truncated_patch:
                    chunk_trunc.append("Patch (unified diff — TRUNCATED to fit budget):")
                    chunk_trunc.append(_annotate_patch(truncated_patch))
                chunks.append("\n".join(chunk_trunc))
            break
        chunks.append(rendered)
        used += len(rendered)
    return "\n\n".join(chunks)


def _chunk_diff_scope(scope: DiffScope, *, max_chars_per_chunk: int = 180_000) -> list[DiffScope]:
    """Split a DiffScope into multiple chunks that each fit within the token budget.

    Large files are split by hunk boundaries so each chunk is self-contained.
    Small files are grouped together to minimize the number of LLM calls.
    """
    if not scope.files:
        return []

    chunks: list[DiffScope] = []
    current_files: list[ChangedFile] = []
    current_size = 0

    for changed_file in scope.files:
        # Estimate the serialized size of this file
        file_size = len(changed_file.patch or "") * 2  # new_hunk + old_hunk
        if changed_file.contents:
            file_size += min(len(changed_file.contents), 60_000)
        file_size += 200  # metadata overhead

        # If a single file is too large, split it by hunk boundaries
        if file_size > max_chars_per_chunk and changed_file.patch:
            # Flush current batch first
            if current_files:
                chunks.append(DiffScope(
                    base_sha=scope.base_sha,
                    head_sha=scope.head_sha,
                    files=current_files,
                    incremental=scope.incremental,
                ))
                current_files = []
                current_size = 0

            hunk_groups = _split_patch_into_groups(changed_file.patch, max_chars_per_chunk // 3)
            for hunk_patch in hunk_groups:
                from dataclasses import replace as _replace
                chunk_file = _replace(changed_file, patch=hunk_patch)
                chunks.append(DiffScope(
                    base_sha=scope.base_sha,
                    head_sha=scope.head_sha,
                    files=[chunk_file],
                    incremental=scope.incremental,
                ))
            continue

        # If adding this file would exceed the budget, flush
        if current_size + file_size > max_chars_per_chunk and current_files:
            chunks.append(DiffScope(
                base_sha=scope.base_sha,
                head_sha=scope.head_sha,
                files=current_files,
                incremental=scope.incremental,
            ))
            current_files = []
            current_size = 0

        current_files.append(changed_file)
        current_size += file_size

    if current_files:
        chunks.append(DiffScope(
            base_sha=scope.base_sha,
            head_sha=scope.head_sha,
            files=current_files,
            incremental=scope.incremental,
        ))

    return chunks if chunks else [scope]


def _split_patch_into_groups(patch: str, max_chars: int) -> list[str]:
    """Split a unified diff patch into groups of hunks that fit within max_chars."""
    hunks: list[str] = []
    current_hunk_lines: list[str] = []

    for line in patch.splitlines():
        if line.startswith("@@") and current_hunk_lines:
            hunks.append("\n".join(current_hunk_lines))
            current_hunk_lines = []
        current_hunk_lines.append(line)

    if current_hunk_lines:
        hunks.append("\n".join(current_hunk_lines))

    if not hunks:
        return [patch]

    groups: list[str] = []
    current_group: list[str] = []
    current_size = 0

    for hunk in hunks:
        if current_size + len(hunk) > max_chars and current_group:
            groups.append("\n".join(current_group))
            current_group = []
            current_size = 0
        current_group.append(hunk)
        current_size += len(hunk)

    if current_group:
        groups.append("\n".join(current_group))

    return groups if groups else [patch]
def _annotate_patch(patch: str) -> str:
    """Prefix each diff line with its right-side line number.

    Hunk headers and deleted lines (which have no right-side position) are
    left unnumbered so the LLM can read line numbers directly instead of
    counting from ``@@`` headers.
    """
    out: list[str] = []
    current_line = 0
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            if match:
                current_line = int(match.group(1))
            out.append(raw)
            continue
        if raw.startswith("-"):
            # Deleted lines have no right-side position
            out.append(f"       {raw}")
            continue
        # '+' (added) or ' ' (context) lines map to the right side
        out.append(f"L{current_line:<5d} {raw}")
        current_line += 1
    return "\n".join(out)


def _split_hunks(patch: str) -> tuple[str, str]:
    """Split a unified diff into annotated new_hunk and old_hunk.

    new_hunk: added and context lines with right-side line numbers.
    old_hunk: removed and context lines (the code being replaced).

    This gives the LLM both the new code (with line numbers for commenting)
    and the old code (for understanding what changed).
    """
    new_lines: list[str] = []
    old_lines: list[str] = []
    current_line = 0
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            if match:
                current_line = int(match.group(1))
            new_lines.append(raw)
            old_lines.append(raw)
            continue
        if raw.startswith("-"):
            old_lines.append(raw[1:] if len(raw) > 1 else "")
            continue
        if raw.startswith("+"):
            new_lines.append(f"{current_line}: {raw[1:] if len(raw) > 1 else ''}")
            current_line += 1
            continue
        # context line
        old_lines.append(raw)
        new_lines.append(f"{current_line}: {raw}")
        current_line += 1
    return "\n".join(new_lines), "\n".join(old_lines)


def _parse_diff_lines(patch: str) -> tuple[set[int], set[int]]:
    """Extract valid RIGHT-side line numbers from a unified diff.

    Returns (added_lines, context_lines) where added_lines are ``+`` lines
    and context_lines are unchanged lines shown in the diff.
    """
    added_lines: set[int] = set()
    context_lines: set[int] = set()
    current_line = 0
    for raw_line in patch.splitlines():
        if raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)", raw_line)
            if match:
                current_line = int(match.group(1))
            continue
        if raw_line.startswith("-"):
            continue
        if raw_line.startswith("+"):
            added_lines.add(current_line)
            current_line += 1
        else:
            context_lines.add(current_line)
            current_line += 1
    return added_lines, context_lines


def _snap_line_to_diff(
    line: int,
    added_lines: set[int],
    context_lines: set[int],
) -> int | None:
    """Snap a line number to the nearest added line in the diff.

    Prefers ``+`` (added) lines over context lines.  Falls back to the
    nearest context line only when no added line is within range.
    Returns ``None`` when no diff line is within 5 lines.
    """
    all_lines = added_lines | context_lines
    if not all_lines:
        return None
    if line in added_lines:
        return line
    # Try nearest added line first (within 5 lines)
    if added_lines:
        closest_added = min(added_lines, key=lambda v: abs(v - line))
        if abs(closest_added - line) <= 5:
            return closest_added
    # Fall back to any diff line
    if line in context_lines:
        return line
    closest = min(all_lines, key=lambda v: abs(v - line))
    if abs(closest - line) <= 5:
        return closest
    return None


def _normalize_finding_text(text: str) -> str:
    """Collapse whitespace and lowercase text for filtering and dedupe."""
    return " ".join(text.lower().split())


def _is_speculative_finding(body: str) -> bool:
    """Reject findings that explicitly depend on missing or unseen evidence."""
    normalized = _normalize_finding_text(body)
    if any(marker in normalized for marker in _SPECULATIVE_SUBSTRINGS):
        return True
    return any(pattern.search(normalized) for pattern in _SPECULATIVE_PATTERNS)


_INDENTATION_KEYWORDS = re.compile(
    r"\bindent(?:ation|ed)?\b|\b\d+\s*spaces?\b|\btab[s ]?\b"
    r"|\bwhitespace\b|\bmis-?indent\b|\bnesting\b",
    re.IGNORECASE,
)


def _is_inside_yaml_script_block(
    file_contents: str,
    line: int,
) -> bool:
    """Return True when *line* falls inside a YAML ``run: |`` heredoc block.

    In GitHub Actions workflow files, Python/shell code is embedded inside
    ``run: |`` (or ``run: |+``, ``run: |-``) blocks.  The YAML-level
    indentation is stripped at runtime, so the *relative* indentation of
    the embedded code is what matters — not the absolute column count in
    the file.  LLMs frequently miscount indentation in these blocks
    because they reason about the raw file columns.
    """
    lines = file_contents.splitlines()
    if line < 1 or line > len(lines):
        return False

    target_line_raw = lines[line - 1]
    if not target_line_raw.strip():
        return False

    # Scan the file for all ``run: |`` blocks and check whether the
    # target line falls inside any of them.
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.lstrip()
        if re.match(r"run:\s*\|[+-]?\s*$", stripped):
            run_indent = len(raw) - len(stripped)
            # The block content starts on the next non-empty line.
            # Its indent defines the base indent of the block.
            block_start = i + 1
            base_indent: int | None = None
            j = block_start
            while j < len(lines):
                bline = lines[j]
                bstripped = bline.lstrip()
                if not bstripped:
                    # Blank lines are part of the block.
                    j += 1
                    continue
                bindent = len(bline) - len(bstripped)
                if base_indent is None:
                    if bindent <= run_indent:
                        # Empty block — no content.
                        break
                    base_indent = bindent
                if bindent < base_indent:
                    # Left the block.
                    break
                j += 1
            # block spans lines[block_start] .. lines[j-1] (1-indexed: block_start+1 .. j)
            if base_indent is not None and (block_start + 1) <= line <= j:
                return True
            i = j
        else:
            i += 1
    return False


def _is_false_indentation_finding(
    body: str,
    path: str,
    line: int | None,
    file_contents: str | None,
) -> bool:
    """Reject indentation findings that contradict the actual file content.

    LLMs frequently miscount spaces when reasoning from unified diffs
    because the ``+``/``-``/`` `` prefix shifts columns by one.  When
    the finding mentions indentation and we have the full file, verify
    the claim before letting it through.

    Also filters indentation findings inside YAML ``run: |`` heredoc
    blocks, where the absolute column count is misleading because YAML
    strips the block's base indentation at runtime.
    """
    if not _INDENTATION_KEYWORDS.search(body):
        return False
    if line is None or not file_contents:
        return False

    lines = file_contents.splitlines()
    if line < 1 or line > len(lines):
        return False

    # For YAML workflow files, indentation findings inside ``run: |``
    # blocks are almost always false positives — the LLM reasons about
    # the raw YAML-level indent instead of the runtime-stripped indent.
    is_yaml = path.endswith((".yml", ".yaml"))
    if is_yaml and _is_inside_yaml_script_block(file_contents, line):
        logger.info(
            "Filtering indentation finding for %s:%d — "
            "line is inside a YAML run: | heredoc block.",
            path,
            line,
        )
        return True

    actual_line = lines[line - 1]
    actual_indent = len(actual_line) - len(actual_line.lstrip())

    # Extract the claimed indent from the finding body (e.g. "9 spaces")
    claimed_match = re.search(r"(\d+)\s*spaces?", body, re.IGNORECASE)
    if claimed_match:
        claimed_spaces = int(claimed_match.group(1))
        if claimed_spaces != actual_indent:
            logger.info(
                "Filtering false indentation finding for %s:%d — "
                "claimed %d spaces, actual %d spaces.",
                path,
                line,
                claimed_spaces,
                actual_indent,
            )
            return True

    # Also check suggestion blocks — if the suggestion has the same
    # indentation as the actual file, the finding is a no-op.
    suggestion_match = re.search(
        r"```suggestion\n(.*?)\n```", body, re.DOTALL,
    )
    if suggestion_match:
        suggested_line = suggestion_match.group(1).splitlines()[0]
        suggested_indent = len(suggested_line) - len(suggested_line.lstrip())
        if suggested_indent == actual_indent and suggested_line.strip() == actual_line.strip():
            logger.info(
                "Filtering no-op indentation suggestion for %s:%d — "
                "suggestion matches actual file.",
                path,
                line,
            )
            return True

    return False


def _build_retrieval_query(pr: PullRequestContext, diff_scope: DiffScope) -> str:
    """Build a retrieval query for detailed review context."""
    lines = [pr.title, pr.body]
    for changed_file in diff_scope.files:
        lines.extend([
            changed_file.path,
            changed_file.patch or "",
        ])
    return "\n".join(filter(None, lines))


# ------------------------------------------------------------------
# Agentic review: tool definitions and handler
# ------------------------------------------------------------------

_GET_FILE_TOOL: dict = {
    "toolSpec": {
        "name": "get_file",
        "description": (
            "Fetch the contents of a file from the repository at the PR's "
            "head commit. Use this to read files that are referenced by the "
            "changed code but not included in the diff — for example, "
            "imported modules, header files, configuration files, or test "
            "fixtures. Returns the file contents as text (truncated to 60 KB)."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path in the repository.",
                    },
                },
                "required": ["path"],
            },
        },
    },
}

_LIST_FILES_TOOL: dict = {
    "toolSpec": {
        "name": "list_directory",
        "description": (
            "List files in a directory of the repository at the PR's head "
            "commit. Use this to discover file names when you need to find "
            "related source files, headers, or tests. Returns a newline-"
            "separated list of file paths."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Directory path to list. Use empty string or '.' "
                            "for the repository root."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
}

_SEARCH_CODE_TOOL: dict = {
    "toolSpec": {
        "name": "search_code",
        "description": (
            "Search for a text pattern across the repository. Use this to "
            "find all callers of a function, all references to a variable, "
            "all places a macro or constant is used, or to locate where "
            "something is defined. Returns matching file paths and line "
            "excerpts. Limited to 15 results."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The text or symbol to search for. Use a function "
                            "name, variable name, string literal, or short "
                            "code pattern."
                        ),
                    },
                    "path_filter": {
                        "type": "string",
                        "description": (
                            "Optional file extension or path prefix to narrow "
                            "results (e.g. '.c', '.tcl', 'src/'). Omit to "
                            "search all files."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
}

_SUBMIT_REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "body": {"type": "string"},
                },
                "required": ["path", "body"],
            },
        },
        "lgtm": {"type": "boolean"},
    },
    "required": ["reviews", "lgtm"],
}

_SUBMIT_REVIEW_TOOL: dict = {
    "toolSpec": {
        "name": "submit_review",
        "description": (
            "Submit your final code review findings. Call this tool ONLY "
            "after you have gathered all the context you need. Each finding "
            "must reference a file path from the PR diff and include a "
            "concrete, evidence-based description of the defect."
        ),
        "inputSchema": {"json": _SUBMIT_REVIEW_SCHEMA},
    },
}


class ReviewToolHandler:
    """Executes tool calls during the agentic review loop.

    Fetches files and directory listings from the GitHub repository
    at the PR's head commit SHA.
    """

    def __init__(
        self,
        github_client: "Any",
        repo_name: str,
        head_sha: str,
        *,
        max_file_bytes: int = 60_000,
        max_fetches: int = 10,
        github_retries: int = 5,
    ) -> None:
        self._gh = github_client
        self._repo_name = repo_name
        self._head_sha = head_sha
        self._max_file_bytes = max_file_bytes
        self._max_fetches = max_fetches
        self._github_retries = github_retries
        self._fetch_count = 0
        self._cache: dict[str, str] = {}

    def execute(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call and return the result text."""
        if tool_name == "get_file":
            return self._get_file(tool_input.get("path", ""))
        if tool_name == "list_directory":
            return self._list_directory(tool_input.get("path", ""))
        if tool_name == "search_code":
            return self._search_code(
                tool_input.get("query", ""),
                tool_input.get("path_filter", ""),
            )
        return f"Unknown tool: {tool_name}"

    def _get_file(self, path: str) -> str:
        if not path:
            return "Error: path is required."
        if path in self._cache:
            return self._cache[path]
        if self._fetch_count >= self._max_fetches:
            return (
                f"Fetch limit reached ({self._max_fetches}). "
                "Please submit your review with the context you have."
            )
        self._fetch_count += 1
        try:
            repo = retry_github_call(
                lambda: self._gh.get_repo(self._repo_name),
                retries=self._github_retries,
                description=f"get repo {self._repo_name}",
            )
            contents = retry_github_call(
                lambda: repo.get_contents(path, ref=self._head_sha),
                retries=self._github_retries,
                description=f"get file {path}",
            )
            if isinstance(contents, list):
                # It's a directory, not a file
                names = [c.path for c in contents]
                result = f"{path} is a directory. Contents:\n" + "\n".join(names)
                self._cache[path] = result
                return result
            raw = contents.decoded_content.decode("utf-8", errors="replace")
            truncated = raw[: self._max_file_bytes]
            if len(raw) > self._max_file_bytes:
                truncated += f"\n\n[truncated at {self._max_file_bytes} bytes]"
            self._cache[path] = truncated
            logger.info("Fetched %s (%d bytes) for agentic review.", path, len(raw))
            return truncated
        except Exception as exc:
            msg = f"Could not fetch {path}: {exc}"
            logger.warning(msg)
            return msg

    def _list_directory(self, path: str) -> str:
        if self._fetch_count >= self._max_fetches:
            return (
                f"Fetch limit reached ({self._max_fetches}). "
                "Please submit your review with the context you have."
            )
        self._fetch_count += 1
        try:
            repo = retry_github_call(
                lambda: self._gh.get_repo(self._repo_name),
                retries=self._github_retries,
                description=f"get repo {self._repo_name}",
            )
            contents = retry_github_call(
                lambda: repo.get_contents(path or "", ref=self._head_sha),
                retries=self._github_retries,
                description=f"list dir {path}",
            )
            if not isinstance(contents, list):
                return f"{path} is a file, not a directory."
            names = sorted(c.path for c in contents)
            return "\n".join(names)
        except Exception as exc:
            return f"Could not list {path}: {exc}"

    def _search_code(self, query: str, path_filter: str = "") -> str:
        if not query:
            return "Error: query is required."
        if self._fetch_count >= self._max_fetches:
            return (
                f"Fetch limit reached ({self._max_fetches}). "
                "Please submit your review with the context you have."
            )
        self._fetch_count += 1
        try:
            # Build the GitHub code search query
            search_q = f"{query} repo:{self._repo_name}"
            if path_filter:
                # Distinguish bare extensions like ".c" from paths like "src/"
                # or "tests/instances.tcl".  A bare extension has no "/" and
                # matches r"^\.\w+$".
                stripped = path_filter.strip()
                if re.match(r"^\.\w+$", stripped):
                    search_q += f" language:{stripped.lstrip('.')}"
                else:
                    search_q += f" path:{stripped}"

            results = retry_github_call(
                lambda: self._gh.search_code(search_q),
                retries=self._github_retries,
                description=f"search code for '{query}'",
            )

            lines: list[str] = []
            count = 0
            for item in results:
                if count >= 15:
                    break
                # Each result has .path and .text_matches (if available)
                entry = f"- {item.path}"
                text_matches = getattr(item, "text_matches", None)
                if text_matches:
                    for match in text_matches[:2]:
                        fragment = match.get("fragment", "").strip()
                        if fragment:
                            # Show first 200 chars of the fragment
                            entry += f"\n  > {fragment[:200]}"
                lines.append(entry)
                count += 1

            if not lines:
                return f"No results found for '{query}'."

            logger.info(
                "Code search for '%s' returned %d result(s).", query, count,
            )
            return f"Found {count} result(s) for '{query}':\n" + "\n".join(lines)
        except Exception as exc:
            msg = f"Code search failed for '{query}': {exc}"
            logger.warning(msg)
            return msg


class CodeReviewer:
    """Generates focused review findings for risky code changes."""

    def __init__(
        self,
        bedrock_client: PromptClient,
        *,
        retriever: BedrockRetriever | None = None,
        retrieval_config: RetrievalConfig | None = None,
        github_client: "Any | None" = None,
    ) -> None:
        self._bedrock = bedrock_client
        self._retriever = retriever
        self._retrieval_config = retrieval_config or RetrievalConfig()
        self._github_client = github_client

    def classify_simple_change(self, files: list[ChangedFile]) -> bool:
        """Return ``True`` for changes that are likely trivial."""
        if not files:
            return True

        total_delta = sum(changed_file.additions + changed_file.deletions for changed_file in files)
        if total_delta <= 5:
            return True

        return all(not _looks_like_code(changed_file.path) for changed_file in files)

    def triage_file(
        self,
        changed_file: ChangedFile,
        pr: PullRequestContext,
        config: ReviewerConfig,
    ) -> str:
        """Triage a single file as NEEDS_REVIEW or APPROVED using the light model.

        Returns "NEEDS_REVIEW" or "APPROVED".
        """
        if not changed_file.patch:
            return "APPROVED"

        # Very small changes to non-code files are auto-approved
        delta = changed_file.additions + changed_file.deletions
        if delta <= 3 and not _looks_like_code(changed_file.path):
            return "APPROVED"

        triage_prompt = f"""Triage this file diff as NEEDS_REVIEW or APPROVED.

Rules:
- If the diff modifies logic, control flow, function signatures, variable assignments, or anything that could affect behavior: NEEDS_REVIEW
- If the diff only fixes typos, formatting, comments, renames for clarity, or trivial whitespace: APPROVED
- When in doubt, err on the side of NEEDS_REVIEW

PR title: {pr.title}

File: {changed_file.path}
Status: {changed_file.status}

Diff:
{changed_file.patch[:4000]}

Respond with ONLY one line:
[TRIAGE]: NEEDS_REVIEW
or
[TRIAGE]: APPROVED
"""
        try:
            response = self._bedrock.invoke(
                "You triage pull request file diffs. Respond with only the triage line.",
                triage_prompt,
                model_id=config.models.light_model_id,
                max_output_tokens=50,
                temperature=0.0,
            )
            if "APPROVED" in response and "NEEDS_REVIEW" not in response:
                logger.info("Triage: %s -> APPROVED", changed_file.path)
                return "APPROVED"
        except Exception as exc:
            logger.warning("Triage failed for %s: %s, defaulting to NEEDS_REVIEW", changed_file.path, exc)

        logger.info("Triage: %s -> NEEDS_REVIEW", changed_file.path)
        return "NEEDS_REVIEW"

    def triage_files(
        self,
        files: list[ChangedFile],
        pr: PullRequestContext,
        config: ReviewerConfig,
    ) -> list[ChangedFile]:
        """Filter files to only those that need detailed review.

        Uses the light model to triage each file, skipping trivial changes.
        Returns the subset of files that need review.
        """
        if not files:
            return []

        needs_review: list[ChangedFile] = []
        approved_count = 0
        for changed_file in files:
            verdict = self.triage_file(changed_file, pr, config)
            if verdict == "NEEDS_REVIEW":
                needs_review.append(changed_file)
            else:
                approved_count += 1

        logger.info(
            "File triage complete: %d need review, %d approved (skipped).",
            len(needs_review),
            approved_count,
        )
        return needs_review

    def review(
        self,
        pr: PullRequestContext,
        diff_scope: DiffScope,
        config: ReviewerConfig,
        *,
        short_summary: str = "",
    ) -> list[ReviewFinding]:
        """Review the selected diff scope with the configured heavy model.

        When a GitHub client is available, uses the agentic review path
        which can fetch additional files from the repo during review.
        Otherwise falls back to the single-scope prompt-based review.
        """
        if not diff_scope.files:
            return []

        # Choose the review strategy
        use_agentic = (
            self._github_client is not None
            and isinstance(self._bedrock, BedrockClient)
        )
        review_fn = self._review_agentic if use_agentic else self._review_single_scope

        if use_agentic:
            logger.info("Using agentic review with tool-use loop.")

        # Check if the scope needs to be split into multiple chunks.
        char_budget = (config.max_input_tokens * 4) * 3 // 4
        scope_size = sum(
            len(f.patch or "") * 2 + min(len(f.contents or ""), 60_000) + 200
            for f in diff_scope.files
        )
        if scope_size > char_budget:
            chunks = _chunk_diff_scope(diff_scope, max_chars_per_chunk=char_budget)
            if len(chunks) > 1:
                logger.info(
                    "Splitting review into %d chunks (scope_size=%d, budget=%d).",
                    len(chunks), scope_size, char_budget,
                )
                all_findings: list[ReviewFinding] = []
                for i, chunk in enumerate(chunks):
                    logger.info(
                        "Reviewing chunk %d/%d (%d file(s)).",
                        i + 1, len(chunks), len(chunk.files),
                    )
                    chunk_findings = review_fn(
                        pr, chunk, config,
                        short_summary=short_summary,
                    )
                    all_findings.extend(chunk_findings)
                # Deduplicate across chunks
                seen: set[tuple[str, int | None, str]] = set()
                deduped: list[ReviewFinding] = []
                for f in all_findings:
                    key = (f.path, f.line, _normalize_finding_text(f.body))
                    if key not in seen:
                        seen.add(key)
                        deduped.append(f)
                capped = deduped[: config.max_review_comments]
                return capped

        findings = review_fn(
            pr, diff_scope, config, short_summary=short_summary,
        )
        capped = findings[: config.max_review_comments]
        return capped

    def _review_single_scope(
        self,
        pr: PullRequestContext,
        diff_scope: DiffScope,
        config: ReviewerConfig,
        *,
        short_summary: str = "",
    ) -> list[ReviewFinding]:
        """Review a single diff scope chunk with the configured heavy model."""
        if not diff_scope.files:
            return []

        retrieved_context = ""
        if self._retriever is not None:
            retrieved_context = self._retriever.render_for_prompt(
                _build_retrieval_query(pr, diff_scope),
                self._retrieval_config,
                section_title="Retrieved Valkey Context",
            )

        summary_section = ""
        if short_summary:
            summary_section = f"""
Summary of all changes in this PR (for cross-file context):
{short_summary}
"""

        custom_instructions_section = ""
        if config.custom_instructions:
            custom_instructions_section = f"""
## Project-Specific Review Guidelines
{config.custom_instructions}
"""

        user_prompt = f"""Review this pull request and return only actionable findings.

PR title: {pr.title}
PR description:
{pr.body}
{summary_section}
Review scope excerpts (patch/content may be truncated):
{_serialize_scope(diff_scope)}

{retrieved_context}
{custom_instructions_section}

Return JSON in one of these shapes:
[
  {{
    "path": "relative/path",
    "line": <line number from the diff's +N side, or null if unsure>,
    "severity": "high|medium|low",
    "body": "single concrete finding in markdown"
  }}
]

or
{{ "findings": [ ... ] }}

CRITICAL rules:
- New hunks are annotated with line numbers (e.g. "120: code"). Old hunks show the replaced code without line numbers. Use the line numbers from new_hunk for the "line" field.
- Only return findings with direct evidence in the shown patch/content excerpts.
- Do NOT provide general feedback, summaries, explanations of changes, or praises for making good additions.
- Focus solely on offering specific, objective insights based on the given context and refrain from making broad comments about potential impacts on the system or question intentions behind the changes.
- Do NOT speculate about array sizes, buffer lengths, variable values, or code structure that is not fully visible in the provided excerpts.
- Do not infer missing definitions from other files or from omitted parts of a file.
- Do not report that a file, diff, or workflow looks truncated.
- Do not ask maintainers to verify whether a symbol exists.
- Prefer one strongest finding per root cause; if unsure, return [].
- Do not emit generic praise.
- For code modification suggestions, use GitHub's suggestion format in the body:
  ```suggestion
  corrected code here
  ```

<example_input>
<new_hunk file="src/server.c">
@@ -118,7 +118,7 @@
120:   int timeout = config->timeout;
121:   if (timeout = 0) {{
122:       timeout = DEFAULT_TIMEOUT;
123:   }}
124:   startServer(timeout);
</new_hunk>

<old_hunk file="src/server.c">
@@ -118,7 +118,7 @@
  int timeout = config->timeout;
  if (timeout == 0) {{
      timeout = DEFAULT_TIMEOUT;
  }}
  startServer(timeout);
</old_hunk>
</example_input>

<example_output>
[
  {{
    "path": "src/server.c",
    "line": 121,
    "severity": "high",
    "body": "Bug: assignment `=` used instead of comparison `==` in condition. This will always set timeout to 0 and evaluate as false, so `DEFAULT_TIMEOUT` is never applied.\\n\\n```suggestion\\n    if (timeout == 0) {{\\n```"
  }}
]
</example_output>
"""
        # Define the JSON schema for structured tool-use output
        _review_schema: dict = {
            "type": "object",
            "properties": {
                "reviews": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "line": {"type": "integer"},
                            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                            "body": {"type": "string"},
                        },
                        "required": ["path", "body"],
                    },
                },
                "lgtm": {"type": "boolean"},
            },
            "required": ["reviews", "lgtm"],
        }

        # Try structured tool-use first, fall back to plain invoke + JSON parsing
        raw_findings: list = []
        _has_schema_method = (
            callable(getattr(self._bedrock, "invoke_with_schema", None))
            and type(self._bedrock).__name__ != "MagicMock"
        )
        used_tool_use = False
        if _has_schema_method:
            try:
                response = self._bedrock.invoke_with_schema(
                    _SYSTEM_PROMPT,
                    user_prompt,
                    tool_name="generate_review_json",
                    tool_description="Generate code review findings in structured JSON format",
                    json_schema=_review_schema,
                    model_id=config.models.heavy_model_id,
                    max_output_tokens=config.max_output_tokens,
                    temperature=config.models.temperature,
                )
                payload = json.loads(response) if isinstance(response, str) else response
                if isinstance(payload, dict):
                    raw_findings = payload.get("reviews", [])
                    used_tool_use = True
                    logger.info("Used structured tool-use output, got %d finding(s).", len(raw_findings))
            except Exception as tool_exc:
                logger.info("Tool-use failed (%s), falling back to plain invoke.", tool_exc)

        if not used_tool_use:
            response = self._bedrock.invoke(
                _SYSTEM_PROMPT,
                user_prompt,
                model_id=config.models.heavy_model_id,
                max_output_tokens=config.max_output_tokens,
                temperature=config.models.temperature,
            )
            try:
                payload = _extract_json_payload(response)
            except Exception as exc:
                raise ValueError("Unparseable review response") from exc
            raw_findings = payload.get("findings", []) if isinstance(payload, dict) else payload

        if not isinstance(raw_findings, list):
            raise ValueError("Review response did not contain a findings list.")

        logger.info("LLM returned %d raw finding(s).", len(raw_findings))

        allowed_paths = {changed_file.path for changed_file in diff_scope.files}
        reviewable_files = {changed_file.path: changed_file for changed_file in diff_scope.files}
        findings: list[ReviewFinding] = []
        seen_keys: set[tuple[str, int | None, str]] = set()
        filtered_no_line = 0
        filtered_speculative = 0
        filtered_lgtm = 0
        filtered_path = 0
        for raw_finding in raw_findings:
            if not isinstance(raw_finding, dict):
                continue
            path = str(raw_finding.get("path", "")).strip()
            if not path or path not in allowed_paths:
                filtered_path += 1
                continue
            body = str(raw_finding.get("body", "")).strip()
            if not body:
                continue
            lowered = body.lower()
            if not config.review_comment_lgtm and (
                "lgtm" in lowered or "looks good" in lowered or "no issues" in lowered
            ):
                filtered_lgtm += 1
                continue
            line = raw_finding.get("line")
            if _is_speculative_finding(body):
                filtered_speculative += 1
                continue
            changed_file = reviewable_files[path]
            normalized_body = _normalize_finding_text(body)
            normalized_line = int(line) if isinstance(line, int) and line > 0 else None
            if normalized_line is not None and changed_file.patch:
                added_lines, context_lines = _parse_diff_lines(changed_file.patch)
                snapped = _snap_line_to_diff(normalized_line, added_lines, context_lines)
                if snapped != normalized_line:
                    logger.info(
                        "Line %d for %s snapped to %s (added=%d, context=%d lines in diff)",
                        normalized_line,
                        path,
                        snapped,
                        len(added_lines),
                        len(context_lines),
                    )
                normalized_line = snapped
            if _is_false_indentation_finding(
                body, path, normalized_line, changed_file.contents,
            ):
                filtered_speculative += 1
                continue
            dedupe_key = (path, normalized_line, normalized_body)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            findings.append(
                ReviewFinding(
                    path=path,
                    line=normalized_line,
                    body=body,
                    severity=str(raw_finding.get("severity", "medium")).strip() or "medium",
                )
            )

        logger.info(
            "Finding filter stats: path=%d, lgtm=%d, speculative=%d, kept=%d",
            filtered_path,
            filtered_lgtm,
            filtered_speculative,
            len(findings),
        )

        capped = findings[: config.max_review_comments]

        return capped


    # ------------------------------------------------------------------
    # Agentic review with tool-use loop
    # ------------------------------------------------------------------

    def _review_agentic(
        self,
        pr: PullRequestContext,
        diff_scope: DiffScope,
        config: ReviewerConfig,
        *,
        short_summary: str = "",
    ) -> list[ReviewFinding]:
        """Review using a multi-turn tool-use loop.

        The model can call ``get_file`` and ``list_directory`` to fetch
        additional context from the repository before submitting its
        final findings via ``submit_review``.

        Falls back to ``_review_single_scope`` if the bedrock client
        does not support ``converse_with_tools`` or if the GitHub client
        is not available.
        """
        if not isinstance(self._bedrock, BedrockClient):
            logger.info("Agentic review requires BedrockClient; falling back.")
            return self._review_single_scope(
                pr, diff_scope, config, short_summary=short_summary,
            )
        if self._github_client is None:
            logger.info("No GitHub client for agentic review; falling back.")
            return self._review_single_scope(
                pr, diff_scope, config, short_summary=short_summary,
            )
        if not diff_scope.files:
            return []

        retrieved_context = ""
        if self._retriever is not None:
            retrieved_context = self._retriever.render_for_prompt(
                _build_retrieval_query(pr, diff_scope),
                self._retrieval_config,
                section_title="Retrieved Valkey Context",
            )

        summary_section = ""
        if short_summary:
            summary_section = f"\nSummary of all changes in this PR (for cross-file context):\n{short_summary}\n"

        custom_instructions_section = ""
        if config.custom_instructions:
            custom_instructions_section = f"\n## Project-Specific Review Guidelines\n{config.custom_instructions}\n"

        user_prompt = f"""Review this pull request. You have tools to fetch additional files from the repository if you need more context to verify a potential finding.

PR title: {pr.title}
PR description:
{pr.body}
{summary_section}
Review scope excerpts (patch/content may be truncated):
{_serialize_scope(diff_scope)}

{retrieved_context}
{custom_instructions_section}

WORKFLOW:
1. Read the diff carefully and identify potential issues.
2. If a potential finding depends on code in another file (e.g. an imported function, a header, a caller, a config file), use get_file to fetch that file and verify your hypothesis BEFORE reporting it.
3. If you're unsure which file to look at, use list_directory to explore the repo structure.
4. Use search_code to find all callers of a function, all references to a variable, or to locate where something is defined across the codebase.
5. Once you have enough context, call submit_review with your findings.

CRITICAL rules:
- New hunks are annotated with line numbers (e.g. "120: code"). Use these for the "line" field.
- Only report findings with direct evidence. If you fetched a file and it disproves your hypothesis, DROP the finding.
- Do NOT report speculative issues. If you cannot verify a finding with the available tools, do not report it.
- Do NOT provide general feedback, summaries, or praise.
- Prefer precision over recall: it is far better to miss a real bug than to report a false positive.
- YAML workflow files embed scripts in run: | blocks — YAML strips the base indentation at runtime. Do NOT report indentation errors in these blocks.
- For code suggestions, use GitHub's suggestion format in the body:
  ```suggestion
  corrected code here
  ```
- When you are done, call submit_review. If there are no issues, set lgtm to true and reviews to [].
"""

        tool_handler = ReviewToolHandler(
            github_client=self._github_client,
            repo_name=pr.repo,
            head_sha=pr.head_sha,
            github_retries=config.github_retries,
        )

        tools = [_GET_FILE_TOOL, _LIST_FILES_TOOL, _SEARCH_CODE_TOOL, _SUBMIT_REVIEW_TOOL]

        try:
            response = self._bedrock.converse_with_tools(
                _SYSTEM_PROMPT,
                user_prompt,
                tools=tools,
                tool_handler=tool_handler,
                terminal_tool="submit_review",
                max_turns=10,
                model_id=config.models.heavy_model_id,
                max_output_tokens=config.max_output_tokens,
                temperature=config.models.temperature,
            )
        except Exception as exc:
            logger.warning(
                "Agentic review failed (%s); falling back to single-scope.",
                exc,
            )
            return self._review_single_scope(
                pr, diff_scope, config, short_summary=short_summary,
            )

        # Parse the submit_review JSON
        try:
            payload = json.loads(response) if isinstance(response, str) else response
        except Exception:
            payload = _extract_json_payload(response)

        raw_findings = []
        if isinstance(payload, dict):
            raw_findings = payload.get("reviews", [])
        elif isinstance(payload, list):
            raw_findings = payload

        logger.info("Agentic review returned %d raw finding(s).", len(raw_findings))

        # Apply the same filtering as _review_single_scope
        return self._filter_raw_findings(raw_findings, diff_scope, config)

    def _filter_raw_findings(
        self,
        raw_findings: list,
        diff_scope: DiffScope,
        config: ReviewerConfig,
    ) -> list[ReviewFinding]:
        """Apply standard post-processing filters to raw LLM findings."""
        allowed_paths = {f.path for f in diff_scope.files}
        reviewable_files = {f.path: f for f in diff_scope.files}
        findings: list[ReviewFinding] = []
        seen_keys: set[tuple[str, int | None, str]] = set()
        filtered_speculative = 0
        filtered_lgtm = 0
        filtered_path = 0

        for raw_finding in raw_findings:
            if not isinstance(raw_finding, dict):
                continue
            path = str(raw_finding.get("path", "")).strip()
            if not path or path not in allowed_paths:
                filtered_path += 1
                continue
            body = str(raw_finding.get("body", "")).strip()
            if not body:
                continue
            lowered = body.lower()
            if not config.review_comment_lgtm and (
                "lgtm" in lowered or "looks good" in lowered or "no issues" in lowered
            ):
                filtered_lgtm += 1
                continue
            line = raw_finding.get("line")
            if _is_speculative_finding(body):
                filtered_speculative += 1
                continue
            changed_file = reviewable_files[path]
            normalized_body = _normalize_finding_text(body)
            normalized_line = int(line) if isinstance(line, int) and line > 0 else None
            if normalized_line is not None and changed_file.patch:
                added_lines, context_lines = _parse_diff_lines(changed_file.patch)
                snapped = _snap_line_to_diff(normalized_line, added_lines, context_lines)
                if snapped != normalized_line:
                    logger.info(
                        "Line %d for %s snapped to %s",
                        normalized_line, path, snapped,
                    )
                normalized_line = snapped
            if _is_false_indentation_finding(
                body, path, normalized_line, changed_file.contents,
            ):
                filtered_speculative += 1
                continue
            dedupe_key = (path, normalized_line, normalized_body)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            findings.append(
                ReviewFinding(
                    path=path,
                    line=normalized_line,
                    body=body,
                    severity=str(raw_finding.get("severity", "medium")).strip() or "medium",
                )
            )

        logger.info(
            "Agentic filter stats: path=%d, lgtm=%d, speculative=%d, kept=%d",
            filtered_path, filtered_lgtm, filtered_speculative, len(findings),
        )
        return findings[: config.max_review_comments]

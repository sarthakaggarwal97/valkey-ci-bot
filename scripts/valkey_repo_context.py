"""Live Valkey repository guidance and workflow-derived runtime defaults."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]

from scripts.config import BotConfig, ReviewerConfig, ValidationProfile
from scripts.models import FailureReport, PullRequestContext

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)

_VALKEY_CONTEXT_MARKER = "## Live Valkey Maintainer Context"
_IMPORTANT_LABELS = (
    "needs-doc-pr",
    "pending-missing-dco",
    "run-extra-tests",
    "test-failure",
    "breaking-change",
    "to-be-merged",
    "to-be-closed",
)
_WORKFLOW_FALLBACK_FILES = (
    "ci.yml",
    "daily.yml",
    "external.yml",
    "weekly.yml",
    "benchmark-on-label.yml",
    "benchmark-release.yml",
)
_TEST_STEP_TOKENS = (
    "runtest",
    "test-unit",
    "unit tests",
    "unittest",
    "module api",
    "sentinel tests",
    "cluster tests",
    "compatibility tests",
    "backward compatibility",
    "test-tls",
    "moduleapi",
)
_BUILD_STEP_TOKENS = (
    "make",
    "cmake",
    "./configure",
    "configure ",
    "commands.def",
    "build ",
)
_INSTALL_STEP_TOKENS = (
    "apt-get install",
    "brew install",
    "apk add",
    "dnf install",
    "yum install",
    "pip install",
    "install gtest",
    "install libbacktrace",
)
_VALKEY_SUBSYSTEM_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cluster", ("cluster", "slot", "reshard", "failover")),
    ("sentinel", ("sentinel",)),
    ("moduleapi", ("moduleapi", "module api", "module unload", "tests/modules/", "src/modules/")),
    ("replication", ("replication", "replica", "psync")),
    ("persistence", ("appendonly", "aof", "rdb", "persistence")),
    ("tls", ("tls", "ssl", "openssl")),
    ("memory", ("maxmemory", "evict", "jemalloc", "defrag", "malloc")),
    ("acl-auth", ("acl", "auth", "user ")),
    ("scripting", ("eval", "lua", "function", "script")),
    ("pubsub", ("pubsub", "client tracking", "tracking")),
)


def is_valkey_repo(repo_name: str) -> bool:
    """Return whether the repository looks like a Valkey server repo."""
    normalized = repo_name.strip().lower()
    return normalized.endswith("/valkey")


@dataclass
class ValkeyInstruction:
    """One path-targeted instruction file from the Valkey repository."""

    name: str
    path: str
    apply_to: list[str]
    body: str

    def matches_path(self, path: str) -> bool:
        """Return whether an instruction applies to a changed path."""
        if not self.apply_to:
            return False
        normalized = path.strip()
        return any(
            fnmatchcase(normalized, candidate)
            for pattern in self.apply_to
            for expanded in _expand_brace_pattern(pattern)
            for candidate in _candidate_patterns(expanded)
        )

    def matches_any(self, paths: list[str]) -> bool:
        """Return whether an instruction applies to any changed path."""
        return any(self.matches_path(path) for path in paths)


@dataclass
class WorkflowRecipe:
    """A workflow-derived validation recipe for one Valkey job."""

    workflow_file: str
    job_id: str
    job_name: str = ""
    install_commands: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    def iter_validation_profiles(self) -> list[ValidationProfile]:
        """Convert the recipe into one or more runtime ValidationProfiles."""
        profiles: list[ValidationProfile] = []
        seen_patterns: set[str] = set()
        for candidate in [self.job_id, self.job_name]:
            normalized = _normalize_job_label(candidate)
            if not normalized:
                continue
            pattern = rf"^{re.escape(normalized)}(?:\s*\(|$)"
            if pattern in seen_patterns:
                continue
            seen_patterns.add(pattern)
            profiles.append(
                ValidationProfile(
                    job_name_pattern=pattern,
                    env=dict(self.env),
                    install_commands=list(self.install_commands),
                    build_commands=list(self.build_commands),
                    test_commands=list(self.test_commands),
                )
            )
        return profiles

    def to_validation_profile(self) -> ValidationProfile:
        """Convert the recipe into its primary runtime ValidationProfile."""
        profiles = self.iter_validation_profiles()
        if profiles:
            return profiles[0]
        return ValidationProfile(
            job_name_pattern=rf"^{re.escape(self.job_id)}(?:\s*\(|$)",
            env=dict(self.env),
            install_commands=list(self.install_commands),
            build_commands=list(self.build_commands),
            test_commands=list(self.test_commands),
        )


@dataclass
class ValkeyRepoContext:
    """Current Valkey repository metadata used to specialize the agent."""

    repo_name: str
    ref: str
    default_branch: str
    labels: dict[str, str]
    copilot_instructions: str
    instructions: list[ValkeyInstruction] = field(default_factory=list)
    workflow_texts: dict[str, str] = field(default_factory=dict)
    workflow_recipes: list[WorkflowRecipe] = field(default_factory=list)

    def applicable_instructions(self, paths: list[str]) -> list[ValkeyInstruction]:
        """Return the repo instruction files relevant to the given paths."""
        return [instruction for instruction in self.instructions if instruction.matches_any(paths)]

    def matching_recipes(self, job_name: str) -> list[WorkflowRecipe]:
        """Return workflow recipes whose job regex matches the given job name."""
        matches: list[WorkflowRecipe] = []
        for recipe in self.workflow_recipes:
            if any(
                re.search(profile.job_name_pattern, job_name)
                for profile in recipe.iter_validation_profiles()
            ):
                matches.append(recipe)
        return matches

    def render_review_guidance(self, pr: PullRequestContext) -> str:
        """Render live Valkey maintainer guidance for a review run."""
        paths = [changed_file.path for changed_file in pr.files]
        lines = [f"Target repository default branch: `{self.default_branch}`."]
        if _is_release_branch(pr.base_ref):
            lines.append(
                f"This pull request targets release branch `{pr.base_ref}`. "
                "Prefer narrowly scoped, low-risk changes that keep backports simple."
            )
        elif "daily.yml" in self.workflow_texts and pr.base_ref == "unstable":
            lines.append(
                "Valkey uses `run-extra-tests` to request the broader Daily matrix "
                "for `unstable` PRs."
            )
        else:
            lines.append("Review against Valkey maintainer policy and current repository conventions.")
        workflow_surface = _important_workflow_surface(self.workflow_texts)
        if workflow_surface:
            lines.append(
                "Loaded workflow context: "
                + ", ".join(f"`{name}`" for name in workflow_surface)
                + "."
            )
        important_labels = [
            f"- `{name}`: {self.labels[name]}"
            for name in _IMPORTANT_LABELS
            if name in self.labels
        ]
        if important_labels:
            lines.extend(["", "Important Valkey labels:", *important_labels])

        if self.copilot_instructions.strip():
            lines.extend([
                "",
                "Repository-wide maintainer policy:",
                _bulleted_block(self.copilot_instructions),
            ])

        applicable = self.applicable_instructions(paths)
        if applicable:
            lines.append("")
            lines.append("Path-specific guidance from `.github/instructions`:")
            for instruction in applicable:
                lines.append(f"- `{instruction.name}`")
                lines.append(_bulleted_block(instruction.body))
        return "\n".join(lines).strip()

    def render_failure_guidance(self, report: FailureReport) -> str:
        """Render live Valkey workflow guidance for one failure-analysis run."""
        evidence_paths = [
            parsed.file_path
            for parsed in report.parsed_failures
            if parsed.file_path
        ]
        evidence_text = [
            parsed.test_name or parsed.failure_identifier
            for parsed in report.parsed_failures
            if parsed.test_name or parsed.failure_identifier
        ]
        lines = [
            f"Target repository default branch: `{self.default_branch}`.",
            f"Observed target branch: `{report.target_branch or self.default_branch}`.",
        ]
        subsystem = infer_valkey_subsystem(
            evidence_paths,
            [report.job_name, report.workflow_file or "", *evidence_text],
        )
        if subsystem:
            lines.append(f"Likely Valkey subsystem: `{subsystem}`.")
        if _is_release_branch(report.target_branch):
            lines.append(
                "This failure is on a release branch. Favor the smallest safe fix "
                "and avoid changes that would widen release risk."
            )
        if report.workflow_file:
            lines.append(f"Workflow file: `{report.workflow_file}`.")
        workflow_text = self.workflow_texts.get(report.workflow_file or "")
        if workflow_text and report.workflow_file == "daily.yml":
            lines.append(
                "The Daily workflow is a broad matrix and uses `run-extra-tests` "
                "for deeper `unstable` PR coverage."
            )
        if workflow_text and report.workflow_file == "external.yml":
            lines.append(
                "The External workflow validates downstream integration behavior. "
                "Prefer compatibility fixes over Valkey-internal churn."
            )
        if workflow_text and report.workflow_file in {
            "benchmark-on-label.yml",
            "benchmark-release.yml",
        }:
            lines.append(
                "This benchmark workflow is performance-sensitive. Preserve "
                "throughput and latency behavior when proposing fixes."
            )
        if workflow_text and report.workflow_file == "weekly.yml":
            lines.append(
                "Weekly runs often exercise release maintenance paths. Treat "
                "branch-specific compatibility as part of the fix surface."
            )
        recipes = self.matching_recipes(report.job_name)
        if recipes:
            lines.append("")
            lines.append("Workflow-derived validation recipes for this job:")
            for recipe in recipes[:2]:
                lines.append(
                    f"- `{recipe.job_name or recipe.job_id}` build={len(recipe.build_commands)} "
                    f"test={len(recipe.test_commands)}"
                )
                for command in recipe.build_commands[:2]:
                    lines.append(f"  - build: `{command}`")
                for command in recipe.test_commands[:3]:
                    lines.append(f"  - test: `{command}`")
        applicable = self.applicable_instructions(evidence_paths)
        if applicable:
            lines.append("")
            lines.append("Relevant Valkey path guidance:")
            for instruction in applicable:
                lines.append(f"- `{instruction.name}`")
                lines.append(_bulleted_block(instruction.body))
        return "\n".join(lines).strip()


def _expand_brace_pattern(pattern: str) -> list[str]:
    """Expand a simple ``*.{c,h}``-style brace pattern."""
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    options = [item.strip() for item in match.group(1).split(",") if item.strip()]
    if not options:
        return [pattern]
    prefix = pattern[: match.start()]
    suffix = pattern[match.end() :]
    expanded: list[str] = []
    for option in options:
        expanded.extend(_expand_brace_pattern(f"{prefix}{option}{suffix}"))
    return expanded


def _candidate_patterns(pattern: str) -> list[str]:
    """Return small fnmatch-compatible variants for ``**`` path globs."""
    variants = [pattern]
    if "/**/" in pattern:
        variants.append(pattern.replace("/**/", "/"))
    if pattern.endswith("/**"):
        variants.append(pattern[: -len("/**")])
    return list(dict.fromkeys(variants))


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Split markdown front matter from the remaining body text."""
    if not text.startswith("---\n"):
        return {}, text.strip()
    end_marker = "\n---\n"
    end = text.find(end_marker, 4)
    if end == -1:
        return {}, text.strip()
    metadata_raw = text[4:end]
    body = text[end + len(end_marker) :].strip()
    try:
        metadata = yaml.safe_load(metadata_raw) or {}
    except yaml.YAMLError:
        metadata = {}
    return metadata if isinstance(metadata, dict) else {}, body


def _parse_instruction(name: str, path: str, text: str) -> ValkeyInstruction:
    """Parse one instruction markdown file."""
    metadata, body = _split_front_matter(text)
    apply_to = metadata.get("applyTo", [])
    if not isinstance(apply_to, list):
        apply_to = []
    return ValkeyInstruction(
        name=name,
        path=path,
        apply_to=[item for item in apply_to if isinstance(item, str)],
        body=body,
    )


def _normalize_command(command: str) -> str:
    """Normalize a workflow ``run`` block into a reusable shell command."""
    cleaned = re.sub(r"\$\{\{.*?\}\}", "", command, flags=re.DOTALL)
    lines = [line.rstrip() for line in cleaned.strip().splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _normalize_job_label(label: str) -> str:
    """Normalize a workflow job label into a stable matching string."""
    if not isinstance(label, str):
        return ""
    cleaned = re.sub(r"\$\{\{.*?\}\}", "", label, flags=re.DOTALL)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _classify_step(name: str, command: str) -> str:
    """Classify a workflow step as install/build/test/other."""
    haystack = f"{name}\n{command}".lower()
    if any(token in haystack for token in _INSTALL_STEP_TOKENS):
        return "install"
    if "testprep" in haystack:
        return "install"
    if any(token in haystack for token in _TEST_STEP_TOKENS):
        return "test"
    if "test" in name.lower() and "testprep" not in name.lower():
        return "test"
    if any(token in haystack for token in _BUILD_STEP_TOKENS):
        return "build"
    return "other"


def infer_valkey_subsystem(paths: list[str], text_fragments: list[str]) -> str:
    """Infer the most likely Valkey subsystem from paths and failure text."""
    evidence = "\n".join(
        part.strip().lower()
        for part in [*paths, *text_fragments]
        if isinstance(part, str) and part.strip()
    )
    if not evidence:
        return ""
    scores: dict[str, int] = {}
    for subsystem, tokens in _VALKEY_SUBSYSTEM_RULES:
        score = sum(1 for token in tokens if token in evidence)
        if score:
            scores[subsystem] = score
    if not scores:
        return ""
    return max(scores.items(), key=lambda item: (item[1], item[0]))[0]


def _coerce_env(env: Any) -> dict[str, str]:
    """Normalize a workflow ``env`` map into string pairs."""
    if not isinstance(env, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in env.items()
        if isinstance(key, str) and isinstance(value, (str, int, float, bool))
    }


def _parse_workflow_recipes(workflow_file: str, text: str) -> list[WorkflowRecipe]:
    """Derive validation recipes from a GitHub Actions workflow file."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse Valkey workflow %s: %s", workflow_file, exc)
        return []
    if not isinstance(raw, dict):
        return []
    jobs = raw.get("jobs")
    if not isinstance(jobs, dict):
        return []

    recipes: list[WorkflowRecipe] = []
    for job_id, job_data in jobs.items():
        if not isinstance(job_id, str) or not isinstance(job_data, dict):
            continue
        steps = job_data.get("steps")
        if not isinstance(steps, list):
            continue
        recipe = WorkflowRecipe(
            workflow_file=workflow_file,
            job_id=job_id,
            job_name=_normalize_job_label(str(job_data.get("name", ""))),
            env=_coerce_env(job_data.get("env")),
        )
        for raw_step in steps:
            if not isinstance(raw_step, dict):
                continue
            command = raw_step.get("run")
            if not isinstance(command, str):
                continue
            normalized_command = _normalize_command(command)
            if not normalized_command:
                continue
            bucket = _classify_step(str(raw_step.get("name", "")), normalized_command)
            if bucket == "install":
                recipe.install_commands.append(normalized_command)
            elif bucket == "build":
                recipe.build_commands.append(normalized_command)
            elif bucket == "test":
                recipe.test_commands.append(normalized_command)
        if recipe.build_commands or recipe.test_commands:
            recipes.append(recipe)
    return recipes


def build_valkey_repo_context(
    repo_name: str,
    default_branch: str,
    *,
    ref: str,
    labels: dict[str, str],
    copilot_instructions: str,
    instruction_files: dict[str, str],
    workflow_files: dict[str, str],
) -> ValkeyRepoContext:
    """Build a ValkeyRepoContext from raw GitHub snapshots."""
    instructions = [
        _parse_instruction(PurePosixPath(path).name, path, text)
        for path, text in sorted(instruction_files.items())
        if isinstance(text, str) and text.strip()
    ]
    recipes: list[WorkflowRecipe] = []
    for path, text in sorted(workflow_files.items()):
        workflow_file = PurePosixPath(path).name
        if isinstance(text, str) and text.strip():
            recipes.extend(_parse_workflow_recipes(workflow_file, text))
    return ValkeyRepoContext(
        repo_name=repo_name,
        ref=ref,
        default_branch=default_branch,
        labels=dict(labels),
        copilot_instructions=copilot_instructions.strip(),
        instructions=instructions,
        workflow_texts={
            PurePosixPath(path).name: text
            for path, text in workflow_files.items()
            if isinstance(text, str)
        },
        workflow_recipes=recipes,
    )


def _fetch_text(repo: Any, path: str, ref: str) -> str:
    """Best-effort fetch of a text file from GitHub."""
    try:
        contents = repo.get_contents(path, ref=ref)
    except Exception as exc:
        logger.info("Valkey context fetch skipped for %s at %s: %s", path, ref, exc)
        return ""
    if isinstance(contents, list):
        return ""
    try:
        return contents.decoded_content.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Failed to decode Valkey context file %s: %s", path, exc)
        return ""


def load_valkey_repo_context(
    github_client: "Github",
    repo_name: str,
    *,
    ref: str | None = None,
) -> ValkeyRepoContext | None:
    """Load live Valkey repo metadata from GitHub when the repo matches."""
    if not is_valkey_repo(repo_name):
        return None
    try:
        repo = github_client.get_repo(repo_name)
    except Exception as exc:
        logger.warning("Could not load Valkey repo %s: %s", repo_name, exc)
        return None

    resolved_ref = ref or getattr(repo, "default_branch", None) or "unstable"
    labels: dict[str, str] = {}
    try:
        for label in repo.get_labels():
            name = getattr(label, "name", "")
            if isinstance(name, str) and name in _IMPORTANT_LABELS:
                labels[name] = str(getattr(label, "description", "") or "")
    except Exception as exc:
        logger.info("Could not load Valkey labels for %s: %s", repo_name, exc)

    copilot_instructions = _fetch_text(repo, ".github/copilot-instructions.md", resolved_ref)
    instruction_files: dict[str, str] = {}
    try:
        contents = repo.get_contents(".github/instructions", ref=resolved_ref)
        if isinstance(contents, list):
            for item in contents:
                path = getattr(item, "path", "")
                if isinstance(path, str) and path.endswith(".md"):
                    instruction_files[path] = _fetch_text(repo, path, resolved_ref)
    except Exception as exc:
        logger.info("Could not load Valkey instruction files for %s: %s", repo_name, exc)

    workflow_files: dict[str, str] = {}
    try:
        contents = repo.get_contents(".github/workflows", ref=resolved_ref)
        if isinstance(contents, list):
            for item in contents:
                path = getattr(item, "path", "")
                if isinstance(path, str) and path.endswith((".yml", ".yaml")):
                    workflow_files[path] = _fetch_text(repo, path, resolved_ref)
    except Exception as exc:
        logger.info("Could not enumerate Valkey workflow files for %s: %s", repo_name, exc)

    if not workflow_files:
        workflow_files = {
            f".github/workflows/{workflow_file}": _fetch_text(
                repo,
                f".github/workflows/{workflow_file}",
                resolved_ref,
            )
            for workflow_file in _WORKFLOW_FALLBACK_FILES
        }
    return build_valkey_repo_context(
        repo_name,
        getattr(repo, "default_branch", "unstable") or "unstable",
        ref=str(resolved_ref),
        labels=labels,
        copilot_instructions=copilot_instructions,
        instruction_files=instruction_files,
        workflow_files=workflow_files,
    )


def apply_valkey_runtime_defaults(
    config: BotConfig,
    repo_context: ValkeyRepoContext | None,
) -> BotConfig:
    """Append live Valkey workflow recipes to the runtime validation config."""
    if repo_context is None:
        return config
    existing_patterns = {profile.job_name_pattern for profile in config.validation_profiles}
    for recipe in repo_context.workflow_recipes:
        for profile in recipe.iter_validation_profiles():
            if profile.job_name_pattern in existing_patterns:
                continue
            config.validation_profiles.append(profile)
            existing_patterns.add(profile.job_name_pattern)
    if repo_context.default_branch and repo_context.default_branch not in config.project.description:
        config.project = replace(
            config.project,
            description=(
                f"{config.project.description.rstrip()} "
                f"Default development branch is {repo_context.default_branch}."
            ).strip(),
        )
    return config


def augment_reviewer_config_for_valkey(
    config: ReviewerConfig,
    pr: PullRequestContext,
    repo_context: ValkeyRepoContext | None,
) -> ReviewerConfig:
    """Inject live Valkey repo guidance into reviewer prompts."""
    if repo_context is None:
        return config
    dynamic_block = repo_context.render_review_guidance(pr)
    if not dynamic_block:
        return config
    merged = config.custom_instructions.strip()
    addition = f"{_VALKEY_CONTEXT_MARKER}\n{dynamic_block}"
    if addition in merged:
        return config
    if merged:
        merged = f"{merged}\n\n{addition}"
    else:
        merged = addition
    return replace(config, custom_instructions=merged)


def _bulleted_block(text: str) -> str:
    """Render a multi-line text block as compact markdown bullets."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(f"  - {line}" for line in lines[:12])


def _is_release_branch(name: str) -> bool:
    """Return whether the branch name looks like a Valkey release branch."""
    return bool(re.fullmatch(r"\d+\.\d+", (name or "").strip()))


def _important_workflow_surface(workflow_texts: dict[str, str]) -> list[str]:
    """Return the most relevant workflow files for maintainer-facing guidance."""
    preferred = [
        "ci.yml",
        "daily.yml",
        "external.yml",
        "weekly.yml",
        "benchmark-on-label.yml",
        "benchmark-release.yml",
    ]
    available = {name for name, text in workflow_texts.items() if text.strip()}
    return [name for name in preferred if name in available]

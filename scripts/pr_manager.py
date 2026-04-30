"""PR Manager — creates branches, commits patches, and opens pull requests."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from github.GithubException import GithubException
from github.InputGitTreeElement import InputGitTreeElement

from scripts.commit_signoff import (
    CommitSigner,
    append_signoff,
    load_signer_from_env,
    require_dco_signoff_from_env,
)
from scripts.failure_store import FailureStore
from scripts.github_client import retry_github_call
from scripts.models import FailureReport, RootCauseReport

if TYPE_CHECKING:
    from github import Github
    from github.PullRequest import PullRequest
    from github.Repository import Repository

    from scripts.summary import PRSummaryComment

logger = logging.getLogger(__name__)


def _escape_table_cell(value: object) -> str:
    """Return markdown-table-safe text."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", "<br>")


def _compute_fingerprint(report: FailureReport) -> str:
    """Derive the canonical incident key from the first parsed failure."""
    if report.parsed_failures:
        pf = report.parsed_failures[0]
        return FailureStore.compute_incident_key(
            pf.failure_identifier,
            pf.file_path,
            test_name=pf.test_name,
        )
    # Fallback for unparseable failures — use job name as identifier
    return FailureStore.compute_incident_key(
        report.job_name, ""
    )


def _build_commit_message(
    report: FailureReport,
    root_cause: RootCauseReport,
    signer: CommitSigner | None = None,
    *,
    require_dco_signoff: bool = False,
) -> str:
    """Build a descriptive commit message per Requirement 6.2.

    Includes: stable failure identifier (test name when available),
    job name, and summary of the root cause.
    """
    parts: list[str] = []

    # Stable failure identifier / test name
    if report.parsed_failures:
        pf = report.parsed_failures[0]
        identifier = pf.test_name or pf.failure_identifier
        parts.append(f"fix: {identifier}")
    else:
        parts.append(f"fix: {report.job_name}")

    # Job name
    parts.append(f"\nJob: {report.job_name}")

    # Root cause summary (first line of description, capped at 200 chars)
    summary = root_cause.description.split("\n")[0][:200]
    parts.append(f"Root cause: {summary}")

    return append_signoff(
        "\n".join(parts),
        signer or CommitSigner(),
        require_signoff=require_dco_signoff,
    )


def _build_pr_body(
    report: FailureReport,
    root_cause: RootCauseReport,
    workflow_run_url: str,
) -> str:
    """Build the PR body per Requirement 6.4.

    Includes: link to failing CI run, parsed failure summary,
    root cause analysis, confidence level, and AI disclaimer.
    """
    lines: list[str] = []
    primary_failure = (
        report.parsed_failures[0].failure_identifier
        if report.parsed_failures
        else report.job_name
    )

    lines.append("## Fix Summary\n")
    lines.append(
        "This PR proposes an automated fix for "
        f"`{primary_failure}` in `{report.job_name}`.\n"
    )
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(
        f"| Failing CI run | [workflow run]({workflow_run_url}) |"
    )
    lines.append(f"| Workflow | `{report.workflow_name}` |")
    lines.append(f"| Job | `{report.job_name}` |")
    lines.append(f"| Confidence | `{root_cause.confidence}` |")
    targeted_files = ", ".join(f"`{path}`" for path in root_cause.files_to_change)
    lines.append(
        f"| Targeted files | {_escape_table_cell(targeted_files or '_none provided_')} |"
    )
    lines.append("")

    # Parsed failure summary
    lines.append("### Failure Details\n")
    if report.parsed_failures:
        for pf in report.parsed_failures:
            detail = (
                f"- **{pf.failure_identifier}** in `{pf.file_path}`: "
                f"{pf.error_message}"
            )
            if pf.line_number is not None:
                detail += f" (line {pf.line_number})"
            lines.append(detail)
    elif report.is_unparseable:
        lines.append("The failure could not be parsed automatically. Raw log excerpt:\n")
        lines.append(f"```\n{report.raw_log_excerpt or '(no log)'}\n```")
    lines.append("")

    # Root cause analysis
    lines.append("### Root Cause Analysis\n")
    lines.append(root_cause.description)
    lines.append(f"\n**Rationale:** {root_cause.rationale}\n")
    lines.append(
        "**Files targeted:** "
        + (
            ", ".join(f"`{path}`" for path in root_cause.files_to_change)
            or "_none provided_"
        )
        + "\n"
    )

    lines.append("### Reviewer Checklist\n")
    lines.append("- Verify the proposed change matches the failing CI signal.")
    lines.append("- Confirm the generated diff stays within the targeted files.")
    lines.append("- Re-run or inspect validation before merging.\n")

    total_failures = (
        root_cause.total_failure_observations
        if isinstance(root_cause.total_failure_observations, int)
        else 0
    )
    failure_streak = (
        root_cause.failure_streak
        if isinstance(root_cause.failure_streak, int)
        else 0
    )
    last_known_good_sha = (
        root_cause.last_known_good_sha
        if isinstance(root_cause.last_known_good_sha, str)
        else None
    )
    first_bad_sha = (
        root_cause.first_bad_sha
        if isinstance(root_cause.first_bad_sha, str)
        else None
    )

    if total_failures > 0 or failure_streak > 0 or last_known_good_sha or first_bad_sha:
        lines.append("### Failure History\n")
        if total_failures > 0:
            lines.append(f"- Observed failures: {total_failures}")
        if failure_streak > 0:
            lines.append(f"- Consecutive failing runs: {failure_streak}")
        if last_known_good_sha:
            lines.append(f"- Last known good commit: `{last_known_good_sha}`")
        if first_bad_sha:
            lines.append(f"- First bad commit: `{first_bad_sha}`")
        lines.append("")

    # AI disclaimer
    lines.append(
        "### AI Notice\n"
        "This fix was generated by an AI agent and "
        "requires human review. Please verify the changes carefully "
        "before merging."
    )

    return "\n".join(lines)


def _build_workflow_run_url(
    report: FailureReport,
    default_repo_name: str,
) -> str:
    """Build the best available URL back to the originating failure."""
    repo_name = report.repo_full_name or default_repo_name
    if report.workflow_run_id is not None:
        return (
            f"https://github.com/{repo_name}/actions/runs/"
            f"{report.workflow_run_id}"
        )
    if report.commit_sha:
        return f"https://github.com/{repo_name}/commit/{report.commit_sha}"
    return f"https://github.com/{repo_name}"


def _is_permission_denied_for_branch_creation(exc: Exception) -> bool:
    """Return whether branch creation failed due to missing upstream write access."""
    if not isinstance(exc, GithubException):
        return False
    message = str(exc).lower()
    return exc.status in {403, 404} or "resource not accessible" in message


def upsert_pull_request(
    repo: "Repository",
    *,
    head: str,
    base: str,
    title: str,
    body: str,
    draft: bool = False,
    labels: tuple[str, ...] = (),
) -> "PullRequest":
    """Return an existing open PR for ``head``/``base`` or create one."""
    head_filter = head
    if ":" not in head_filter:
        owner = getattr(getattr(repo, "owner", None), "login", "")
        if owner:
            head_filter = f"{owner}:{head_filter}"
    pulls = retry_github_call(
        lambda: repo.get_pulls(state="open", base=base, head=head_filter),
        retries=2,
        description=f"list pull requests for {head_filter}->{base}",
    )
    pr = next(iter(pulls), None)
    if pr is None:
        pr = retry_github_call(
            lambda: repo.create_pull(
                title=title,
                body=body,
                head=head,
                base=base,
                draft=draft,
            ),
            retries=2,
            description=f"create pull request for {head}->{base}",
        )
    else:
        current_title = str(getattr(pr, "title", "") or "")
        current_body = str(getattr(pr, "body", "") or "")
        if current_title != title or current_body != body:
            retry_github_call(
                lambda: pr.edit(title=title, body=body),
                retries=2,
                description=f"update pull request #{pr.number}",
            )
    for label in labels:
        try:
            def _add_label() -> None:
                pr.add_to_labels(label)

            retry_github_call(
                _add_label,
                retries=2,
                description=f"label pull request #{pr.number} with {label}",
            )
        except Exception as exc:
            logger.warning("Could not apply %r label to PR #%s: %s", label, pr.number, exc)
    return pr


class PRManager:
    """Creates branches, commits patches, and opens pull requests."""

    def __init__(
        self,
        github_client: "Github",
        repo_full_name: str,
        failure_store: FailureStore,
        *,
        signer: CommitSigner | None = None,
        require_dco_signoff: bool | None = None,
    ) -> None:
        self._gh = github_client
        self._repo_name = repo_full_name
        self._failure_store = failure_store
        self._signer = signer or load_signer_from_env()
        if require_dco_signoff is None:
            require_dco_signoff = require_dco_signoff_from_env()
        self._require_dco_signoff = require_dco_signoff

    def create_pr(
        self,
        patch: str,
        failure_report: FailureReport,
        root_cause: RootCauseReport,
        target_branch: str,
        *,
        draft: bool = False,
    ) -> str:
        """Create a branch, apply patch, commit, open PR, apply label.

        Only called for trusted same-repository failures that passed validation.

        Returns the PR URL on success.

        Raises:
            ValueError: for fork PR failures (no write access).
            RuntimeError: when GitHub API rejects the operation.
        """
        # Req 6.3 — skip for fork PR failures
        if failure_report.failure_source == "untrusted-fork":
            logger.warning(
                "fork-pr-no-write-access: skipping PR creation for fork failure "
                "job=%s",
                failure_report.job_name,
            )
            raise ValueError("fork-pr-no-write-access")

        fingerprint = _compute_fingerprint(failure_report)
        branch_name = f"bot/fix/{fingerprint}"

        logger.info(
            "PR creation started for job %s (fingerprint %s).",
            failure_report.job_name, fingerprint[:12],
        )

        try:
            repo = self._gh.get_repo(self._repo_name)

            # 1. Create branch from the failing commit SHA and verify the target branch exists.
            repo.get_git_ref(f"heads/{target_branch}")
            base_sha = failure_report.commit_sha
            write_repo = repo
            pr_head = branch_name
            try:
                self._create_branch(write_repo, branch_name, base_sha)
                logger.info(
                    "Created branch %s from commit %s for base %s",
                    branch_name, base_sha[:12], target_branch,
                )
            except Exception as exc:
                if not _is_permission_denied_for_branch_creation(exc):
                    raise
                write_repo, pr_head = self._create_fork_branch(
                    repo,
                    branch_name,
                    base_sha,
                )
                logger.info(
                    "Created fallback fork branch %s in %s for base %s",
                    branch_name,
                    write_repo.full_name,
                    target_branch,
                )

            # 2. Apply patch by creating/updating files via the Git Data API
            #    Parse the unified diff to extract file changes and commit them.
            commit_message = _build_commit_message(
                failure_report,
                root_cause,
                self._signer,
                require_dco_signoff=self._require_dco_signoff,
            )
            self._apply_patch_and_commit(write_repo, branch_name, base_sha, patch, commit_message)

            # 3. Build PR body
            workflow_run_url = _build_workflow_run_url(
                failure_report, self._repo_name,
            )
            pr_body = _build_pr_body(failure_report, root_cause, workflow_run_url)

            # PR title
            if failure_report.parsed_failures:
                pf = failure_report.parsed_failures[0]
                title = f"[bot-fix] Fix {pf.test_name or pf.failure_identifier} in {failure_report.job_name}"
            else:
                title = f"[bot-fix] Fix failure in {failure_report.job_name}"

            # 4. Open PR
            pr = upsert_pull_request(
                repo,
                head=pr_head,
                base=target_branch,
                title=title,
                body=pr_body,
                draft=draft,
                labels=("bot-fix",),
            )
            logger.info("Opened PR #%d: %s", pr.number, pr.html_url)

            # 6. Record in failure store (Req 6.6)
            pr_url = pr.html_url
            if failure_report.parsed_failures:
                pf = failure_report.parsed_failures[0]
                self._failure_store.record(
                    fingerprint,
                    pf.failure_identifier,
                    pf.error_message,
                    pf.file_path,
                    pr_url=pr_url,
                    status="open",
                    test_name=pf.test_name,
                )
            else:
                self._failure_store.record(
                    fingerprint,
                    failure_report.job_name,
                    failure_report.raw_log_excerpt or "",
                    "",
                    pr_url=pr_url,
                    status="open",
                )

            return pr_url

        except ValueError:
            # Re-raise fork-pr-no-write-access
            raise
        except Exception as exc:
            # Req 6.7 — handle GitHub API rejections gracefully
            logger.error(
                "pr-creation-failed: GitHub API error creating PR for job=%s: %s",
                failure_report.job_name,
                exc,
            )
            # Record failure in store
            if failure_report.parsed_failures:
                pf = failure_report.parsed_failures[0]
                self._failure_store.record(
                    fingerprint,
                    pf.failure_identifier,
                    pf.error_message,
                    pf.file_path,
                    status="pr-creation-failed",
                    test_name=pf.test_name,
                )
            else:
                self._failure_store.record(
                    fingerprint,
                    failure_report.job_name,
                    failure_report.raw_log_excerpt or "",
                    "",
                    status="pr-creation-failed",
                )
            raise RuntimeError(f"pr-creation-failed: {exc}") from exc

    def _create_branch(
        self,
        repo: "Repository",
        branch_name: str,
        base_sha: str,
    ) -> None:
        """Create the working branch or reset an existing agent branch to base."""
        try:
            repo.create_git_ref(
                ref=f"refs/heads/{branch_name}",
                sha=base_sha,
            )
        except GithubException as exc:
            if exc.status == 422:
                logger.info(
                    "Branch %s already exists; resetting it to %s before patching.",
                    branch_name,
                    base_sha[:12],
                )
                branch_ref = repo.get_git_ref(f"heads/{branch_name}")
                branch_ref.edit(base_sha, force=True)
                return
            raise

    def _create_fork_branch(
        self,
        upstream_repo: "Repository",
        branch_name: str,
        base_sha: str,
    ) -> tuple["Repository", str]:
        """Create or reuse a writable fork branch and return its PR head spec."""
        fork_repo = upstream_repo.create_fork()
        self._create_branch(fork_repo, branch_name, base_sha)
        owner = getattr(getattr(fork_repo, "owner", None), "login", "")
        if not owner:
            raise RuntimeError("fork repo owner could not be determined")
        return fork_repo, f"{owner}:{branch_name}"

    def post_summary_comment(
        self,
        pr_url: str,
        summary_comment: "PRSummaryComment",
    ) -> None:
        """Post a processing summary comment on a created PR.

        Requirement 11.2: The agent SHALL produce a summary comment on each
        created PR listing the processing steps, time taken, and retries.

        Args:
            pr_url: The HTML URL of the PR (e.g. https://github.com/owner/repo/pull/42).
            summary_comment: A ``PRSummaryComment`` with collected step data.
        """

        body = summary_comment.render()
        try:
            repo = self._gh.get_repo(self._repo_name)
            # Extract PR number from URL
            pr_number = int(pr_url.rstrip("/").split("/")[-1])
            pr = repo.get_pull(pr_number)
            pr.create_issue_comment(body=body)
            logger.info("Posted summary comment on PR #%d.", pr_number)
        except Exception as exc:
            # Non-fatal — the PR was already created successfully
            logger.warning("Failed to post summary comment on %s: %s", pr_url, exc)


    def _apply_patch_and_commit(
        self,
        repo: "Repository",
        branch_name: str,
        base_sha: str,
        patch: str,
        message: str,
    ) -> None:
        """Apply a unified diff patch via the GitHub Git Data API.

        Creates a single tree/commit update so a validated patch lands as one
        commit on the agent branch.
        """
        file_patches = _parse_unified_diff(patch)
        if not file_patches:
            raise ValueError("patch contained no file changes")

        parent_commit = repo.get_git_commit(base_sha)
        tree_elements: list[InputGitTreeElement] = []

        for file_path, file_diff in file_patches.items():
            try:
                def _fetch_contents(fp: str = file_path):  # type: ignore[assignment]
                    return repo.get_contents(fp, ref=branch_name)

                contents = retry_github_call(
                    _fetch_contents,
                    retries=5,
                    description=f"load branch contents for {file_path}",
                )
                if isinstance(contents, list):
                    raise ValueError(f"Patch target {file_path} resolved to a directory.")
                original = contents.decoded_content.decode("utf-8")
            except GithubException as exc:
                if exc.status != 404:
                    raise
                original = ""
            except FileNotFoundError:
                original = ""
            patched = _apply_hunks(original, file_diff)
            tree_elements.append(
                InputGitTreeElement(
                    path=file_path,
                    mode="100644",
                    type="blob",
                    content=patched,
                )
            )

        new_tree = repo.create_git_tree(tree_elements, base_tree=parent_commit.tree)
        new_commit = repo.create_git_commit(message, new_tree, [parent_commit])
        branch_ref = repo.get_git_ref(f"heads/{branch_name}")
        branch_ref.edit(new_commit.sha)


def _parse_unified_diff(patch: str) -> dict[str, list[dict]]:
    """Parse a unified diff into per-file hunk data.

    Returns a dict mapping file paths to lists of hunk dicts, each with:
      - old_start, old_count, new_start, new_count
      - lines: list of diff lines (prefixed with +, -, or space)
    """
    files: dict[str, list[dict]] = {}
    current_file: str | None = None
    current_hunk: dict | None = None

    for line in patch.split("\n"):
        # File headers are not hunk content. In multi-file patches, the
        # ``--- a/path`` header would otherwise look like a removed line.
        if re.match(r"^--- a/(.+)$", line):
            current_hunk = None
            continue

        # Detect target file
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            current_file = m.group(1)
            if current_file not in files:
                files[current_file] = []
            current_hunk = None
            continue

        # Detect hunk header
        hunk_match = re.match(
            r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line
        )
        if hunk_match and current_file is not None:
            current_hunk = {
                "old_start": int(hunk_match.group(1)),
                "old_count": int(hunk_match.group(2) or "1"),
                "new_start": int(hunk_match.group(3)),
                "new_count": int(hunk_match.group(4) or "1"),
                "lines": [],
            }
            files[current_file].append(current_hunk)
            continue

        # Collect hunk lines
        if current_hunk is not None and (
            line.startswith("+") or line.startswith("-") or line.startswith(" ")
        ):
            current_hunk["lines"].append(line)

    return files


def _apply_hunks(original: str, hunks: list[dict]) -> str:
    """Apply parsed hunks to original file content.

    Returns the patched file content.
    """
    if not hunks:
        return original

    original_lines = original.split("\n") if original else []
    result_lines: list[str] = []
    orig_idx = 0  # 0-based index into original_lines

    for hunk in hunks:
        # old_start is 1-based; for new files (@@ -0,0 +1,N @@) it is 0.
        hunk_start = hunk["old_start"] - 1 if hunk["old_start"] > 0 else 0

        if hunk_start > len(original_lines):
            raise ValueError(
                f"Patch hunk starts at line {hunk_start + 1}, "
                f"past end of file with {len(original_lines)} line(s)."
            )

        # Copy lines before this hunk
        while orig_idx < hunk_start and orig_idx < len(original_lines):
            result_lines.append(original_lines[orig_idx])
            orig_idx += 1

        # Apply hunk lines
        for diff_line in hunk["lines"]:
            if diff_line.startswith("+"):
                result_lines.append(diff_line[1:])
            elif diff_line.startswith("-"):
                expected = diff_line[1:]
                if orig_idx >= len(original_lines):
                    raise ValueError(
                        "Patch deletion exceeded original file length: "
                        f"expected {expected!r}."
                    )
                if original_lines[orig_idx] != expected:
                    raise ValueError(
                        "Patch deletion mismatch at line "
                        f"{orig_idx + 1}: expected {expected!r}, "
                        f"got {original_lines[orig_idx]!r}."
                    )
                orig_idx += 1
            elif diff_line.startswith(" "):
                expected = diff_line[1:]
                if orig_idx >= len(original_lines):
                    raise ValueError(
                        "Patch context exceeded original file length: "
                        f"expected {expected!r}."
                    )
                if original_lines[orig_idx] != expected:
                    raise ValueError(
                        "Patch context mismatch at line "
                        f"{orig_idx + 1}: expected {expected!r}, "
                        f"got {original_lines[orig_idx]!r}."
                    )
                result_lines.append(expected)
                orig_idx += 1

    # Copy remaining lines after last hunk
    while orig_idx < len(original_lines):
        result_lines.append(original_lines[orig_idx])
        orig_idx += 1

    return "\n".join(result_lines)

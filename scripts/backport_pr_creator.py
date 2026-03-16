"""Backport PR creator for the Backport Agent pipeline.

Creates backport branches and pull requests via the GitHub API, with
duplicate detection and structured PR body generation.
"""

from __future__ import annotations

import logging

from github import Github

from scripts.backport_models import (
    BackportPRContext,
    CherryPickResult,
    ResolutionResult,
)
from scripts.backport_utils import build_branch_name, build_pr_title
from scripts.github_client import retry_github_call

logger = logging.getLogger(__name__)


class BackportPRCreator:
    """Create backport branches and pull requests via the GitHub API."""

    def __init__(self, github_client: Github, repo_full_name: str) -> None:
        self._github = github_client
        self._repo_full_name = repo_full_name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_backport_pr(
        self,
        context: BackportPRContext,
        cherry_pick_result: CherryPickResult,
        resolution_results: list[ResolutionResult] | None,
        branch_name: str | None = None,
    ) -> str:
        """Create backport PR from an already-pushed branch.

        If *branch_name* is provided, the branch is assumed to already
        exist on the remote (pushed from the local cherry-pick clone).
        Otherwise, falls back to creating the branch via the API from
        target branch HEAD (useful for testing).

        Returns the PR URL.

        **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7**
        """
        repo = retry_github_call(
            lambda: self._github.get_repo(self._repo_full_name),
            retries=3,
            description=f"get repo {self._repo_full_name}",
        )

        if branch_name is None:
            branch_name = build_branch_name(
                context.source_pr_number, context.target_branch,
            )
        title = build_pr_title(context.source_pr_title, context.target_branch)

        had_conflicts = not cherry_pick_result.success
        any_llm_resolved = bool(
            resolution_results
            and any(r.resolved_content is not None for r in resolution_results)
        )

        body = self.build_pr_body(context, had_conflicts, resolution_results)

        # Open the pull request (branch already exists on remote).
        logger.info(
            "Opening backport PR: %s -> %s", branch_name, context.target_branch,
        )
        pr = retry_github_call(
            lambda: repo.create_pull(
                title=title,
                body=body,
                head=branch_name,
                base=context.target_branch,
            ),
            retries=3,
            description="create backport PR",
        )

        # Apply labels.
        labels = ["backport"]
        if any_llm_resolved:
            labels.append("llm-resolved-conflicts")

        logger.info("Applying labels %s to PR #%d", labels, pr.number)
        retry_github_call(
            lambda: pr.add_to_labels(*labels),
            retries=3,
            description="apply labels to backport PR",
        )

        logger.info("Backport PR created: %s", pr.html_url)
        return pr.html_url

    @staticmethod
    def build_pr_body(
        context: BackportPRContext,
        had_conflicts: bool,
        resolution_results: list[ResolutionResult] | None,
    ) -> str:
        """Build the PR body with links, commit list, conflict info.

        Includes:
        * Link to the source PR
        * List of cherry-picked commit SHAs
        * Whether conflicts were encountered
        * Per-file LLM resolution summaries (when applicable)
        * Human review disclaimer (when any file was LLM-resolved)

        **Validates: Requirements 4.4, 5.3**
        """
        sections: list[str] = []

        # Source PR link.
        sections.append(
            f"## Source Pull Request\n\n"
            f"Backport of {context.source_pr_url} (#{context.source_pr_number})"
        )

        # Cherry-picked commits.
        commits_list = "\n".join(
            f"- `{sha}`" for sha in context.commits
        )
        sections.append(
            f"## Cherry-Picked Commits\n\n{commits_list}"
        )

        # Conflict status.
        if had_conflicts:
            sections.append(
                "## Conflict Status\n\n"
                "⚠️ Cherry-pick produced merge conflicts."
            )
        else:
            sections.append(
                "## Conflict Status\n\n"
                "✅ Cherry-pick applied cleanly — no conflicts."
            )

        # Per-file resolution summaries.
        if resolution_results:
            file_lines: list[str] = []
            for result in resolution_results:
                status = (
                    "✅ Resolved" if result.resolved_content is not None
                    else "❌ Unresolved"
                )
                file_lines.append(
                    f"- **`{result.path}`**: {status} — {result.resolution_summary}"
                )
            sections.append(
                "## Conflict Resolution Details\n\n" + "\n".join(file_lines)
            )

        # Human review disclaimer (when any file was LLM-resolved).
        any_llm_resolved = bool(
            resolution_results
            and any(r.resolved_content is not None for r in resolution_results)
        )
        if any_llm_resolved:
            sections.append(
                "## ⚠️ LLM-Resolved Conflicts — Human Review Required\n\n"
                "Some conflicts in this backport were resolved using an LLM. "
                "These resolutions require careful human review to ensure "
                "correctness. Please verify that the resolved code matches "
                "the intent of the original pull request."
            )

        return "\n\n".join(sections)

    def check_duplicate(
        self,
        source_pr_number: int,
        target_branch: str,
    ) -> str | None:
        """Return existing backport PR URL if one exists, else ``None``.

        Checks for open PRs whose head branch matches the naming
        convention ``backport/<pr>-to-<branch>``.  Also checks recently
        closed PRs to handle label removal and re-addition.

        **Validates: Requirements 6.1, 6.2, 6.3**
        """
        branch_name = build_branch_name(source_pr_number, target_branch)

        repo = retry_github_call(
            lambda: self._github.get_repo(self._repo_full_name),
            retries=3,
            description=f"get repo {self._repo_full_name}",
        )

        # Check open PRs with matching head branch.
        logger.info(
            "Checking for duplicate backport PR with head branch %s",
            branch_name,
        )
        open_pulls = retry_github_call(
            lambda: repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch_name}"),
            retries=3,
            description="search open PRs for duplicate",
        )
        for pr in open_pulls:
            logger.info("Found existing open backport PR: %s", pr.html_url)
            return pr.html_url

        # Check recently closed PRs with matching head branch.
        closed_pulls = retry_github_call(
            lambda: repo.get_pulls(state="closed", head=f"{repo.owner.login}:{branch_name}"),
            retries=3,
            description="search closed PRs for duplicate",
        )
        for pr in closed_pulls:
            logger.info(
                "Found existing closed backport PR: %s", pr.html_url,
            )
            return pr.html_url

        logger.info("No duplicate backport PR found for %s", branch_name)
        return None

"""Cherry-pick executor for the Backport Bot pipeline.

Handles git operations for cherry-picking source PR commits onto a target
release branch.  Uses ``subprocess.run`` for all git CLI calls.
"""

from __future__ import annotations

import logging
import subprocess

from scripts.backport_models import CherryPickResult, ConflictedFile

logger = logging.getLogger(__name__)


class CherryPickExecutor:
    """Execute cherry-pick operations on a local git repository."""

    def __init__(self, repo_dir: str) -> None:
        self._repo_dir = repo_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        target_branch: str,
        merge_commit_sha: str | None,
        commit_shas: list[str],
    ) -> CherryPickResult:
        """Cherry-pick commits onto *target_branch*.

        Prefers *merge_commit_sha* when available (single operation via
        ``git cherry-pick -m 1``).  Falls back to sequential cherry-pick
        of individual *commit_shas*.

        **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**
        """
        logger.info("Checking out target branch %s", target_branch)
        self._run_git("checkout", target_branch)

        if merge_commit_sha:
            return self._cherry_pick_merge(target_branch, merge_commit_sha)
        return self._cherry_pick_sequential(target_branch, commit_shas)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cherry_pick_merge(
        self,
        target_branch: str,
        merge_commit_sha: str,
    ) -> CherryPickResult:
        """Cherry-pick a merge commit using ``-m 1``."""
        logger.info(
            "Cherry-picking merge commit %s onto %s",
            merge_commit_sha,
            target_branch,
        )
        result = self._run_git(
            "cherry-pick", "-m", "1", merge_commit_sha, check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "Cherry-pick of merge commit %s produced conflicts",
                merge_commit_sha,
            )
            conflicts = self._collect_conflicts(target_branch)

            # Empty cherry-pick: non-zero exit but no unmerged files means
            # the changes already exist on the target branch.  Abort the
            # cherry-pick and retry with --allow-empty so the branch has a
            # commit that can be pushed.
            if not conflicts:
                logger.info(
                    "No conflicting files — cherry-pick is empty. "
                    "Retrying with --allow-empty.",
                )
                self._run_git("cherry-pick", "--abort", check=False)
                retry = self._run_git(
                    "cherry-pick", "-m", "1", "--allow-empty",
                    merge_commit_sha, check=False,
                )
                if retry.returncode == 0:
                    logger.info(
                        "Empty cherry-pick of %s succeeded with --allow-empty",
                        merge_commit_sha,
                    )
                    return CherryPickResult(
                        success=True,
                        applied_commits=[merge_commit_sha],
                    )
                # If --allow-empty also fails, fall through to conflict path
                logger.warning(
                    "Retry with --allow-empty also failed for %s",
                    merge_commit_sha,
                )
                conflicts = self._collect_conflicts(target_branch)

            return CherryPickResult(
                success=False,
                conflicting_files=conflicts,
                applied_commits=[merge_commit_sha],
            )
        logger.info("Cherry-pick of merge commit %s succeeded", merge_commit_sha)
        return CherryPickResult(
            success=True,
            applied_commits=[merge_commit_sha],
        )

    def _cherry_pick_sequential(
        self,
        target_branch: str,
        commit_shas: list[str],
    ) -> CherryPickResult:
        """Cherry-pick each commit SHA sequentially."""
        applied: list[str] = []
        for sha in commit_shas:
            logger.info("Cherry-picking commit %s onto %s", sha, target_branch)
            result = self._run_git("cherry-pick", sha, check=False)
            if result.returncode != 0:
                logger.warning(
                    "Cherry-pick of commit %s produced conflicts", sha,
                )
                conflicts = self._collect_conflicts(target_branch)
                applied.append(sha)
                return CherryPickResult(
                    success=False,
                    conflicting_files=conflicts,
                    applied_commits=applied,
                )
            applied.append(sha)
        logger.info("All %d commits cherry-picked cleanly", len(applied))
        return CherryPickResult(success=True, applied_commits=applied)

    def _collect_conflicts(self, target_branch: str) -> list[ConflictedFile]:
        """Gather conflict information for all unmerged files."""
        result = self._run_git(
            "diff", "--name-only", "--diff-filter=U",
        )
        paths = [p for p in result.stdout.strip().splitlines() if p]
        logger.info("Found %d conflicting file(s): %s", len(paths), paths)

        conflicts: list[ConflictedFile] = []
        for path in paths:
            conflicts.append(self._build_conflicted_file(target_branch, path))
        return conflicts

    def _build_conflicted_file(
        self,
        target_branch: str,
        file_path: str,
    ) -> ConflictedFile:
        """Read conflict markers, target-branch version, and source version."""
        # Working-directory copy contains conflict markers
        content_with_markers = self._read_working_file(file_path)

        # Target branch version (before cherry-pick)
        target_branch_content = self._show_file(target_branch, file_path)

        # Source branch version (the commit being cherry-picked)
        source_branch_content = self._show_file("CHERRY_PICK_HEAD", file_path)

        return ConflictedFile(
            path=file_path,
            content_with_markers=content_with_markers,
            target_branch_content=target_branch_content,
            source_branch_content=source_branch_content,
        )

    def _read_working_file(self, file_path: str) -> str:
        """Read a file from the working directory."""
        import os

        full_path = os.path.join(self._repo_dir, file_path)
        with open(full_path, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def _show_file(self, ref: str, file_path: str) -> str:
        """Return file content at *ref* via ``git show``."""
        result = self._run_git("show", f"{ref}:{file_path}", check=False)
        if result.returncode != 0:
            logger.warning(
                "Could not read %s:%s — %s",
                ref,
                file_path,
                result.stderr.strip(),
            )
            return ""
        return result.stdout

    def _run_git(
        self,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command inside the repository directory."""
        cmd = ["git", *args]
        logger.debug("Running: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=self._repo_dir,
            check=False,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                cmd,
                output=result.stdout,
                stderr=result.stderr,
            )
        return result

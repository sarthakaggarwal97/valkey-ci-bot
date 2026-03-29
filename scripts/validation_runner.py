"""Validation runner for the CI Failure Bot.

Checks out the consumer repo at the target commit SHA, applies the
generated patch, builds with the matching CI configuration, and runs
the specific failing test(s) — all within the bot's own workflow
environment.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from scripts.config import BotConfig, ValidationProfile
from scripts.models import FailureReport, ValidationResult

logger = logging.getLogger(__name__)

_DEFAULT_FALLBACK_PROFILE = ValidationProfile(
    job_name_pattern=".*",
    build_commands=["make"],
    test_commands=[],
)


def _match_profile(
    job_name: str,
    matrix_params: dict[str, str],
    profiles: list[ValidationProfile],
) -> ValidationProfile | None:
    """Select the first ValidationProfile whose job_name_pattern matches
    the job name and whose matrix_params are a subset of the job's params.

    Returns None if no profile matches.
    """
    for profile in profiles:
        if not profile.job_name_pattern:
            continue
        try:
            if not re.search(profile.job_name_pattern, job_name):
                continue
        except re.error:
            logger.warning(
                "Invalid regex in validation profile: %s",
                profile.job_name_pattern,
            )
            continue

        # Check that all profile matrix_params match the job's params
        if profile.matrix_params:
            if not all(
                matrix_params.get(k) == v
                for k, v in profile.matrix_params.items()
            ):
                continue

        return profile

    return None


def _run_commands(
    commands: list[str],
    cwd: str | Path,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> tuple[bool, str]:
    """Execute a list of shell commands sequentially.

    Returns (all_passed, combined_output).

    .. note::

       Commands are executed with ``shell=True`` because they originate
       from YAML configuration authored by the repository owner (the
       ``validation_profiles`` section).  The trust boundary is the
       config file author — do not pass untrusted input as commands.
    """
    merged_env = {**os.environ}
    if env:
        merged_env.update(env)

    output_parts: list[str] = []
    for cmd in commands:
        logger.info("Running: %s", cmd)
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=merged_env,
            )
            combined = result.stdout + result.stderr
            output_parts.append(f"$ {cmd}\n{combined}")
            if result.returncode != 0:
                logger.warning("Command failed (rc=%d): %s", result.returncode, cmd)
                return False, "\n".join(output_parts)
        except subprocess.TimeoutExpired:
            output_parts.append(f"$ {cmd}\nTIMEOUT after {timeout}s")
            return False, "\n".join(output_parts)
        except OSError as exc:
            output_parts.append(f"$ {cmd}\nERROR: {exc}")
            return False, "\n".join(output_parts)

    return True, "\n".join(output_parts)


def _substitute_test_commands(
    commands: list[str],
    failure_report: FailureReport,
) -> list[str]:
    """Replace placeholders in test commands with actual failing test info.

    Supported placeholders:
    - {test_name}: the first parsed failure's test_name or failure_identifier
    - {file_path}: the first parsed failure's file_path
    - {parser_type}: the first parsed failure's parser type
    """
    if not failure_report.parsed_failures:
        return commands

    first = failure_report.parsed_failures[0]
    test_name = first.test_name or first.failure_identifier
    file_path = first.file_path
    parser_type = first.parser_type

    return [
        cmd
        .replace("{test_name}", test_name)
        .replace("{file_path}", file_path)
        .replace("{parser_type}", parser_type)
        for cmd in commands
    ]


class ValidationRunner:
    """Orchestrates build + test validation of a proposed fix.

    The validation runs within the bot's own workflow environment:
    1. Checks out the consumer repo at the target commit SHA
    2. Applies the generated patch via ``git apply``
    3. Selects a ValidationProfile from config using job name and matrix params
    4. Builds the project with matching configuration
    5. Runs the specific failing test(s), or only the build when build-only
    """

    def __init__(
        self,
        config: BotConfig,
        *,
        repo_clone_url: str | None = None,
        github_client: Any | None = None,
        repo_full_name: str = "",
    ):
        self._config = config
        self._repo_clone_url = repo_clone_url
        self._github_client = github_client
        self._repo_full_name = repo_full_name

    def validate(
        self,
        patch: str,
        failure_report: FailureReport,
        *,
        repeat_count: int = 1,
    ) -> ValidationResult:
        """Run validation for a proposed fix.

        Args:
            patch: Unified diff string to apply.
            failure_report: The failure report describing the failing job.

        Returns:
            ValidationResult with pass/fail status and output.
        """
        # Skip untrusted fork failures
        if failure_report.failure_source == "untrusted-fork":
            logger.warning(
                "Skipping validation for untrusted fork failure: job=%s",
                failure_report.job_name,
            )
            return ValidationResult(
                passed=False,
                output="untrusted-fork",
                strategy="local",
            )
        requested_runs = max(1, repeat_count)
        outputs: list[str] = []
        for run_number in range(1, requested_runs + 1):
            result = self._validate_once(patch, failure_report)
            outputs.append(f"[run {run_number}/{requested_runs}]\n{result.output}")
            if not result.passed:
                return ValidationResult(
                    passed=False,
                    output="\n\n".join(outputs),
                    strategy="local",
                    passed_runs=run_number - 1,
                    attempted_runs=run_number,
                )
        return ValidationResult(
            passed=True,
            output="\n\n".join(outputs),
            strategy="local",
            passed_runs=requested_runs,
            attempted_runs=requested_runs,
        )

    def _validate_once(
        self,
        patch: str,
        failure_report: FailureReport,
    ) -> ValidationResult:
        """Run a single clean validation pass for the proposed fix."""
        logger.info(
            "Validation started for job %s (commit %s).",
            failure_report.job_name, failure_report.commit_sha[:12],
        )

        profile = _match_profile(
            failure_report.job_name,
            failure_report.matrix_params,
            self._config.validation_profiles,
        )
        if profile is None:
            logger.warning(
                "No validation profile matches job '%s' with params %s. "
                "Using default fallback profile. Consider configuring a "
                "specific profile for this job.",
                failure_report.job_name,
                failure_report.matrix_params,
            )
            profile = _DEFAULT_FALLBACK_PROFILE

        logger.info(
            "Validation profile matched: pattern=%s for job '%s'.",
            profile.job_name_pattern, failure_report.job_name,
        )

        with tempfile.TemporaryDirectory(prefix="ci-bot-validate-") as tmpdir:
            work_dir = Path(tmpdir) / "repo"

            clone_ok, clone_output = self._checkout_repo(
                failure_report.commit_sha, work_dir
            )
            if not clone_ok:
                return ValidationResult(
                    passed=False,
                    output=clone_output,
                    strategy="local",
                    attempted_runs=1,
                )

            apply_ok, apply_output = self._apply_patch(patch, work_dir)
            if not apply_ok:
                return ValidationResult(
                    passed=False,
                    output=apply_output,
                    strategy="local",
                    attempted_runs=1,
                )

            if profile.install_commands:
                install_ok, install_output = _run_commands(
                    profile.install_commands, work_dir, env=profile.env
                )
                if not install_ok:
                    return ValidationResult(
                        passed=False,
                        output=f"Install failed:\n{install_output}",
                        strategy="local",
                        attempted_runs=1,
                    )

            if profile.build_commands:
                build_ok, build_output = _run_commands(
                    profile.build_commands, work_dir, env=profile.env
                )
                if not build_ok:
                    return ValidationResult(
                        passed=False,
                        output=f"Build failed:\n{build_output}",
                        strategy="local",
                        attempted_runs=1,
                    )
            else:
                build_output = ""

            if profile.test_commands:
                test_cmds = _substitute_test_commands(
                    profile.test_commands, failure_report
                )
                test_ok, test_output = _run_commands(
                    test_cmds, work_dir, env=profile.env
                )
                if not test_ok:
                    return ValidationResult(
                        passed=False,
                        output=f"Tests failed:\n{test_output}",
                        strategy="local",
                        attempted_runs=1,
                    )
            else:
                test_output = ""
                logger.info(
                    "No test commands in profile — build-only validation for '%s'.",
                    failure_report.job_name,
                )

        combined = "\n".join(
            part for part in [build_output, test_output] if part
        )
        logger.info(
            "Validation complete for job %s: passed.",
            failure_report.job_name,
        )
        return ValidationResult(
            passed=True,
            output=combined or "Validation passed.",
            strategy="local",
            passed_runs=1,
            attempted_runs=1,
        )

    def _checkout_repo(
        self, commit_sha: str, work_dir: Path
    ) -> tuple[bool, str]:
        """Clone the consumer repo and check out the target SHA."""
        clone_url = self._repo_clone_url
        if not clone_url:
            return False, "No repository clone URL configured."

        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", clone_url, str(work_dir)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                return False, f"Clone failed:\n{result.stderr}"

            # Fetch the specific commit (shallow clone may not have it)
            fetch_result = subprocess.run(
                ["git", "fetch", "origin", commit_sha],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(work_dir),
            )
            if fetch_result.returncode != 0:
                return False, f"Fetch SHA failed:\n{fetch_result.stderr}"

            checkout_result = subprocess.run(
                ["git", "checkout", commit_sha],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(work_dir),
            )
            if checkout_result.returncode != 0:
                return False, f"Checkout failed:\n{checkout_result.stderr}"

            return True, ""
        except subprocess.TimeoutExpired:
            return False, "Repository checkout timed out."
        except OSError as exc:
            return False, f"Checkout error: {exc}"

    def _apply_patch(
        self, patch: str, work_dir: Path
    ) -> tuple[bool, str]:
        """Apply a unified diff patch to the working directory."""
        try:
            result = subprocess.run(
                ["git", "apply"],
                input=patch,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(work_dir),
            )
            if result.returncode != 0:
                return False, f"Patch apply failed:\n{result.stderr}"
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "Patch apply timed out."
        except OSError as exc:
            return False, f"Patch apply error: {exc}"

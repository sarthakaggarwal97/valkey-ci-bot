"""Tests for the validation runner module."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest

from scripts.config import BotConfig, ValidationProfile
from scripts.models import FailureReport, ParsedFailure, ValidationResult
from scripts.validation_runner import (
    ValidationRunner,
    _match_profile,
    _run_commands,
    _substitute_test_commands,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_failure_report(**overrides) -> FailureReport:
    defaults = dict(
        workflow_name="CI",
        job_name="test-sanitizer-address",
        matrix_params={"os": "ubuntu-latest"},
        commit_sha="abc123def456",
        failure_source="trusted",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="TestSuite.TestCase",
                test_name="TestSuite.TestCase",
                file_path="src/server.c",
                error_message="assertion failed",
                assertion_details=None,
                line_number=42,
                stack_trace=None,
                parser_type="gtest",
            )
        ],
    )
    defaults.update(overrides)
    return FailureReport(**defaults)


def _make_profile(**overrides) -> ValidationProfile:
    defaults = dict(
        job_name_pattern="^test-sanitizer-address$",
        matrix_params={},
        env={"SANITIZER": "address"},
        install_commands=[],
        build_commands=["make -j BUILD_TLS=no SANITIZER=address"],
        test_commands=["./runtest --test {file_path}"],
    )
    defaults.update(overrides)
    return ValidationProfile(**defaults)


def _make_config(profiles: list[ValidationProfile] | None = None) -> BotConfig:
    return BotConfig(
        validation_profiles=profiles or [_make_profile()],
    )


# ---------------------------------------------------------------------------
# _match_profile
# ---------------------------------------------------------------------------

class TestMatchProfile:
    def test_matches_exact_job_name(self):
        profile = _make_profile(job_name_pattern="^test-sanitizer-address$")
        result = _match_profile("test-sanitizer-address", {}, [profile])
        assert result is profile

    def test_matches_regex_pattern(self):
        profile = _make_profile(job_name_pattern="test-sanitizer-.*")
        result = _match_profile("test-sanitizer-address", {}, [profile])
        assert result is profile

    def test_no_match_returns_none(self):
        profile = _make_profile(job_name_pattern="^test-tls$")
        result = _match_profile("test-sanitizer-address", {}, [profile])
        assert result is None

    def test_matches_first_profile(self):
        p1 = _make_profile(job_name_pattern="test-sanitizer-.*", env={"SANITIZER": "address"})
        p2 = _make_profile(job_name_pattern="test-.*", env={"GENERIC": "yes"})
        result = _match_profile("test-sanitizer-address", {}, [p1, p2])
        assert result is p1

    def test_matrix_params_must_match(self):
        profile = _make_profile(
            job_name_pattern="test-.*",
            matrix_params={"os": "ubuntu-latest"},
        )
        result = _match_profile("test-unit", {"os": "macos-latest"}, [profile])
        assert result is None

    def test_matrix_params_subset_match(self):
        profile = _make_profile(
            job_name_pattern="test-.*",
            matrix_params={"os": "ubuntu-latest"},
        )
        result = _match_profile(
            "test-unit",
            {"os": "ubuntu-latest", "arch": "x64"},
            [profile],
        )
        assert result is profile

    def test_empty_profiles_returns_none(self):
        result = _match_profile("test-unit", {}, [])
        assert result is None

    def test_empty_pattern_skipped(self):
        profile = _make_profile(job_name_pattern="")
        result = _match_profile("test-unit", {}, [profile])
        assert result is None

    def test_invalid_regex_skipped(self):
        profile = _make_profile(job_name_pattern="[invalid")
        good = _make_profile(job_name_pattern="test-.*")
        result = _match_profile("test-unit", {}, [profile, good])
        assert result is good


# ---------------------------------------------------------------------------
# _substitute_test_commands
# ---------------------------------------------------------------------------

class TestSubstituteTestCommands:
    def test_replaces_test_name(self):
        report = _make_failure_report()
        cmds = _substitute_test_commands(
            ["./run --test {test_name}"], report
        )
        assert cmds == ["./run --test TestSuite.TestCase"]

    def test_replaces_file_path(self):
        report = _make_failure_report()
        cmds = _substitute_test_commands(
            ["./run --file {file_path}"], report
        )
        assert cmds == ["./run --file src/server.c"]

    def test_replaces_parser_type(self):
        report = _make_failure_report()
        cmds = _substitute_test_commands(
            ["./run --kind {parser_type}"], report
        )
        assert cmds == ["./run --kind gtest"]

    def test_no_parsed_failures_returns_unchanged(self):
        report = _make_failure_report(parsed_failures=[])
        cmds = _substitute_test_commands(["./run --test {test_name}"], report)
        assert cmds == ["./run --test {test_name}"]

    def test_no_placeholders_unchanged(self):
        report = _make_failure_report()
        cmds = _substitute_test_commands(["make test"], report)
        assert cmds == ["make test"]

    def test_uses_failure_identifier_when_no_test_name(self):
        report = _make_failure_report(
            parsed_failures=[
                ParsedFailure(
                    failure_identifier="build:test-unit:src/foo.c:10",
                    test_name=None,
                    file_path="src/foo.c",
                    error_message="error",
                    assertion_details=None,
                    line_number=10,
                    stack_trace=None,
                    parser_type="build",
                )
            ]
        )
        cmds = _substitute_test_commands(["./run --test {test_name}"], report)
        assert cmds == ["./run --test build:test-unit:src/foo.c:10"]


# ---------------------------------------------------------------------------
# ValidationRunner.validate — untrusted fork
# ---------------------------------------------------------------------------

class TestUntrustedForkSkip:
    def test_skips_untrusted_fork(self):
        config = _make_config()
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report(failure_source="untrusted-fork")

        result = runner.validate("some patch", report)

        assert result.passed is False
        assert result.output == "untrusted-fork"

    def test_trusted_proceeds(self):
        """Trusted failures don't get the untrusted-fork short-circuit."""
        config = _make_config()
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report(failure_source="trusted")

        with patch.object(runner, "_checkout_repo", return_value=(True, "")), \
             patch.object(runner, "_apply_patch", return_value=(True, "")), \
             patch(
                 "scripts.validation_runner._run_commands",
                 return_value=(True, "OK"),
             ):
            result = runner.validate("some patch", report)

        assert result.passed is True


# ---------------------------------------------------------------------------
# ValidationRunner.validate — no matching profile
# ---------------------------------------------------------------------------

class TestNoMatchingProfile:
    def test_missing_profile_fails_closed_by_default(self):
        config = _make_config(profiles=[
            _make_profile(job_name_pattern="^completely-different$"),
        ])
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report(job_name="test-sanitizer-address")

        result = runner.validate("some patch", report)

        assert result.passed is False
        assert result.strategy == "missing-validation-profile"
        assert "missing-validation-profile" in result.output

    def test_uses_fallback_when_explicitly_allowed(self):
        config = _make_config(profiles=[
            _make_profile(job_name_pattern="^completely-different$"),
        ])
        config.require_validation_profile = False
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report(job_name="test-sanitizer-address")

        with patch.object(runner, "_checkout_repo", return_value=(True, "")), \
             patch.object(runner, "_apply_patch", return_value=(True, "")), \
             patch(
                 "scripts.validation_runner._run_commands",
                 return_value=(True, "build OK"),
             ):
            result = runner.validate("some patch", report)

        assert result.passed is True


# ---------------------------------------------------------------------------
# ValidationRunner.validate — full pipeline
# ---------------------------------------------------------------------------

class TestValidationPipeline:
    def test_full_success(self):
        """All steps succeed → ValidationResult.passed is True."""
        config = _make_config()
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report()

        with patch.object(runner, "_checkout_repo", return_value=(True, "")), \
             patch.object(runner, "_apply_patch", return_value=(True, "")), \
             patch(
                 "scripts.validation_runner._run_commands",
                 return_value=(True, "build/test output"),
             ):
            result = runner.validate("diff content", report)

        assert result.passed is True

    def test_clone_failure(self):
        config = _make_config()
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report()

        with patch.object(
            runner, "_checkout_repo",
            return_value=(False, "Clone failed:\nfatal: repo not found"),
        ):
            result = runner.validate("diff content", report)

        assert result.passed is False
        assert "Clone failed" in result.output

    def test_patch_apply_failure(self):
        config = _make_config()
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report()

        with patch.object(runner, "_checkout_repo", return_value=(True, "")), \
             patch.object(
                 runner, "_apply_patch",
                 return_value=(False, "Patch apply failed:\nerror: patch does not apply"),
             ):
            result = runner.validate("bad diff", report)

        assert result.passed is False
        assert "Patch apply failed" in result.output

    def test_build_failure(self):
        config = _make_config()
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report()

        call_count = 0

        def mock_run_commands(cmds, cwd, env=None, timeout=600):
            nonlocal call_count
            call_count += 1
            # Build commands fail
            return (False, "make: *** Error 2")

        with patch.object(runner, "_checkout_repo", return_value=(True, "")), \
             patch.object(runner, "_apply_patch", return_value=(True, "")), \
             patch(
                 "scripts.validation_runner._run_commands",
                 side_effect=mock_run_commands,
             ):
            result = runner.validate("diff content", report)

        assert result.passed is False
        assert "Build failed" in result.output

    def test_test_failure(self):
        config = _make_config()
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report()

        call_count = 0

        def mock_run_commands(cmds, cwd, env=None, timeout=600):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Build succeeds
                return (True, "build OK")
            # Tests fail
            return (False, "FAIL: TestSuite.TestCase")

        with patch.object(runner, "_checkout_repo", return_value=(True, "")), \
             patch.object(runner, "_apply_patch", return_value=(True, "")), \
             patch(
                 "scripts.validation_runner._run_commands",
                 side_effect=mock_run_commands,
             ):
            result = runner.validate("diff content", report)

        assert result.passed is False
        assert "Tests failed" in result.output

    def test_build_only_profile(self):
        """Profile with no test_commands → build-only validation."""
        profile = _make_profile(test_commands=[])
        config = _make_config(profiles=[profile])
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report()

        with patch.object(runner, "_checkout_repo", return_value=(True, "")), \
             patch.object(runner, "_apply_patch", return_value=(True, "")), \
             patch(
                 "scripts.validation_runner._run_commands",
                 return_value=(True, "build OK"),
             ):
            result = runner.validate("diff content", report)

        assert result.passed is True

    def test_no_clone_url_fails(self):
        config = _make_config()
        runner = ValidationRunner(config)
        report = _make_failure_report()

        result = runner.validate("diff content", report)

        assert result.passed is False
        assert "No repository clone URL" in result.output

    def test_install_commands_run_before_build(self):
        """Install commands are executed before build commands."""
        profile = _make_profile(
            install_commands=["apt-get install -y libssl-dev"],
        )
        config = _make_config(profiles=[profile])
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report()

        call_order: list[str] = []

        def mock_run_commands(cmds, cwd, env=None, timeout=600):
            call_order.append(cmds[0] if cmds else "empty")
            return (True, "OK")

        with patch.object(runner, "_checkout_repo", return_value=(True, "")), \
             patch.object(runner, "_apply_patch", return_value=(True, "")), \
             patch(
                 "scripts.validation_runner._run_commands",
                 side_effect=mock_run_commands,
             ):
            result = runner.validate("diff content", report)

        assert result.passed is True
        assert call_order[0] == "apt-get install -y libssl-dev"

    def test_install_failure_stops_pipeline(self):
        profile = _make_profile(
            install_commands=["apt-get install -y missing-pkg"],
        )
        config = _make_config(profiles=[profile])
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report()

        call_count = 0

        def mock_run_commands(cmds, cwd, env=None, timeout=600):
            nonlocal call_count
            call_count += 1
            return (False, "E: Unable to locate package")

        with patch.object(runner, "_checkout_repo", return_value=(True, "")), \
             patch.object(runner, "_apply_patch", return_value=(True, "")), \
             patch(
                 "scripts.validation_runner._run_commands",
                 side_effect=mock_run_commands,
             ):
            result = runner.validate("diff content", report)

        assert result.passed is False
        assert "Install failed" in result.output
        # Only install was called, build/test should not run
        assert call_count == 1

    def test_env_from_profile_is_passed(self):
        """Profile env vars are passed to build and test commands."""
        profile = _make_profile(
            env={"SANITIZER": "address", "BUILD_TLS": "no"},
        )
        config = _make_config(profiles=[profile])
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report()

        captured_envs: list[dict[str, str] | None] = []

        def mock_run_commands(cmds, cwd, env=None, timeout=600):
            captured_envs.append(env)
            return (True, "OK")

        with patch.object(runner, "_checkout_repo", return_value=(True, "")), \
             patch.object(runner, "_apply_patch", return_value=(True, "")), \
             patch(
                 "scripts.validation_runner._run_commands",
                 side_effect=mock_run_commands,
             ):
            runner.validate("diff content", report)

        # Both build and test calls should receive the profile env
        for env in captured_envs:
            assert env is not None
            assert env.get("SANITIZER") == "address"
            assert env.get("BUILD_TLS") == "no"

    def test_repeat_count_requires_multiple_clean_runs(self):
        config = _make_config()
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report()

        with patch.object(
            runner,
            "_validate_once",
            side_effect=[
                ValidationResult(passed=True, output="ok-1"),
                ValidationResult(passed=True, output="ok-2"),
                ValidationResult(passed=True, output="ok-3"),
            ],
        ) as validate_once:
            result = runner.validate("diff content", report, repeat_count=3)

        assert result.passed is True
        assert result.passed_runs == 3
        assert result.attempted_runs == 3
        assert validate_once.call_count == 3
        assert "[run 3/3]" in result.output

    def test_repeat_count_stops_after_first_failed_run(self):
        config = _make_config()
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report()

        with patch.object(
            runner,
            "_validate_once",
            side_effect=[
                ValidationResult(passed=True, output="ok-1"),
                ValidationResult(passed=False, output="boom"),
                ValidationResult(passed=True, output="should-not-run"),
            ],
        ) as validate_once:
            result = runner.validate("diff content", report, repeat_count=3)

        assert result.passed is False
        assert result.passed_runs == 1
        assert result.attempted_runs == 2
        assert validate_once.call_count == 2
        assert "boom" in result.output

    def test_large_repeat_count_compacts_success_output(self):
        config = _make_config()
        runner = ValidationRunner(config, repo_clone_url="https://github.com/owner/repo.git")
        report = _make_failure_report()

        with patch.object(
            runner,
            "_validate_once",
            side_effect=[ValidationResult(passed=True, output="ok") for _ in range(12)],
        ) as validate_once:
            result = runner.validate("diff content", report, repeat_count=12)

        assert result.passed is True
        assert result.passed_runs == 12
        assert result.attempted_runs == 12
        assert validate_once.call_count == 12
        assert "Validation passed across 12/12 consecutive runs." in result.output
        assert "[representative run 1/12]" in result.output

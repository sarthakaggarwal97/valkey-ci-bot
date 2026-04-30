"""Tests for the Analyze → Fix wiring in the pipeline orchestrator."""

from __future__ import annotations

from unittest.mock import ANY, MagicMock, patch

import pytest
from github.GithubException import GithubException

from scripts.config import BotConfig, ProjectContext
from scripts.main import _analyze_and_fix, _load_runtime_config, _process_failure, run_pipeline
from scripts.models import (
    FailedJob,
    FailureReport,
    ParsedFailure,
    RootCauseReport,
    WorkflowRun,
    failure_report_to_dict,
    root_cause_report_to_dict,
)


def _make_report(**overrides) -> FailureReport:
    """Create a minimal FailureReport for testing."""
    defaults = dict(
        workflow_name="CI",
        job_name="test-unit",
        matrix_params={},
        commit_sha="abc123",
        failure_source="trusted",
        repo_full_name="owner/repo",
        workflow_run_id=123,
        target_branch="unstable",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="TestSuite.TestCase",
                test_name="TestSuite.TestCase",
                file_path="src/foo.c",
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


def _make_root_cause(**overrides) -> RootCauseReport:
    defaults = dict(
        description="Null pointer dereference in foo()",
        files_to_change=["src/foo.c"],
        confidence="high",
        rationale="The pointer is not checked before use.",
        is_flaky=False,
        flakiness_indicators=None,
    )
    defaults.update(overrides)
    return RootCauseReport(**defaults)


class TestLoadRuntimeConfig:
    def test_loads_from_repo_when_local_file_is_missing(self, tmp_path, monkeypatch):
        gh = MagicMock()
        repo = MagicMock()
        repo.default_branch = "main"
        contents = MagicMock()
        contents.decoded_content = (
            b"bedrock:\n"
            b"  model_id: amazon.nova-pro-v1:0\n"
            b"limits:\n"
            b"  max_prs_per_day: 2\n"
        )
        repo.get_contents.return_value = contents
        gh.get_repo.return_value = repo
        monkeypatch.chdir(tmp_path)

        cfg = _load_runtime_config(
            gh, "owner/repo", ".github/ci-failure-bot.yml", ref="abc123",
        )

        assert cfg.bedrock_model_id == "amazon.nova-pro-v1:0"
        assert cfg.max_prs_per_day == 2
        repo.get_contents.assert_called_once_with(
            ".github/ci-failure-bot.yml", ref="abc123",
        )


class TestAnalyzeAndFix:
    """Tests for _analyze_and_fix helper."""

    def test_happy_path_high_confidence(self):
        """Analyze returns high confidence → fix generator is called."""
        report = _make_report()
        project = ProjectContext()
        root_cause = _make_root_cause(confidence="high")

        analyzer = MagicMock()
        analyzer.analyze.return_value = root_cause
        analyzer.identify_relevant_files.return_value = ["src/foo.c"]
        analyzer._retrieve_file_contents.return_value = {"src/foo.c": "int foo() {}"}

        fix_gen = MagicMock()
        fix_gen.generate.return_value = "--- a/src/foo.c\n+++ b/src/foo.c\n@@ -1 +1 @@\n-int foo() {}\n+int foo() { return 0; }"

        rc, diff = _analyze_and_fix(report, analyzer, fix_gen, project)

        assert rc is root_cause
        assert diff is not None
        analyzer.analyze.assert_called_once_with(report, project)
        fix_gen.generate.assert_called_once()

    def test_happy_path_medium_confidence(self):
        """Analyze returns medium confidence → fix generator is called."""
        report = _make_report()
        project = ProjectContext()
        root_cause = _make_root_cause(confidence="medium")

        analyzer = MagicMock()
        analyzer.analyze.return_value = root_cause
        analyzer.identify_relevant_files.return_value = []
        analyzer._retrieve_file_contents.return_value = {}

        fix_gen = MagicMock()
        fix_gen.generate.return_value = "some diff"

        rc, diff = _analyze_and_fix(report, analyzer, fix_gen, project)

        assert rc is root_cause
        assert diff == "some diff"
        fix_gen.generate.assert_called_once()

    def test_low_confidence_skips_fix(self):
        """Analyze returns low confidence → fix generator is NOT called."""
        report = _make_report()
        project = ProjectContext()
        root_cause = _make_root_cause(confidence="low")

        analyzer = MagicMock()
        analyzer.analyze.return_value = root_cause

        fix_gen = MagicMock()

        rc, diff = _analyze_and_fix(report, analyzer, fix_gen, project)

        assert rc is root_cause
        assert diff is None
        fix_gen.generate.assert_not_called()

    def test_analysis_failed_skips_fix(self):
        """When analysis returns 'analysis-failed', fix is skipped."""
        report = _make_report()
        project = ProjectContext()
        root_cause = _make_root_cause(
            description="analysis-failed: Bedrock error",
            confidence="low",
        )

        analyzer = MagicMock()
        analyzer.analyze.return_value = root_cause

        fix_gen = MagicMock()

        rc, diff = _analyze_and_fix(report, analyzer, fix_gen, project)

        assert rc is root_cause
        assert diff is None
        fix_gen.generate.assert_not_called()

    def test_analyze_exception_returns_none(self):
        """If analyze() raises, returns (None, None) gracefully."""
        report = _make_report()
        project = ProjectContext()

        analyzer = MagicMock()
        analyzer.analyze.side_effect = RuntimeError("boom")

        fix_gen = MagicMock()

        rc, diff = _analyze_and_fix(report, analyzer, fix_gen, project)

        assert rc is None
        assert diff is None
        fix_gen.generate.assert_not_called()

    def test_fix_generation_exception_returns_root_cause_only(self):
        """If generate() raises, returns (root_cause, None)."""
        report = _make_report()
        project = ProjectContext()
        root_cause = _make_root_cause(confidence="high")

        analyzer = MagicMock()
        analyzer.analyze.return_value = root_cause
        analyzer.identify_relevant_files.return_value = []
        analyzer._retrieve_file_contents.return_value = {}

        fix_gen = MagicMock()
        fix_gen.generate.side_effect = RuntimeError("bedrock down")

        rc, diff = _analyze_and_fix(report, analyzer, fix_gen, project)

        assert rc is root_cause
        assert diff is None

    def test_fix_generation_returns_none(self):
        """If generate() returns None (e.g., retries exhausted), diff is None."""
        report = _make_report()
        project = ProjectContext()
        root_cause = _make_root_cause(confidence="high")

        analyzer = MagicMock()
        analyzer.analyze.return_value = root_cause
        analyzer.identify_relevant_files.return_value = []
        analyzer._retrieve_file_contents.return_value = {}

        fix_gen = MagicMock()
        fix_gen.generate.return_value = None

        rc, diff = _analyze_and_fix(report, analyzer, fix_gen, project)

        assert rc is root_cause
        assert diff is None

    def test_loads_root_cause_target_files_for_fix_generation(self):
        report = _make_report()
        project = ProjectContext()
        root_cause = _make_root_cause(
            confidence="high",
            files_to_change=["src/foo.c", "include/foo.h"],
        )

        analyzer = MagicMock()
        analyzer.analyze.return_value = root_cause
        analyzer.identify_relevant_files.return_value = ["src/foo.c"]
        analyzer._retrieve_file_contents.side_effect = [
            {"src/foo.c": "int foo(void);"},
            {"include/foo.h": "#define FOO 1"},
        ]

        fix_gen = MagicMock()
        fix_gen.generate.return_value = "some diff"

        rc, diff = _analyze_and_fix(report, analyzer, fix_gen, project)

        assert rc is root_cause
        assert diff == "some diff"
        fix_gen.generate.assert_called_once_with(
            root_cause,
            {
                "src/foo.c": "int foo(void);",
                "include/foo.h": "#define FOO 1",
            },
            repo_ref=report.commit_sha,
        )

    def test_source_file_retrieval_failure_still_generates(self):
        """If source file retrieval fails, fix generation still proceeds with empty files."""
        report = _make_report()
        project = ProjectContext()
        root_cause = _make_root_cause(confidence="high")

        analyzer = MagicMock()
        analyzer.analyze.return_value = root_cause
        analyzer.identify_relevant_files.side_effect = RuntimeError("github down")

        fix_gen = MagicMock()
        fix_gen.generate.return_value = "some diff"

        rc, diff = _analyze_and_fix(report, analyzer, fix_gen, project)

        assert rc is root_cause
        # Fix generation is still called even if source retrieval fails
        assert diff == "some diff"


from scripts.failure_store import FailureStore
from scripts.main import _validate_and_create_pr, run_reconciliation
from scripts.models import ValidationResult
from scripts.rate_limiter import RateLimiter


class TestValidateAndCreatePR:
    """Tests for _validate_and_create_pr helper."""

    def _make_deps(self, validation_passed=True, validation_output="ok"):
        """Create mock dependencies for _validate_and_create_pr."""
        config = BotConfig(max_retries_validation=1)
        project = ProjectContext()

        validation_runner = MagicMock()
        validation_runner.validate.return_value = ValidationResult(
            passed=validation_passed, output=validation_output,
        )

        fix_gen = MagicMock()
        pr_mgr = MagicMock()
        pr_mgr.create_pr.return_value = "https://github.com/owner/repo/pull/1"

        rate_limiter = MagicMock()
        failure_store = MagicMock()

        root_cause_analyzer = MagicMock()
        root_cause_analyzer.identify_relevant_files.return_value = []
        root_cause_analyzer._retrieve_file_contents.return_value = {}

        return {
            "validation_runner": validation_runner,
            "fix_generator": fix_gen,
            "pr_manager": pr_mgr,
            "rate_limiter": rate_limiter,
            "failure_store": failure_store,
            "config": config,
            "root_cause_analyzer": root_cause_analyzer,
            "project": project,
        }

    def test_validation_passes_creates_pr(self):
        """When validation passes, PR is created and URL returned."""
        report = _make_report()
        root_cause = _make_root_cause()
        deps = self._make_deps(validation_passed=True)

        result = _validate_and_create_pr(
            report, root_cause, "some diff",
            deps["validation_runner"], deps["fix_generator"],
            deps["pr_manager"], deps["rate_limiter"],
            deps["failure_store"], deps["config"],
            deps["root_cause_analyzer"], deps["project"],
        )

        assert result == "https://github.com/owner/repo/pull/1"
        deps["pr_manager"].create_pr.assert_called_once()
        deps["rate_limiter"].record_pr_created.assert_called_once()

    def test_validation_fails_retries_fix_then_succeeds(self):
        """When validation fails once, fix is regenerated and retried."""
        report = _make_report()
        root_cause = _make_root_cause()
        deps = self._make_deps()

        # First validation fails, second passes
        deps["validation_runner"].validate.side_effect = [
            ValidationResult(passed=False, output="test failed"),
            ValidationResult(passed=True, output="ok"),
        ]
        deps["fix_generator"].generate.return_value = "new diff"

        result = _validate_and_create_pr(
            report, root_cause, "original diff",
            deps["validation_runner"], deps["fix_generator"],
            deps["pr_manager"], deps["rate_limiter"],
            deps["failure_store"], deps["config"],
            deps["root_cause_analyzer"], deps["project"],
        )

        assert result is not None
        # Fix generator was called with validation_error context
        deps["fix_generator"].generate.assert_called_once()
        call_kwargs = deps["fix_generator"].generate.call_args
        assert call_kwargs[1].get("validation_error") == "test failed"
        assert call_kwargs[1].get("repo_ref") == report.commit_sha
        deps["pr_manager"].create_pr.assert_called_once()

    def test_validation_fails_exhausts_retries_abandons(self):
        """When validation fails and retries are exhausted, fix is abandoned."""
        report = _make_report()
        root_cause = _make_root_cause()
        config = BotConfig(max_retries_validation=1)
        deps = self._make_deps()
        deps["config"] = config

        # Both validations fail
        deps["validation_runner"].validate.side_effect = [
            ValidationResult(passed=False, output="test failed"),
            ValidationResult(passed=False, output="still failing"),
        ]
        deps["fix_generator"].generate.return_value = "new diff"

        result = _validate_and_create_pr(
            report, root_cause, "original diff",
            deps["validation_runner"], deps["fix_generator"],
            deps["pr_manager"], deps["rate_limiter"],
            deps["failure_store"], deps["config"],
            deps["root_cause_analyzer"], deps["project"],
        )

        assert result is None
        deps["pr_manager"].create_pr.assert_not_called()

    def test_validation_fails_no_retries_configured(self):
        """With max_retries_validation=0, validation failure immediately abandons."""
        report = _make_report()
        root_cause = _make_root_cause()
        config = BotConfig(max_retries_validation=0)
        deps = self._make_deps()
        deps["config"] = config

        deps["validation_runner"].validate.return_value = ValidationResult(
            passed=False, output="test failed",
        )

        result = _validate_and_create_pr(
            report, root_cause, "some diff",
            deps["validation_runner"], deps["fix_generator"],
            deps["pr_manager"], deps["rate_limiter"],
            deps["failure_store"], deps["config"],
            deps["root_cause_analyzer"], deps["project"],
        )

        assert result is None
        deps["fix_generator"].generate.assert_not_called()
        deps["pr_manager"].create_pr.assert_not_called()

    def test_validation_exception_returns_none(self):
        """If validate() raises, returns None gracefully."""
        report = _make_report()
        root_cause = _make_root_cause()
        deps = self._make_deps()

        deps["validation_runner"].validate.side_effect = RuntimeError("boom")

        result = _validate_and_create_pr(
            report, root_cause, "some diff",
            deps["validation_runner"], deps["fix_generator"],
            deps["pr_manager"], deps["rate_limiter"],
            deps["failure_store"], deps["config"],
            deps["root_cause_analyzer"], deps["project"],
        )

        assert result is None

    def test_pr_creation_fork_failure_returns_none(self):
        """Fork PR failures return None without error."""
        report = _make_report()
        root_cause = _make_root_cause()
        deps = self._make_deps(validation_passed=True)

        deps["pr_manager"].create_pr.side_effect = ValueError("fork-pr-no-write-access")

        result = _validate_and_create_pr(
            report, root_cause, "some diff",
            deps["validation_runner"], deps["fix_generator"],
            deps["pr_manager"], deps["rate_limiter"],
            deps["failure_store"], deps["config"],
            deps["root_cause_analyzer"], deps["project"],
        )

        assert result is None

    def test_pr_creation_runtime_error_returns_none(self):
        """GitHub API errors during PR creation return None."""
        report = _make_report()
        root_cause = _make_root_cause()
        deps = self._make_deps(validation_passed=True)

        deps["pr_manager"].create_pr.side_effect = RuntimeError("pr-creation-failed")

        result = _validate_and_create_pr(
            report, root_cause, "some diff",
            deps["validation_runner"], deps["fix_generator"],
            deps["pr_manager"], deps["rate_limiter"],
            deps["failure_store"], deps["config"],
            deps["root_cause_analyzer"], deps["project"],
        )

        assert result is None

    def test_fix_regeneration_returns_none_abandons(self):
        """If fix regeneration returns None, the fix is abandoned."""
        report = _make_report()
        root_cause = _make_root_cause()
        deps = self._make_deps()

        deps["validation_runner"].validate.return_value = ValidationResult(
            passed=False, output="test failed",
        )
        deps["fix_generator"].generate.return_value = None

        result = _validate_and_create_pr(
            report, root_cause, "original diff",
            deps["validation_runner"], deps["fix_generator"],
            deps["pr_manager"], deps["rate_limiter"],
            deps["failure_store"], deps["config"],
            deps["root_cause_analyzer"], deps["project"],
        )

        assert result is None
        deps["pr_manager"].create_pr.assert_not_called()


class TestRunReconciliation:
    """Tests for run_reconciliation function."""

    @staticmethod
    def _queued_payload(
        report: FailureReport | None = None,
        root_cause: RootCauseReport | None = None,
        patch: str = "diff",
    ) -> dict:
        report = report or _make_report()
        root_cause = root_cause or _make_root_cause()
        return {
            "failure_report": failure_report_to_dict(report),
            "root_cause": root_cause_report_to_dict(root_cause),
            "patch": patch,
            "target_branch": report.target_branch,
        }

    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.PRManager")
    @patch("scripts.main.Github")
    def test_no_queued_failures(
        self, mock_gh, mock_pr_mgr, mock_store, mock_load_config,
    ):
        """When no failures are queued, returns 0."""
        mock_load_config.return_value = BotConfig()
        rate_limiter = MagicMock()
        store_instance = mock_store.return_value
        store_instance.reconcile_pr_states.return_value = []
        store_instance.list_queued_failures.return_value = []

        count = run_reconciliation(
            "owner/repo", "config.yml", "token",
            rate_limiter=rate_limiter,
        )

        assert count == 0
        store_instance.reconcile_pr_states.assert_called_once()
        store_instance.save.assert_called_once()

    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.PRManager")
    @patch("scripts.main.Github")
    def test_drains_queued_failures_up_to_pr_limit(
        self, mock_gh, mock_pr_mgr, mock_store, mock_load_config,
    ):
        """Queued failures are turned into PRs until rate limit is hit."""
        mock_load_config.return_value = BotConfig()

        rate_limiter = MagicMock()
        # Allow first 2, then block
        rate_limiter.reserve_pr_creation.side_effect = [True, True, False]

        store_instance = MagicMock()
        store_instance.list_queued_failures.return_value = ["fp1", "fp2", "fp3"]
        entry = MagicMock(failure_identifier="TestSuite.TestCase")
        entry.queued_pr_payload = self._queued_payload()
        store_instance.get_entry.return_value = entry
        store_instance.has_open_pr.return_value = False
        store_instance.record_queued_pr_failure.return_value = 1
        mock_store.return_value = store_instance

        pr_manager = MagicMock()
        pr_manager.create_pr.return_value = "https://github.com/owner/repo/pull/1"
        mock_pr_mgr.return_value = pr_manager

        count = run_reconciliation(
            "owner/repo", "config.yml", "token",
            rate_limiter=rate_limiter,
        )

        assert count == 2
        assert store_instance.clear_queued_pr.call_count == 2
        assert pr_manager.create_pr.call_count == 2
        assert rate_limiter.record_pr_created.call_count == 2

    @patch("scripts.main._dispatch_proof_campaign")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.PRManager")
    @patch("scripts.main.Github")
    def test_reconciliation_can_open_draft_prs(
        self, mock_gh, mock_pr_mgr, mock_store, mock_load_config, mock_dispatch_proof,
    ):
        """Queued failures can be reconciled into draft PRs."""
        report = _make_report(workflow_file="daily.yml")
        root_cause = _make_root_cause(is_flaky=True)
        mock_load_config.return_value = BotConfig(
            soak_validation_workflows=["daily.yml"],
            soak_validation_passes=100,
            flaky_campaign_enabled=True,
            flaky_validation_passes=3,
        )

        rate_limiter = MagicMock()
        rate_limiter.can_create_pr.return_value = True

        store_instance = MagicMock()
        store_instance.list_queued_failures.return_value = ["fp1"]
        entry = MagicMock(failure_identifier="TestSuite.TestCase")
        entry.queued_pr_payload = self._queued_payload(report=report, root_cause=root_cause)
        store_instance.get_entry.return_value = entry
        store_instance.has_open_pr.return_value = False
        store_instance.record_queued_pr_failure.return_value = 1
        mock_store.return_value = store_instance

        pr_manager = MagicMock()
        pr_manager.create_pr.return_value = "https://github.com/owner/repo/pull/1"
        mock_pr_mgr.return_value = pr_manager

        count = run_reconciliation(
            "owner/repo",
            "config.yml",
            "token",
            state_github_token="state-token",
            state_repo_name="owner/state-repo",
            rate_limiter=rate_limiter,
            draft_prs=True,
        )

        assert count == 1
        pr_manager.create_pr.assert_called_once()
        assert pr_manager.create_pr.call_args.kwargs["draft"] is True
        mock_dispatch_proof.assert_called_once()
        assert mock_dispatch_proof.call_args.kwargs["repeat_count"] == 100
        store_instance.update_proof_campaign.assert_called_once()

    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.PRManager")
    @patch("scripts.main.Github")
    def test_skips_already_open_pr(
        self, mock_gh, mock_pr_mgr, mock_store, mock_load_config,
    ):
        """Queued failures with open PRs are dequeued without reprocessing."""
        mock_load_config.return_value = BotConfig()

        rate_limiter = MagicMock()
        rate_limiter.can_create_pr.return_value = True

        store_instance = MagicMock()
        store_instance.list_queued_failures.return_value = ["fp1"]
        store_instance.get_entry.return_value = MagicMock(
            failure_identifier="test",
            queued_pr_payload=self._queued_payload(),
        )
        store_instance.has_open_pr.return_value = True
        mock_store.return_value = store_instance

        count = run_reconciliation(
            "owner/repo", "config.yml", "token",
            rate_limiter=rate_limiter,
        )

        assert count == 1
        store_instance.clear_queued_pr.assert_called_once_with("fp1")

    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.PRManager")
    @patch("scripts.main.Github")
    def test_dequeues_missing_payload(
        self, mock_gh, mock_pr_mgr, mock_store, mock_load_config,
    ):
        """Queued fingerprints without persisted PR context are dropped."""
        mock_load_config.return_value = BotConfig()

        rate_limiter = MagicMock()
        rate_limiter.can_create_pr.return_value = True

        store_instance = MagicMock()
        store_instance.list_queued_failures.return_value = ["fp1"]
        store_instance.get_entry.return_value = MagicMock(
            failure_identifier="test",
            queued_pr_payload=None,
        )
        store_instance.has_open_pr.return_value = False
        mock_store.return_value = store_instance

        count = run_reconciliation(
            "owner/repo", "config.yml", "token",
            rate_limiter=rate_limiter,
        )

        assert count == 1
        store_instance.clear_queued_pr.assert_called_once_with("fp1")
        mock_pr_mgr.return_value.create_pr.assert_not_called()

    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.PRManager")
    @patch("scripts.main.Github")
    def test_pr_creation_failure_keeps_queued_payload_for_retry(
        self, mock_gh, mock_pr_mgr, mock_store, mock_load_config,
    ):
        """Transient queued PR failures stay queued for a future attempt."""
        mock_load_config.return_value = BotConfig()

        rate_limiter = MagicMock()
        rate_limiter.can_create_pr.return_value = True

        store_instance = MagicMock()
        store_instance.list_queued_failures.return_value = ["fp1"]
        entry = MagicMock(failure_identifier="TestSuite.TestCase")
        entry.queued_pr_payload = self._queued_payload()
        store_instance.get_entry.return_value = entry
        store_instance.has_open_pr.return_value = False
        store_instance.record_queued_pr_failure.return_value = 1
        mock_store.return_value = store_instance

        pr_manager = MagicMock()
        pr_manager.create_pr.side_effect = RuntimeError("pr-creation-failed: 500")
        mock_pr_mgr.return_value = pr_manager

        count = run_reconciliation(
            "owner/repo", "config.yml", "token",
            rate_limiter=rate_limiter,
        )

        assert count == 0
        store_instance.clear_queued_pr.assert_not_called()
        store_instance.clear_queued_pr.assert_not_called()
        pr_manager.create_pr.assert_called_once()


class TestRunPipeline:
    @patch("scripts.main._build_workflow_run")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.Github")
    @patch("scripts.main.ApprovalSummary")
    @patch("scripts.main.FailureDetector")
    @patch("scripts.main.LogRetriever")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.BedrockClient")
    @patch("scripts.main.RootCauseAnalyzer")
    @patch("scripts.main.FixGenerator")
    @patch("scripts.main.ValidationRunner")
    @patch("scripts.main.PRManager")
    def test_skips_unparseable_jobs_without_consuming_structured_failure_cap(
        self,
        mock_pr_manager,
        mock_validation_runner,
        mock_fix_generator,
        mock_root_cause_analyzer,
        mock_bedrock_client,
        mock_failure_store,
        mock_log_retriever,
        mock_detector,
        mock_approval_summary,
        mock_gh,
        mock_load_config,
        mock_build_workflow_run,
    ):
        mock_build_workflow_run.return_value = WorkflowRun(
            id=1,
            name="CI",
            event="push",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="ci.yml",
        )
        mock_load_config.return_value = BotConfig(
            monitored_workflows=["ci.yml"],
            max_failures_per_run=2,
        )
        mock_detector.return_value.detect.return_value = [
            FailedJob(id=1, name="job-a-unparseable", conclusion="failure", step_name=None, matrix_params={}),
            FailedJob(id=2, name="job-b-parseable", conclusion="failure", step_name=None, matrix_params={}),
            FailedJob(id=3, name="job-c-unparseable", conclusion="failure", step_name=None, matrix_params={}),
            FailedJob(id=4, name="job-d-parseable", conclusion="failure", step_name=None, matrix_params={}),
            FailedJob(id=5, name="job-e-parseable-over-limit", conclusion="failure", step_name=None, matrix_params={}),
        ]

        log_retriever = mock_log_retriever.return_value
        logs = {
            1: "plain text with no structured parser match",
            2: "src/foo.c:42: Failure\nExpected: 1\n  Actual: 0\n[  FAILED  ] TestSuite.ParseableOne\n",
            3: "still plain text without parser markers",
            4: "src/bar.c:43: Failure\nExpected: 1\n  Actual: 0\n[  FAILED  ] TestSuite.ParseableTwo\n",
            5: "src/baz.c:44: Failure\nExpected: 1\n  Actual: 0\n[  FAILED  ] TestSuite.ParseableThree\n",
        }
        log_retriever.get_job_log.side_effect = lambda _repo, job_id: logs[job_id]

        failure_store = mock_failure_store.return_value
        failure_store.compute_incident_key.side_effect = (
            lambda failure_identifier, file_path, *, test_name=None: f"{test_name or failure_identifier}|{file_path}"
        )
        failure_store.has_open_pr.return_value = False
        failure_store.get_entry.return_value = None
        failure_store.get_flaky_campaign.return_value = None
        failure_store.summarize_history.return_value = MagicMock(
            consecutive_failures=2,
            failure_count=2,
            last_known_good_sha="goodsha",
            first_bad_sha="badsha",
        )

        mock_root_cause_analyzer.return_value.analyze.return_value = _make_root_cause()
        mock_root_cause_analyzer.return_value.identify_relevant_files.return_value = []
        mock_root_cause_analyzer.return_value._retrieve_file_contents.return_value = {}
        mock_fix_generator.return_value.generate.return_value = "diff"
        mock_validation_runner.return_value.validate.return_value = ValidationResult(
            passed=True, output="ok",
        )

        rate_limiter = MagicMock()
        rate_limiter.can_use_tokens.return_value = True
        rate_limiter.can_create_pr.return_value = True

        result = run_pipeline(
            "owner/repo",
            1,
            ".github/ci-failure-bot.yml",
            "token",
            allow_pr_creation=False,
            rate_limiter=rate_limiter,
        )

        outcomes = {
            (item["job_name"], item["outcome"])
            for item in result.job_outcomes
        }
        assert ("job-a-unparseable", "unparseable") in outcomes
        assert ("job-c-unparseable", "unparseable") in outcomes
        assert ("job-e-parseable-over-limit", "skipped-rate-limit") in outcomes
        assert ("job-b-parseable", "queued-manual-approval") in outcomes
        assert ("job-d-parseable", "queued-manual-approval") in outcomes
        assert len(result.reports) == 2
        assert mock_fix_generator.return_value.generate.call_count == 2
        assert failure_store.record_queued_pr.call_count == 2

    @patch("scripts.main._build_workflow_run")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.Github")
    @patch("scripts.main.ApprovalSummary")
    @patch("scripts.main.FailureDetector")
    @patch("scripts.main.LogRetriever")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.BedrockClient")
    @patch("scripts.main.RootCauseAnalyzer")
    @patch("scripts.main.FixGenerator")
    @patch("scripts.main.ValidationRunner")
    @patch("scripts.main.PRManager")
    def test_groups_duplicate_incidents_before_applying_structured_failure_cap(
        self,
        mock_pr_manager,
        mock_validation_runner,
        mock_fix_generator,
        mock_root_cause_analyzer,
        mock_bedrock_client,
        mock_failure_store,
        mock_log_retriever,
        mock_detector,
        mock_approval_summary,
        mock_gh,
        mock_load_config,
        mock_build_workflow_run,
    ):
        mock_build_workflow_run.return_value = WorkflowRun(
            id=1,
            name="CI",
            event="push",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="ci.yml",
        )
        mock_load_config.return_value = BotConfig(
            monitored_workflows=["ci.yml"],
            max_failures_per_run=1,
        )
        mock_detector.return_value.detect.return_value = [
            FailedJob(id=1, name="job-a-representative", conclusion="failure", step_name=None, matrix_params={}),
            FailedJob(id=2, name="job-b-duplicate-incident", conclusion="failure", step_name=None, matrix_params={}),
            FailedJob(id=3, name="job-c-distinct-incident", conclusion="failure", step_name=None, matrix_params={}),
        ]

        log_retriever = mock_log_retriever.return_value
        logs = {
            1: "src/foo.c:42: Failure\nExpected: 1\n  Actual: 0\n[  FAILED  ] TestSuite.SharedIncident\n",
            2: "src/foo.c:42: Failure\nExpected: 1\n  Actual: 0\n[  FAILED  ] TestSuite.SharedIncident\n",
            3: "src/bar.c:43: Failure\nExpected: 1\n  Actual: 0\n[  FAILED  ] TestSuite.UniqueIncident\n",
        }
        log_retriever.get_job_log.side_effect = lambda _repo, job_id: logs[job_id]

        failure_store = mock_failure_store.return_value
        failure_store.compute_incident_key.side_effect = (
            lambda failure_identifier, file_path, *, test_name=None: f"{test_name or failure_identifier}|{file_path}"
        )
        failure_store.has_open_pr.return_value = False
        failure_store.get_entry.return_value = None
        failure_store.get_flaky_campaign.return_value = None
        failure_store.summarize_history.return_value = MagicMock(
            consecutive_failures=2,
            failure_count=2,
            last_known_good_sha="goodsha",
            first_bad_sha="badsha",
        )

        mock_root_cause_analyzer.return_value.analyze.return_value = _make_root_cause()
        mock_root_cause_analyzer.return_value.identify_relevant_files.return_value = []
        mock_root_cause_analyzer.return_value._retrieve_file_contents.return_value = {}
        mock_fix_generator.return_value.generate.return_value = "diff"
        mock_validation_runner.return_value.validate.return_value = ValidationResult(
            passed=True, output="ok",
        )

        rate_limiter = MagicMock()
        rate_limiter.can_use_tokens.return_value = True
        rate_limiter.can_create_pr.return_value = True

        result = run_pipeline(
            "owner/repo",
            1,
            ".github/ci-failure-bot.yml",
            "token",
            allow_pr_creation=False,
            rate_limiter=rate_limiter,
        )

        outcomes = {
            (item["job_name"], item["outcome"])
            for item in result.job_outcomes
        }
        assert ("job-a-representative", "queued-manual-approval") in outcomes
        assert ("job-b-duplicate-incident", "skipped") in outcomes
        assert ("job-c-distinct-incident", "skipped-rate-limit") in outcomes
        assert len(result.reports) == 1
        assert mock_fix_generator.return_value.generate.call_count == 1
        assert failure_store.record_queued_pr.call_count == 1

    @patch("scripts.main._build_workflow_run")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.Github")
    @patch("scripts.main.FailureDetector")
    def test_skips_unmonitored_workflow(
        self, mock_detector, mock_gh, mock_load_config, mock_build_workflow_run,
    ):
        mock_build_workflow_run.return_value = WorkflowRun(
            id=1,
            name="CI",
            event="push",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="nightly.yml",
        )
        mock_load_config.return_value = BotConfig(monitored_workflows=["ci.yml"])

        result = run_pipeline(
            "owner/repo", 1, ".github/ci-failure-bot.yml", "token",
        )

        assert result.reports == []
        assert result.job_outcomes == []

    @patch("scripts.main._build_workflow_run")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.Github")
    @patch("scripts.main.ApprovalSummary")
    @patch("scripts.main.FailureDetector")
    @patch("scripts.main.LogRetriever")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.BedrockClient")
    @patch("scripts.main.RootCauseAnalyzer")
    @patch("scripts.main.FixGenerator")
    @patch("scripts.main.ValidationRunner")
    @patch("scripts.main.PRManager")
    def test_queues_validated_fix_when_pr_creation_is_rate_limited(
        self,
        mock_pr_manager,
        mock_validation_runner,
        mock_fix_generator,
        mock_root_cause_analyzer,
        mock_bedrock_client,
        mock_failure_store,
        mock_log_retriever,
        mock_detector,
        mock_approval_summary,
        mock_gh,
        mock_load_config,
        mock_build_workflow_run,
    ):
        workflow_run = WorkflowRun(
            id=1,
            name="CI",
            event="push",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="ci.yml",
        )
        mock_build_workflow_run.return_value = workflow_run
        mock_load_config.return_value = BotConfig(monitored_workflows=["ci.yml"])

        detector_instance = mock_detector.return_value
        detector_instance.detect.return_value = [
            FailedJob(
                id=10,
                name="test-unit",
                conclusion="failure",
                step_name="Run tests",
                matrix_params={},
            )
        ]

        log_retriever_instance = mock_log_retriever.return_value
        log_retriever_instance.get_job_log.return_value = (
            "src/foo.c:42: Failure\n"
            "Expected: 1\n"
            "  Actual: 0\n"
            "[  FAILED  ] TestSuite.TestCase\n"
        )

        failure_store = mock_failure_store.return_value
        failure_store.compute_incident_key.return_value = "fp1"
        failure_store.has_open_pr.return_value = False
        failure_store.get_entry.return_value = None
        failure_store.summarize_history.return_value = MagicMock(
            consecutive_failures=2,
            failure_count=2,
            last_known_good_sha="goodsha",
            first_bad_sha="badsha",
        )
        failure_store.summarize_history.return_value = MagicMock(
            consecutive_failures=2,
            failure_count=2,
            last_known_good_sha="goodsha",
            first_bad_sha="badsha",
        )

        root_cause = _make_root_cause()
        analyzer_instance = mock_root_cause_analyzer.return_value
        analyzer_instance.analyze.return_value = root_cause
        analyzer_instance.identify_relevant_files.return_value = []
        analyzer_instance._retrieve_file_contents.return_value = {}

        fix_generator_instance = mock_fix_generator.return_value
        fix_generator_instance.generate.return_value = "diff"

        validation_runner = mock_validation_runner.return_value
        validation_runner.validate.return_value = ValidationResult(
            passed=True, output="ok",
        )

        rate_limiter = MagicMock()
        rate_limiter.can_use_tokens.return_value = True
        rate_limiter.reserve_pr_creation.return_value = False

        reports = run_pipeline(
            "owner/repo",
            1,
            ".github/ci-failure-bot.yml",
            "token",
            rate_limiter=rate_limiter,
        )

        assert len(reports.reports) == 1
        failure_store.record_queued_pr.assert_called_once()
        mock_pr_manager.return_value.create_pr.assert_not_called()

    @patch("scripts.main._build_workflow_run")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.Github")
    @patch("scripts.main.ApprovalSummary")
    @patch("scripts.main.FailureDetector")
    @patch("scripts.main.LogRetriever")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.BedrockClient")
    @patch("scripts.main.RootCauseAnalyzer")
    @patch("scripts.main.FixGenerator")
    @patch("scripts.main.ValidationRunner")
    @patch("scripts.main.PRManager")
    def test_queues_validated_fix_for_manual_approval_when_pr_creation_is_disabled(
        self,
        mock_pr_manager,
        mock_validation_runner,
        mock_fix_generator,
        mock_root_cause_analyzer,
        mock_bedrock_client,
        mock_failure_store,
        mock_log_retriever,
        mock_detector,
        mock_approval_summary,
        mock_gh,
        mock_load_config,
        mock_build_workflow_run,
    ):
        workflow_run = WorkflowRun(
            id=1,
            name="CI",
            event="push",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="ci.yml",
        )
        mock_build_workflow_run.return_value = workflow_run
        mock_load_config.return_value = BotConfig(monitored_workflows=["ci.yml"])
        mock_detector.return_value.detect.return_value = [
            FailedJob(
                id=10,
                name="test-unit",
                conclusion="failure",
                step_name="Run tests",
                matrix_params={},
            )
        ]
        mock_log_retriever.return_value.get_job_log.return_value = (
            "src/foo.c:42: Failure\n"
            "Expected: 1\n"
            "  Actual: 0\n"
            "[  FAILED  ] TestSuite.TestCase\n"
        )

        failure_store = mock_failure_store.return_value
        failure_store.compute_incident_key.return_value = "fp1"
        failure_store.has_open_pr.return_value = False
        failure_store.get_entry.return_value = None
        failure_store.summarize_history.return_value = MagicMock(
            consecutive_failures=2,
            failure_count=2,
            last_known_good_sha="goodsha",
            first_bad_sha="badsha",
        )
        failure_store.summarize_history.return_value = MagicMock(
            consecutive_failures=2,
            failure_count=2,
            last_known_good_sha="goodsha",
            first_bad_sha="badsha",
        )

        mock_root_cause_analyzer.return_value.analyze.return_value = _make_root_cause()
        mock_root_cause_analyzer.return_value.identify_relevant_files.return_value = []
        mock_root_cause_analyzer.return_value._retrieve_file_contents.return_value = {}
        mock_fix_generator.return_value.generate.return_value = "diff"
        mock_validation_runner.return_value.validate.return_value = ValidationResult(
            passed=True, output="ok",
        )

        rate_limiter = MagicMock()
        rate_limiter.can_use_tokens.return_value = True
        rate_limiter.can_create_pr.return_value = True

        run_pipeline(
            "owner/repo",
            1,
            ".github/ci-failure-bot.yml",
            "token",
            state_github_token="state-token",
            state_repo_name="owner/bot-repo",
            allow_pr_creation=False,
            rate_limiter=rate_limiter,
        )

        failure_store.record_queued_pr.assert_called_once()
        mock_pr_manager.return_value.create_pr.assert_not_called()
        mock_approval_summary.return_value.add_candidate.assert_called_once()

    @patch("scripts.main._build_workflow_run")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.Github")
    @patch("scripts.main.FailureDetector")
    @patch("scripts.main.LogRetriever")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.BedrockClient")
    @patch("scripts.main.RootCauseAnalyzer")
    @patch("scripts.main.FixGenerator")
    @patch("scripts.main.ValidationRunner")
    @patch("scripts.main.PRManager")
    def test_uses_separate_state_repo_for_pipeline_persistence(
        self,
        mock_pr_manager,
        mock_validation_runner,
        mock_fix_generator,
        mock_root_cause_analyzer,
        mock_bedrock_client,
        mock_failure_store,
        mock_log_retriever,
        mock_detector,
        mock_gh,
        mock_load_config,
        mock_build_workflow_run,
    ):
        mock_build_workflow_run.return_value = WorkflowRun(
            id=1,
            name="CI",
            event="push",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="ci.yml",
        )
        mock_load_config.return_value = BotConfig(monitored_workflows=["ci.yml"])
        mock_detector.return_value.detect.return_value = []
        mock_gh.return_value.get_repo.return_value.get_contents.side_effect = (
            GithubException(404, {"message": "missing state"})
        )

        run_pipeline(
            "owner/repo",
            1,
            ".github/ci-failure-bot.yml",
            "target-token",
            state_github_token="state-token",
            state_repo_name="owner/bot-repo",
        )

        assert mock_failure_store.call_args.kwargs["state_repo_full_name"] == "owner/bot-repo"
        assert mock_failure_store.call_args.kwargs["state_github_client"] is mock_gh.return_value

    @patch("scripts.main._build_workflow_run")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.Github")
    @patch("scripts.main.FailureDetector")
    @patch("scripts.main.LogRetriever")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.BedrockClient")
    @patch("scripts.main.RootCauseAnalyzer")
    @patch("scripts.main.FixGenerator")
    @patch("scripts.main.ValidationRunner")
    @patch("scripts.main.PRManager")
    def test_queues_validated_fix_even_when_history_threshold_is_not_met(
        self,
        mock_pr_manager,
        mock_validation_runner,
        mock_fix_generator,
        mock_root_cause_analyzer,
        mock_bedrock_client,
        mock_failure_store,
        mock_log_retriever,
        mock_detector,
        mock_gh,
        mock_load_config,
        mock_build_workflow_run,
    ):
        mock_build_workflow_run.return_value = WorkflowRun(
            id=1,
            name="CI",
            event="push",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="ci.yml",
        )
        mock_load_config.return_value = BotConfig(monitored_workflows=["ci.yml"])
        mock_detector.return_value.detect.return_value = [
            FailedJob(
                id=10,
                name="test-unit",
                conclusion="failure",
                step_name="Run tests",
                matrix_params={},
            )
        ]
        mock_log_retriever.return_value.get_job_log.return_value = (
            "src/foo.c:42: Failure\n"
            "Expected: 1\n"
            "  Actual: 0\n"
            "[  FAILED  ] TestSuite.TestCase\n"
        )

        failure_store = mock_failure_store.return_value
        failure_store.compute_incident_key.return_value = "fp1"
        failure_store.has_open_pr.return_value = False
        failure_store.get_entry.return_value = None
        failure_store.summarize_history.return_value = MagicMock(
            consecutive_failures=1,
            failure_count=1,
            last_known_good_sha=None,
            first_bad_sha="abc123",
        )

        mock_root_cause_analyzer.return_value.analyze.return_value = _make_root_cause(
            confidence="medium",
        )
        mock_root_cause_analyzer.return_value.identify_relevant_files.return_value = []
        mock_root_cause_analyzer.return_value._retrieve_file_contents.return_value = {}
        mock_fix_generator.return_value.generate.return_value = "diff"
        mock_validation_runner.return_value.validate.return_value = ValidationResult(
            passed=True, output="ok",
        )

        rate_limiter = MagicMock()
        rate_limiter.can_use_tokens.return_value = True
        rate_limiter.can_create_pr.return_value = True

        run_pipeline(
            "owner/repo",
            1,
            ".github/ci-failure-bot.yml",
            "token",
            allow_pr_creation=False,
            rate_limiter=rate_limiter,
        )

        failure_store.record_queued_pr.assert_called_once()
        mock_pr_manager.return_value.create_pr.assert_not_called()

    @patch("scripts.main._build_workflow_run")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.Github")
    @patch("scripts.main.FailureDetector")
    @patch("scripts.main.LogRetriever")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.BedrockClient")
    @patch("scripts.main.RootCauseAnalyzer")
    @patch("scripts.main.FixGenerator")
    @patch("scripts.main.ValidationRunner")
    @patch("scripts.main.PRManager")
    def test_posts_processing_summary_on_created_pr(
        self,
        mock_pr_manager,
        mock_validation_runner,
        mock_fix_generator,
        mock_root_cause_analyzer,
        mock_bedrock_client,
        mock_failure_store,
        mock_log_retriever,
        mock_detector,
        mock_gh,
        mock_load_config,
        mock_build_workflow_run,
    ):
        workflow_run = WorkflowRun(
            id=1,
            name="CI",
            event="push",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="ci.yml",
        )
        mock_build_workflow_run.return_value = workflow_run
        mock_load_config.return_value = BotConfig(monitored_workflows=["ci.yml"])

        mock_detector.return_value.detect.return_value = [
            FailedJob(
                id=10,
                name="test-unit",
                conclusion="failure",
                step_name="Run tests",
                matrix_params={},
            )
        ]
        mock_log_retriever.return_value.get_job_log.return_value = (
            "src/foo.c:42: Failure\n"
            "Expected: 1\n"
            "  Actual: 0\n"
            "[  FAILED  ] TestSuite.TestCase\n"
        )

        failure_store = mock_failure_store.return_value
        failure_store.compute_incident_key.return_value = "fp1"
        failure_store.has_open_pr.return_value = False
        failure_store.get_entry.return_value = None
        failure_store.summarize_history.return_value = MagicMock(
            consecutive_failures=2,
            failure_count=2,
            last_known_good_sha="goodsha",
            first_bad_sha="badsha",
        )

        mock_root_cause_analyzer.return_value.analyze.return_value = _make_root_cause()
        mock_root_cause_analyzer.return_value.identify_relevant_files.return_value = []
        mock_root_cause_analyzer.return_value._retrieve_file_contents.return_value = {}
        mock_fix_generator.return_value.generate.return_value = "diff"
        mock_fix_generator.return_value.last_attempt_count = 1
        mock_validation_runner.return_value.validate.return_value = ValidationResult(
            passed=True, output="ok",
        )

        rate_limiter = MagicMock()
        rate_limiter.can_use_tokens.return_value = True
        rate_limiter.can_create_pr.return_value = True
        mock_pr_manager.return_value.create_pr.return_value = "https://github.com/owner/repo/pull/1"

        run_pipeline(
            "owner/repo",
            1,
            ".github/ci-failure-bot.yml",
            "token",
            rate_limiter=rate_limiter,
        )

        mock_pr_manager.return_value.post_summary_comment.assert_called_once()
        pr_url, comment = mock_pr_manager.return_value.post_summary_comment.call_args[0]
        assert pr_url == "https://github.com/owner/repo/pull/1"
        assert comment.fix_retries == 0
        assert comment.validation_retries == 0
        assert [step.name for step in comment.steps] == [
            "detection", "parsing", "analysis", "generation", "validation", "pr_creation",
        ]

    @patch("scripts.main._build_workflow_run")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.Github")
    @patch("scripts.main.FailureDetector")
    @patch("scripts.main.LogRetriever")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.BedrockClient")
    @patch("scripts.main.RootCauseAnalyzer")
    @patch("scripts.main.FixGenerator")
    @patch("scripts.main.ValidationRunner")
    @patch("scripts.main.PRManager")
    def test_flaky_failures_use_campaign_backlog_and_repeated_validation(
        self,
        mock_pr_manager,
        mock_validation_runner,
        mock_fix_generator,
        mock_root_cause_analyzer,
        mock_bedrock_client,
        mock_failure_store,
        mock_log_retriever,
        mock_detector,
        mock_gh,
        mock_load_config,
        mock_build_workflow_run,
    ):
        workflow_run = WorkflowRun(
            id=1,
            name="CI",
            event="push",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="ci.yml",
        )
        mock_build_workflow_run.return_value = workflow_run
        config = BotConfig(monitored_workflows=["ci.yml"])
        config.flaky_campaign_enabled = True
        config.flaky_validation_passes = 3
        mock_load_config.return_value = config

        mock_detector.return_value.detect.return_value = [
            FailedJob(
                id=10,
                name="test-unit",
                conclusion="failure",
                step_name="Run tests",
                matrix_params={},
            )
        ]
        mock_log_retriever.return_value.get_job_log.return_value = (
            "src/foo.c:42: Failure\n"
            "Expected: 1\n"
            "  Actual: 0\n"
            "[  FAILED  ] TestSuite.TestCase\n"
        )

        failure_store = mock_failure_store.return_value
        failure_store.compute_incident_key.return_value = "fp1"
        failure_store.has_open_pr.return_value = False
        failure_store.get_entry.return_value = None
        failure_store.get_flaky_campaign.return_value = MagicMock(
            failed_hypotheses=["null-guard-only fix failed after 1/3 runs"],
        )
        failure_store.summarize_history.return_value = MagicMock(
            consecutive_failures=2,
            failure_count=2,
            last_known_good_sha="goodsha",
            first_bad_sha="badsha",
        )

        mock_root_cause_analyzer.return_value.analyze.return_value = _make_root_cause(
            is_flaky=True,
            flakiness_indicators=["timing"],
            confidence="medium",
        )
        mock_root_cause_analyzer.return_value.identify_relevant_files.return_value = []
        mock_root_cause_analyzer.return_value._retrieve_file_contents.return_value = {}
        mock_fix_generator.return_value.generate.return_value = "diff"
        mock_validation_runner.return_value.validate.return_value = ValidationResult(
            passed=True,
            output="ok",
            passed_runs=3,
            attempted_runs=3,
        )

        rate_limiter = MagicMock()
        rate_limiter.can_use_tokens.return_value = True
        rate_limiter.can_create_pr.return_value = True

        run_pipeline(
            "owner/repo",
            1,
            ".github/ci-failure-bot.yml",
            "token",
            allow_pr_creation=False,
            rate_limiter=rate_limiter,
        )

        mock_fix_generator.return_value.generate.assert_called_once()
        generate_kwargs = mock_fix_generator.return_value.generate.call_args.kwargs
        assert generate_kwargs["failed_hypotheses"] == [
            "null-guard-only fix failed after 1/3 runs"
        ]
        mock_validation_runner.return_value.validate.assert_called_once_with(
            "diff",
            ANY,
            repeat_count=3,
        )
        failure_store.record_flaky_campaign_attempt.assert_called_once()
        failure_store.record_queued_pr.assert_called_once()

    @patch("scripts.main.boto3.client")
    @patch("scripts.main._build_workflow_run")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.Github")
    @patch("scripts.main.FailureDetector")
    @patch("scripts.main.LogRetriever")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.BedrockClient")
    @patch("scripts.main.RootCauseAnalyzer")
    @patch("scripts.main.FixGenerator")
    @patch("scripts.main.ValidationRunner")
    @patch("scripts.main.PRManager")
    def test_daily_failures_require_soak_validation_before_queue(
        self,
        mock_pr_manager,
        mock_validation_runner,
        mock_fix_generator,
        mock_root_cause_analyzer,
        mock_bedrock_client,
        mock_failure_store,
        mock_log_retriever,
        mock_detector,
        mock_gh,
        mock_load_config,
        mock_build_workflow_run,
        mock_boto_client,
    ):
        workflow_run = WorkflowRun(
            id=1,
            name="Daily",
            event="schedule",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="daily.yml",
        )
        mock_build_workflow_run.return_value = workflow_run
        config = BotConfig(monitored_workflows=["daily.yml"])
        config.soak_validation_workflows = ["daily.yml"]
        config.soak_validation_passes = 100
        mock_load_config.return_value = config

        mock_detector.return_value.detect.return_value = [
            FailedJob(
                id=10,
                name="test-unit",
                conclusion="failure",
                step_name="Run tests",
                matrix_params={},
            )
        ]
        mock_log_retriever.return_value.get_job_log.return_value = (
            "src/foo.c:42: Failure\n"
            "Expected: 1\n"
            "  Actual: 0\n"
            "[  FAILED  ] TestSuite.TestCase\n"
        )

        failure_store = mock_failure_store.return_value
        failure_store.compute_incident_key.return_value = "fp-daily"
        failure_store.has_open_pr.return_value = False
        failure_store.get_entry.return_value = None
        failure_store.summarize_history.return_value = MagicMock(
            consecutive_failures=2,
            failure_count=2,
            last_known_good_sha="goodsha",
            first_bad_sha="badsha",
        )

        mock_root_cause_analyzer.return_value.analyze.return_value = _make_root_cause(
            confidence="high",
            is_flaky=False,
        )
        mock_root_cause_analyzer.return_value.identify_relevant_files.return_value = []
        mock_root_cause_analyzer.return_value._retrieve_file_contents.return_value = {}
        mock_fix_generator.return_value.generate.return_value = "diff"
        mock_validation_runner.return_value.validate.return_value = ValidationResult(
            passed=True,
            output="stable",
            passed_runs=100,
            attempted_runs=100,
        )

        rate_limiter = MagicMock()
        rate_limiter.can_use_tokens.return_value = True
        rate_limiter.can_create_pr.return_value = True

        run_pipeline(
            "owner/repo",
            1,
            ".github/ci-failure-bot.yml",
            "token",
            allow_pr_creation=False,
            rate_limiter=rate_limiter,
        )

        mock_validation_runner.return_value.validate.assert_called_once_with(
            "diff",
            ANY,
            repeat_count=100,
        )
        failure_store.record_queued_pr.assert_called_once()

    @patch("scripts.main.boto3.client")
    @patch("scripts.main._build_workflow_run")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.Github")
    @patch("scripts.main.FailureDetector")
    @patch("scripts.main.LogRetriever")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.BedrockClient")
    @patch("scripts.main.RootCauseAnalyzer")
    @patch("scripts.main.FixGenerator")
    @patch("scripts.main.ValidationRunner")
    @patch("scripts.main.PRManager")
    def test_wires_retriever_when_retrieval_is_enabled(
        self,
        mock_pr_manager,
        mock_validation_runner,
        mock_fix_generator,
        mock_root_cause_analyzer,
        mock_bedrock_client,
        mock_failure_store,
        mock_log_retriever,
        mock_detector,
        mock_gh,
        mock_load_config,
        mock_build_workflow_run,
        mock_boto_client,
    ):
        workflow_run = WorkflowRun(
            id=1,
            name="CI",
            event="push",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="ci.yml",
        )
        mock_build_workflow_run.return_value = workflow_run
        config = BotConfig(monitored_workflows=["ci.yml"])
        config.retrieval.enabled = True
        config.retrieval.code_knowledge_base_id = "CODEKB"
        mock_load_config.return_value = config

        mock_detector.return_value.detect.return_value = []
        mock_boto_client.side_effect = [MagicMock(), MagicMock()]
        mock_gh.return_value.get_repo.return_value.get_contents.side_effect = (
            GithubException(404, {"message": "missing state"})
        )

        run_pipeline(
            "owner/repo",
            1,
            ".github/ci-failure-bot.yml",
            "token",
            aws_region="us-east-1",
        )

        mock_boto_client.assert_any_call("bedrock-runtime", region_name="us-east-1")
        mock_boto_client.assert_any_call("bedrock-agent-runtime", region_name="us-east-1")
        assert mock_root_cause_analyzer.return_value.with_retriever.call_count == 1
        assert mock_fix_generator.return_value.with_retriever.call_count == 1

    @patch("scripts.main.boto3.client")
    @patch("scripts.main._build_workflow_run")
    @patch("scripts.main._load_runtime_config")
    @patch("scripts.main.Github")
    @patch("scripts.main.FailureDetector")
    @patch("scripts.main.LogRetriever")
    @patch("scripts.main.FailureStore")
    @patch("scripts.main.BedrockClient")
    @patch("scripts.main.RootCauseAnalyzer")
    @patch("scripts.main.FixGenerator")
    @patch("scripts.main.ValidationRunner")
    @patch("scripts.main.PRManager")
    def test_skips_retriever_client_when_no_kb_ids_are_configured(
        self,
        mock_pr_manager,
        mock_validation_runner,
        mock_fix_generator,
        mock_root_cause_analyzer,
        mock_bedrock_client,
        mock_failure_store,
        mock_log_retriever,
        mock_detector,
        mock_gh,
        mock_load_config,
        mock_build_workflow_run,
        mock_boto_client,
    ):
        workflow_run = WorkflowRun(
            id=1,
            name="CI",
            event="push",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="ci.yml",
        )
        mock_build_workflow_run.return_value = workflow_run
        config = BotConfig(monitored_workflows=["ci.yml"])
        config.retrieval.enabled = True
        mock_load_config.return_value = config

        mock_detector.return_value.detect.return_value = []
        mock_boto_client.return_value = MagicMock()
        mock_gh.return_value.get_repo.return_value.get_contents.side_effect = (
            GithubException(404, {"message": "missing state"})
        )

        run_pipeline(
            "owner/repo",
            1,
            ".github/ci-failure-bot.yml",
            "token",
            aws_region="us-east-1",
        )

        mock_boto_client.assert_called_once_with("bedrock-runtime", region_name="us-east-1")


class TestProcessFailure:
    def test_unparseable_failures_are_recorded_for_deduplication(self):
        workflow_run = WorkflowRun(
            id=1,
            name="CI",
            event="push",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="owner/repo",
            is_fork=False,
            conclusion="failure",
            workflow_file="ci.yml",
        )
        job = FailedJob(
            id=10,
            name="test-unit",
            conclusion="failure",
            step_name="Run tests",
            matrix_params={},
        )
        log_retriever = MagicMock()
        log_retriever.get_job_log.return_value = "plain output\nwith no parser markers"
        parser_router = MagicMock()
        parser_router.parse.return_value = ([], "plain output\nwith no parser markers", True)
        failure_store = MagicMock()
        failure_store.compute_incident_key.return_value = "incident-unparseable"
        failure_store.get_entry.return_value = None
        failure_store.has_open_pr.return_value = False

        report = _process_failure(
            job,
            workflow_run,
            "trusted",
            log_retriever,
            parser_router,
            failure_store,
            max_history_entries=5,
        )

        assert report is not None
        failure_store.compute_incident_key.assert_called_once_with(
            "test-unit", "",
        )
        failure_store.record.assert_called_once_with(
            "incident-unparseable",
            "test-unit",
            "plain output\nwith no parser markers",
            "",
        )
        failure_store.record_incident_observation.assert_called_once()

    def test_same_incident_across_runners_is_skipped_within_one_run(self):
        workflow_run = WorkflowRun(
            id=1,
            name="Daily",
            event="schedule",
            head_sha="abc123",
            head_branch="unstable",
            head_repository="valkey-io/valkey",
            is_fork=False,
            conclusion="failure",
            workflow_file="daily.yml",
        )
        alpine_job = FailedJob(
            id=11,
            name="test-alpine-jemalloc",
            conclusion="failure",
            step_name="Run tests",
            matrix_params={"os": "alpine"},
        )
        log_retriever = MagicMock()
        log_retriever.get_job_log.return_value = "parsed log"
        parser_router = MagicMock()
        parser_router.parse.return_value = (
            [
                ParsedFailure(
                    failure_identifier="TestSuite.TestCase",
                    test_name="TestSuite.TestCase",
                    file_path="src/foo.c",
                    error_message="runner-specific assertion text",
                    assertion_details=None,
                    line_number=42,
                    stack_trace=None,
                    parser_type="gtest",
                )
            ],
            "",
            False,
        )
        failure_store = MagicMock()
        failure_store.compute_incident_key.return_value = "incident-123"
        failure_store.get_entry.return_value = MagicMock(status="processing")
        failure_store.has_open_pr.return_value = False

        seen_incidents = {"incident-123"}
        report = _process_failure(
            alpine_job,
            workflow_run,
            "trusted",
            log_retriever,
            parser_router,
            failure_store,
            seen_incidents=seen_incidents,
            max_history_entries=5,
        )

        assert report is None
        failure_store.record.assert_not_called()
        failure_store.record_incident_observation.assert_called_once()

"""Tests for live Valkey repo context helpers."""

from __future__ import annotations

from scripts.config import BotConfig, ReviewerConfig
from scripts.models import ChangedFile, FailureReport, ParsedFailure, PullRequestContext
from scripts.valkey_repo_context import (
    apply_valkey_runtime_defaults,
    augment_reviewer_config_for_valkey,
    build_valkey_repo_context,
)


def _changed_file(path: str) -> ChangedFile:
    return ChangedFile(
        path=path,
        status="modified",
        additions=3,
        deletions=1,
        patch="@@ -1 +1 @@\n-old\n+new",
        contents="content",
        is_binary=False,
    )


def test_build_context_parses_instructions_and_workflow_recipes() -> None:
    context = build_valkey_repo_context(
        "valkey-io/valkey",
        "unstable",
        ref="abc123",
        labels={
            "needs-doc-pr": "This change needs docs.",
            "run-extra-tests": "Run extra tests on this PR.",
        },
        copilot_instructions="Flag DCO and @core-team escalations.",
        instruction_files={
            ".github/instructions/core-engine.instructions.md": (
                "---\napplyTo:\n  - \"src/**/*.{c,h}\"\n---\n"
                "Core engine files need architectural care."
            ),
            ".github/instructions/integration-tests.instructions.md": (
                "---\napplyTo:\n  - \"tests/**/*.tcl\"\n---\n"
                "Avoid timing-dependent Tcl tests."
            ),
        },
        workflow_files={
            ".github/workflows/ci.yml": """
jobs:
  test-ubuntu-latest:
    env:
      BUILD_TLS: yes
    steps:
      - name: Install gtest
        run: sudo apt-get install pkg-config libgtest-dev
      - name: make
        run: make -j4 all-with-unit-tests SERVER_CFLAGS='-Werror'
      - name: test
        run: ./runtest --verbose --dump-logs
      - name: module api test
        run: ./runtest-moduleapi --verbose --dump-logs
""",
        },
    )

    applicable = context.applicable_instructions(["src/server.c", "tests/unit/foo.tcl"])
    assert [instruction.name for instruction in applicable] == [
        "core-engine.instructions.md",
        "integration-tests.instructions.md",
    ]

    assert len(context.workflow_recipes) == 1
    recipe = context.workflow_recipes[0]
    assert recipe.job_id == "test-ubuntu-latest"
    assert recipe.install_commands == ["sudo apt-get install pkg-config libgtest-dev"]
    assert recipe.build_commands == ["make -j4 all-with-unit-tests SERVER_CFLAGS='-Werror'"]
    assert recipe.test_commands == [
        "./runtest --verbose --dump-logs",
        "./runtest-moduleapi --verbose --dump-logs",
    ]


def test_runtime_defaults_and_reviewer_guidance_include_live_valkey_context() -> None:
    context = build_valkey_repo_context(
        "valkey-io/valkey",
        "unstable",
        ref="abc123",
        labels={
            "needs-doc-pr": "This change needs docs.",
            "run-extra-tests": "Run extra tests on this PR.",
            "pending-missing-dco": "Missing DCO label.",
        },
        copilot_instructions="Flag DCO and @core-team escalations.",
        instruction_files={
            ".github/instructions/core-engine.instructions.md": (
                "---\napplyTo:\n  - \"src/**/*.{c,h}\"\n---\n"
                "Core engine files need architectural care."
            ),
        },
        workflow_files={
            ".github/workflows/daily.yml": """
jobs:
  test-ubuntu-jemalloc:
    steps:
      - name: make
        run: make all-with-unit-tests SERVER_CFLAGS='-Werror'
      - name: test
        run: ./runtest --accurate --verbose
""",
        },
    )

    config = BotConfig()
    apply_valkey_runtime_defaults(config, context)
    assert any(
        profile.job_name_pattern.startswith("^test\\-ubuntu\\-jemalloc")
        for profile in config.validation_profiles
    )
    assert "unstable" in config.project.description

    pr = PullRequestContext(
        repo="valkey-io/valkey",
        number=12,
        title="Tighten cluster timeout handling",
        body="Touches tests too.",
        base_sha="base",
        head_sha="head",
        author="alice",
        files=[_changed_file("src/cluster.c"), _changed_file("tests/unit/cluster/foo.tcl")],
        base_ref="unstable",
    )
    reviewer_config = ReviewerConfig(custom_instructions="Local reviewer note.")
    augmented = augment_reviewer_config_for_valkey(reviewer_config, pr, context)

    assert "Local reviewer note." in augmented.custom_instructions
    assert "Live Valkey Maintainer Context" in augmented.custom_instructions
    assert "run-extra-tests" in augmented.custom_instructions
    assert "core-engine.instructions.md" in augmented.custom_instructions


def test_release_branch_guidance_and_job_display_name_profiles() -> None:
    context = build_valkey_repo_context(
        "valkey-io/valkey",
        "unstable",
        ref="abc123",
        labels={},
        copilot_instructions="",
        instruction_files={},
        workflow_files={
            ".github/workflows/weekly.yml": """
jobs:
  release-smoke:
    name: Ubuntu Daily
    steps:
      - name: make
        run: make -j4
      - name: test
        run: ./runtest --verbose
""",
        },
    )

    config = BotConfig()
    apply_valkey_runtime_defaults(config, context)
    assert any(
        "Ubuntu\\ Daily" in profile.job_name_pattern
        for profile in config.validation_profiles
    )

    pr = PullRequestContext(
        repo="valkey-io/valkey",
        number=18,
        title="Release branch backport candidate",
        body="",
        base_sha="base",
        head_sha="head",
        author="alice",
        files=[_changed_file("src/server.c")],
        base_ref="9.1",
    )
    guidance = context.render_review_guidance(pr)
    assert "release branch `9.1`" in guidance
    assert "`weekly.yml`" in guidance


def test_failure_guidance_surfaces_matching_recipe_and_instructions() -> None:
    context = build_valkey_repo_context(
        "valkey-io/valkey",
        "unstable",
        ref="abc123",
        labels={},
        copilot_instructions="",
        instruction_files={
            ".github/instructions/integration-tests.instructions.md": (
                "---\napplyTo:\n  - \"tests/**/*.tcl\"\n---\n"
                "Avoid timing-dependent Tcl tests."
            ),
        },
        workflow_files={
            ".github/workflows/ci.yml": """
jobs:
  test-ubuntu-latest:
    steps:
      - name: make
        run: make -j4
      - name: test
        run: ./runtest --verbose --dump-logs
""",
        },
    )
    report = FailureReport(
        workflow_name="CI",
        job_name="test-ubuntu-latest",
        matrix_params={},
        commit_sha="abc123",
        failure_source="trusted",
        workflow_file="ci.yml",
        repo_full_name="valkey-io/valkey",
        target_branch="unstable",
        parsed_failures=[
            ParsedFailure(
                failure_identifier="cluster/foo",
                test_name="cluster/foo",
                file_path="tests/cluster/foo.tcl",
                error_message="assertion failed",
                assertion_details=None,
                line_number=12,
                stack_trace=None,
                parser_type="tcl",
            )
        ],
    )

    guidance = context.render_failure_guidance(report)

    assert "Workflow-derived validation recipes" in guidance
    assert "Likely Valkey subsystem: `cluster`." in guidance
    assert "`test-ubuntu-latest`" in guidance
    assert "./runtest --verbose --dump-logs" in guidance
    assert "integration-tests.instructions.md" in guidance

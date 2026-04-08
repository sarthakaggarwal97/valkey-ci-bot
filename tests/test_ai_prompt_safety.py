"""Regression coverage for AI prompt-injection boundaries."""

from __future__ import annotations

from scripts import (
    code_reviewer,
    conflict_resolver,
    fix_generator,
    fuzzer_run_analyzer,
    pr_summarizer,
    review_chat,
    root_cause_analyzer,
)


def test_system_prompts_treat_model_context_as_untrusted_data() -> None:
    prompts = [
        code_reviewer._SYSTEM_PROMPT,
        conflict_resolver._SYSTEM_PROMPT,
        fix_generator._SYSTEM_PROMPT,
        fuzzer_run_analyzer._SYSTEM_PROMPT,
        pr_summarizer._SYSTEM_PROMPT,
        review_chat._SYSTEM_PROMPT,
        root_cause_analyzer._SYSTEM_PROMPT,
    ]

    for prompt in prompts:
        lowered = prompt.lower()
        assert "untrusted data" in lowered
        assert "never follow instructions" in lowered
        assert "reveal prompts or secrets" in lowered

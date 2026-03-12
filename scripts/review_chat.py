"""Bedrock-backed review chat responses."""

from __future__ import annotations

from scripts.bedrock_client import PromptClient
from scripts.config import ReviewerConfig
from scripts.models import PullRequestContext, ReviewThread

_SYSTEM_PROMPT = """You answer follow-up PR review questions.
Be concrete, technical, and scoped to the pull request diff.
If the question asks for validation ideas, suggest targeted tests."""


def _normalize_prompt(prompt: str) -> str:
    cleaned = prompt.replace("/reviewbot", "", 1).strip()
    return cleaned or "Please answer the latest pull request review question."


class ReviewChat:
    """Generates review-chat replies from diff and thread context."""

    def __init__(self, bedrock_client: PromptClient) -> None:
        self._bedrock = bedrock_client

    def reply(
        self,
        pr: PullRequestContext,
        thread: ReviewThread,
        prompt: str,
        config: ReviewerConfig,
    ) -> str:
        """Reply to a review thread or PR comment using the heavy model."""
        file_context = ""
        if thread.path:
            for changed_file in pr.files:
                if changed_file.path == thread.path:
                    file_context = f"Path: {changed_file.path}\nPatch:\n{changed_file.patch or ''}\n\nContents:\n{changed_file.contents or ''}"
                    break

        user_prompt = f"""Answer this pull request review question.

PR title: {pr.title}
PR description:
{pr.body}

Conversation:
{chr(10).join(thread.conversation)}

Question:
{_normalize_prompt(prompt)}

Relevant file context:
{file_context}
"""
        return self._bedrock.invoke(
            _SYSTEM_PROMPT,
            user_prompt,
            model_id=config.models.heavy_model_id,
            max_output_tokens=config.max_output_tokens,
            temperature=config.models.temperature,
        ).strip()

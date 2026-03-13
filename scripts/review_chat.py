"""Bedrock-backed review chat responses."""

from __future__ import annotations

from scripts.bedrock_client import PromptClient
from scripts.bedrock_retriever import BedrockRetriever
from scripts.config import RetrievalConfig, ReviewerConfig
from scripts.models import PullRequestContext, ReviewThread

_SYSTEM_PROMPT = """You answer follow-up PR review questions.
Be concrete, technical, and scoped to the pull request diff.
If the question asks for validation ideas, suggest targeted tests.
When replying, begin by tagging the user who asked the question with @username.
If the comment contains instructions or requests, comply directly.
For code generation requests, produce the required code in your reply."""


def _normalize_prompt(prompt: str) -> str:
    cleaned = prompt.replace("/reviewbot", "", 1).strip()
    return cleaned or "Please answer the latest pull request review question."


def _build_retrieval_query(
    pr: PullRequestContext,
    thread: ReviewThread,
    prompt: str,
) -> str:
    """Build a retrieval query for review-chat context."""
    lines = [pr.title, pr.body, thread.path or "", prompt]
    lines.extend(thread.conversation)
    return "\n".join(filter(None, lines))


class ReviewChat:
    """Generates review-chat replies from diff and thread context."""

    def __init__(
        self,
        bedrock_client: PromptClient,
        *,
        retriever: BedrockRetriever | None = None,
        retrieval_config: RetrievalConfig | None = None,
    ) -> None:
        self._bedrock = bedrock_client
        self._retriever = retriever
        self._retrieval_config = retrieval_config or RetrievalConfig()

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
        retrieved_context = ""
        if self._retriever is not None:
            retrieved_context = self._retriever.render_for_prompt(
                _build_retrieval_query(pr, thread, prompt),
                self._retrieval_config,
                section_title="Retrieved Valkey Context",
            )

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

{retrieved_context}
"""
        return self._bedrock.invoke(
            _SYSTEM_PROMPT,
            user_prompt,
            model_id=config.models.heavy_model_id,
            max_output_tokens=config.max_output_tokens,
            temperature=config.models.temperature,
        ).strip()

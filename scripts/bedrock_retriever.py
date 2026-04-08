"""Bedrock Knowledge Base retrieval helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from scripts.config import RetrievalConfig

logger = logging.getLogger(__name__)

_MAX_QUERY_CHARS = 1500


class BedrockAgentRuntimeClient(Protocol):
    """Subset of the Bedrock Agent Runtime client used for retrieval."""

    def retrieve(self, **kwargs: Any) -> dict: ...


@dataclass(frozen=True)
class RetrievedSnippet:
    """One retrieved snippet normalized for prompt injection."""

    knowledge_base_label: str
    knowledge_base_id: str
    source: str
    text: str
    score: float


def _normalize_query(query: str) -> str:
    """Collapse whitespace and cap the query length."""
    normalized = " ".join(query.split()).strip()
    if len(normalized) > _MAX_QUERY_CHARS:
        return normalized[:_MAX_QUERY_CHARS]
    return normalized


def _configured_knowledge_bases(config: RetrievalConfig) -> list[tuple[str, str]]:
    """Return configured knowledge bases as ordered (label, id) pairs."""
    pairs: list[tuple[str, str]] = []
    if config.code_knowledge_base_id:
        pairs.append(("code", config.code_knowledge_base_id))
    if config.docs_knowledge_base_id:
        pairs.append(("docs", config.docs_knowledge_base_id))
    return pairs


def _extract_source(result: dict[str, Any]) -> str:
    """Normalize a retrieval result into a stable source label."""
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        for key in ("path", "x-amz-bedrock-kb-source-uri", "filename"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    location = result.get("location")
    if isinstance(location, dict):
        location_type = location.get("type")
        if location_type == "WEB":
            web_location = location.get("webLocation")
            if isinstance(web_location, dict):
                url = web_location.get("url")
                if isinstance(url, str) and url.strip():
                    return url.strip()
        if location_type == "CUSTOM":
            custom_location = location.get("customDocumentLocation")
            if isinstance(custom_location, dict):
                custom_id = custom_location.get("id")
                if isinstance(custom_id, str) and custom_id.strip():
                    return custom_id.strip()

    return "unknown-source"


class BedrockRetriever:
    """Small wrapper around Bedrock KB retrieval with prompt-friendly output."""

    def __init__(self, client: BedrockAgentRuntimeClient, metric_recorder: Any | None = None):
        self._client = client
        self._cache: dict[tuple[str, str, int], list[RetrievedSnippet]] = {}
        self._metric_recorder = metric_recorder

    def _record_metric(self, name: str, amount: int = 1) -> None:
        recorder = self._metric_recorder
        if callable(recorder):
            recorder(name, amount)

    def retrieve(
        self,
        query: str,
        config: RetrievalConfig,
    ) -> list[RetrievedSnippet]:
        """Retrieve snippets across configured knowledge bases."""
        if not config.enabled:
            return []

        normalized_query = _normalize_query(query)
        if not normalized_query:
            return []

        snippets: list[RetrievedSnippet] = []
        for label, knowledge_base_id in _configured_knowledge_bases(config):
            self._record_metric("bedrock.retrieval.calls")
            cache_key = (
                knowledge_base_id,
                normalized_query,
                max(1, config.max_results_per_knowledge_base),
            )
            cached = self._cache.get(cache_key)
            if cached is None:
                try:
                    response = self._client.retrieve(
                        knowledgeBaseId=knowledge_base_id,
                        retrievalQuery={"text": normalized_query},
                        retrievalConfiguration={
                            "vectorSearchConfiguration": {
                                "numberOfResults": max(
                                    1, config.max_results_per_knowledge_base,
                                ),
                            }
                        },
                    )
                except (BotoCoreError, ClientError) as exc:
                    self._record_metric("bedrock.retrieval.errors")
                    logger.warning(
                        "Bedrock retrieval failed for KB %s: %s",
                        knowledge_base_id,
                        exc,
                    )
                    self._cache[cache_key] = []
                    continue
                cached = self._parse_results(label, knowledge_base_id, response, config)
                self._cache[cache_key] = cached
                self._record_metric("bedrock.retrieval.snippets", len(cached))
            else:
                self._record_metric("bedrock.retrieval.cache_hits")
            snippets.extend(cached)

        snippets.sort(key=lambda item: item.score, reverse=True)

        seen: set[tuple[str, str]] = set()
        deduped: list[RetrievedSnippet] = []
        for snippet in snippets:
            key = (snippet.source, snippet.text)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(snippet)
        if deduped:
            self._record_metric("bedrock.retrieval.hits")
        else:
            self._record_metric("bedrock.retrieval.empty")
        return deduped

    def render_for_prompt(
        self,
        query: str,
        config: RetrievalConfig,
        *,
        section_title: str = "Retrieved Valkey Context",
    ) -> str:
        """Render retrieved snippets into a bounded prompt section."""
        snippets = self.retrieve(query, config)
        if not snippets:
            return ""

        lines = [f"## {section_title}"]
        used = len(lines[0]) + 1
        total_budget = max(1, config.max_total_chars)
        per_result_budget = max(1, config.max_chars_per_result)

        for snippet in snippets:
            header = (
                f"\n### {snippet.knowledge_base_label.upper()} KB | "
                f"{snippet.source} | score={snippet.score:.2f}"
            )
            body = snippet.text[:per_result_budget]
            block = f"{header}\n{body}"
            if used + len(block) > total_budget:
                break
            lines.append(block)
            used += len(block)
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def _parse_results(
        self,
        label: str,
        knowledge_base_id: str,
        response: dict[str, Any],
        config: RetrievalConfig,
    ) -> list[RetrievedSnippet]:
        """Normalize Bedrock retrieval results."""
        raw_results = response.get("retrievalResults")
        if not isinstance(raw_results, list):
            return []

        snippets: list[RetrievedSnippet] = []
        for raw_result in raw_results:
            if not isinstance(raw_result, dict):
                continue
            content = raw_result.get("content")
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if not isinstance(text, str):
                continue
            normalized_text = text.strip()
            if not normalized_text:
                continue
            score = raw_result.get("score")
            snippets.append(
                RetrievedSnippet(
                    knowledge_base_label=label,
                    knowledge_base_id=knowledge_base_id,
                    source=_extract_source(raw_result),
                    text=normalized_text[: max(1, config.max_chars_per_result)],
                    score=float(score) if isinstance(score, (int, float)) else 0.0,
                )
            )
        return snippets

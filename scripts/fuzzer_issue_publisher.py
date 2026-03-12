"""Issue creation/upsert for anomalous Valkey fuzzer runs."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import timezone

from scripts.github_client import retry_github_call
from scripts.models import FuzzerRunAnalysis, FuzzerSignal

logger = logging.getLogger(__name__)

_GENERIC_TITLES = {
    "Validation error message",
    "Fuzzer run ended in failure",
}
_ISSUE_MARKER_PREFIX = "<!-- valkey-ci-bot:fuzzer-issue:"
_OCCURRENCES_MARKER_RE = re.compile(
    r"<!-- valkey-ci-bot:occurrences:(\d+) -->"
)


def _stable_titles(signals: list[FuzzerSignal]) -> list[str]:
    specific = sorted({
        signal.title.strip()
        for signal in signals
        if signal.title.strip() and signal.title not in _GENERIC_TITLES
    })
    if specific:
        return specific
    return sorted({
        signal.title.strip()
        for signal in signals
        if signal.title.strip()
    })


def _fingerprint_for_analysis(analysis: FuzzerRunAnalysis) -> str:
    titles = _stable_titles(analysis.anomalies)
    basis = "|".join([
        analysis.repo,
        analysis.workflow_file,
        *titles[:6],
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def _issue_marker(fingerprint: str) -> str:
    return f"{_ISSUE_MARKER_PREFIX}{fingerprint} -->"


def _issue_title(analysis: FuzzerRunAnalysis) -> str:
    titles = _stable_titles(analysis.anomalies)
    if not titles:
        return "[fuzzer-run] Anomalous Valkey fuzzer behavior detected"
    primary = titles[0]
    if len(titles) == 1:
        return f"[fuzzer-run] {primary}"
    return f"[fuzzer-run] {primary} (+{len(titles) - 1} more)"


def _extract_occurrence_count(body: str | None) -> int:
    if not body:
        return 0
    match = _OCCURRENCES_MARKER_RE.search(body)
    if match is None:
        return 0
    return int(match.group(1))


def _render_issue_body(
    analysis: FuzzerRunAnalysis,
    *,
    fingerprint: str,
    occurrences: int,
) -> str:
    lines = [
        _issue_marker(fingerprint),
        f"<!-- valkey-ci-bot:occurrences:{occurrences} -->",
        "",
        "## Automated Fuzzer Analysis",
        "",
        f"- Repository: `{analysis.repo}`",
        f"- Workflow: `{analysis.workflow_file}`",
        f"- Latest run: {analysis.run_url}",
        f"- Conclusion: `{analysis.conclusion or 'unknown'}`",
        f"- Overall status: `{analysis.overall_status}`",
        f"- Scenario ID: `{analysis.scenario_id or 'unknown'}`",
        f"- Seed: `{analysis.seed or 'unknown'}`",
        f"- Observations of this anomaly: `{occurrences}`",
    ]
    if analysis.reproduction_hint:
        lines.append(f"- Reproduction: `{analysis.reproduction_hint}`")

    lines.extend([
        "",
        "## Summary",
        "",
        analysis.summary,
        "",
        "## Anomalies",
        "",
    ])
    for anomaly in analysis.anomalies:
        lines.append(
            f"- **{anomaly.title}** ({anomaly.severity}): {anomaly.evidence}"
        )

    if analysis.normal_signals:
        lines.extend([
            "",
            "## Expected / Normal Signals Observed",
            "",
        ])
        for signal in analysis.normal_signals[:8]:
            lines.append(f"- {signal}")

    lines.extend([
        "",
        "Automated issue created/updated by `valkey-ci-bot` based on anomalous fuzzer-run analysis.",
        "",
    ])
    return "\n".join(lines)


class FuzzerIssuePublisher:
    """Creates or updates issues for anomalous fuzzer-run analyses."""

    def __init__(self, github_client, *, retries: int = 5) -> None:
        self._gh = github_client
        self._retries = retries

    def upsert_issue(
        self,
        repo_full_name: str,
        analysis: FuzzerRunAnalysis,
    ) -> tuple[str, str]:
        """Create or update an anomaly issue.

        Returns ``(action, issue_url)`` where action is ``created`` or ``updated``.
        """
        repo = retry_github_call(
            lambda: self._gh.get_repo(repo_full_name),
            retries=self._retries,
            description=f"load repository {repo_full_name}",
        )
        fingerprint = _fingerprint_for_analysis(analysis)
        marker = _issue_marker(fingerprint)
        existing = None
        for issue in retry_github_call(
            lambda: list(repo.get_issues(state="open")),
            retries=self._retries,
            description=f"list open issues for {repo_full_name}",
        ):
            if getattr(issue, "pull_request", None) is not None:
                continue
            if marker in (issue.body or ""):
                existing = issue
                break

        if existing is None:
            body = _render_issue_body(analysis, fingerprint=fingerprint, occurrences=1)
            issue = retry_github_call(
                lambda: repo.create_issue(
                    title=_issue_title(analysis),
                    body=body,
                ),
                retries=self._retries,
                description=f"create anomaly issue for {repo_full_name}",
            )
            logger.info(
                "Created anomaly issue #%s for fuzzer run %s.",
                issue.number,
                analysis.run_id,
            )
            return "created", issue.html_url

        occurrences = _extract_occurrence_count(existing.body) + 1
        body = _render_issue_body(
            analysis,
            fingerprint=fingerprint,
            occurrences=occurrences,
        )
        retry_github_call(
            lambda: existing.edit(
                title=_issue_title(analysis),
                body=body,
            ),
            retries=self._retries,
            description=f"update anomaly issue #{existing.number}",
        )
        logger.info(
            "Updated anomaly issue #%s for fuzzer run %s.",
            existing.number,
            analysis.run_id,
        )
        return "updated", existing.html_url

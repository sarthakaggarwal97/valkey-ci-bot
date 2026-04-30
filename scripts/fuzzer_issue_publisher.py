"""Issue creation/upsert for anomalous Valkey fuzzer runs."""

from __future__ import annotations

import hashlib
import logging
import re

from scripts.github_client import retry_github_call
from scripts.models import FuzzerRunAnalysis, FuzzerSignal

logger = logging.getLogger(__name__)

_GENERIC_TITLES = {
    "Validation error message",
    "Fuzzer run ended in failure",
}
_ISSUE_MARKER_PREFIX = "<!-- valkey-ci-agent:fuzzer-issue:"
_OCCURRENCES_MARKER_RE = re.compile(
    r"<!-- valkey-ci-agent:occurrences:(\d+) -->"
)


def _escape_table_cell(value: object) -> str:
    """Return markdown-table-safe text."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", "<br>")


def _issue_verdict(analysis: FuzzerRunAnalysis) -> str:
    """Return a concise maintainer-facing status line."""
    if analysis.triage_verdict == "likely-core-valkey-bug":
        return (
            "This run looks like a likely core Valkey bug and should get maintainer triage."
        )
    if analysis.triage_verdict == "possible-core-valkey-bug":
        return (
            "This run looks like a possible core Valkey bug and should be triaged as such."
        )
    if analysis.triage_verdict == "environmental-or-infra":
        return (
            "This run looks more like environmental or infrastructure noise than a product bug."
        )
    if analysis.triage_verdict == "expected-chaos-noise":
        return "This run looks consistent with expected chaos noise."
    if analysis.overall_status == "anomalous":
        return "This run looks anomalous and likely needs maintainer attention."
    if analysis.overall_status == "warning":
        return "This run needs review before it is treated as expected chaos noise."
    return "This run is being tracked for follow-up."


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
    if analysis.root_cause_category:
        basis = "|".join([
            analysis.repo,
            analysis.workflow_file,
            analysis.root_cause_category,
        ])
    else:
        titles = _stable_titles(analysis.anomalies)
        basis = "|".join([
            analysis.repo,
            analysis.workflow_file,
            *titles[:6],
        ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def _issue_marker(fingerprint: str) -> str:
    return f"{_ISSUE_MARKER_PREFIX}{fingerprint} -->"


def _format_root_cause_title(category: str) -> str:
    """Convert a stable root-cause slug into a maintainer-facing title."""
    cleaned = re.sub(r"[-_]+", " ", category.strip())
    if not cleaned:
        return ""
    return cleaned.title()


def _issue_title(analysis: FuzzerRunAnalysis) -> str:
    if analysis.root_cause_category:
        formatted = _format_root_cause_title(analysis.root_cause_category)
        if formatted:
            return f"[fuzzer-run] {formatted}"
    titles = _stable_titles(analysis.anomalies)
    if not titles:
        return "[fuzzer-run] Anomalous Valkey fuzzer behavior detected"
    return f"[fuzzer-run] {titles[0]}"


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
        f"<!-- valkey-ci-agent:occurrences:{occurrences} -->",
        "",
        "## Fuzzer Run Summary",
        "",
        _issue_verdict(analysis),
        "",
        "### Metadata",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Run | [{analysis.run_id}]({analysis.run_url}) |",
        f"| Conclusion | `{analysis.conclusion or 'unknown'}` |",
        f"| Status | `{analysis.overall_status}` |",
        f"| Triage verdict | `{_escape_table_cell(analysis.triage_verdict)}` |",
    ]
    if analysis.root_cause_category:
        lines.append(
            f"| Root cause | `{_escape_table_cell(analysis.root_cause_category)}` |"
        )
    lines.extend([
        f"| Scenario | `{_escape_table_cell(analysis.scenario_id or 'unknown')}` |",
        f"| Seed | `{_escape_table_cell(analysis.seed or 'unknown')}` |",
        f"| Evidence source | "
        f"`{'raw job log fallback' if analysis.raw_log_fallback_used else 'artifacts and structured logs'}` |",
        f"| Occurrences | {occurrences} |",
        "",
        "### Summary",
        "",
        analysis.summary,
        "",
        "### Action Needed",
        "",
    ])
    if analysis.overall_status == "anomalous":
        lines.append("- Investigate the findings below as likely bug evidence.")
    elif analysis.overall_status == "warning":
        lines.append("- Review whether the warnings persisted past expected chaos recovery.")
    else:
        lines.append("- Track repeat occurrences and escalate if the pattern becomes stronger.")
    if analysis.reproduction_hint:
        lines.append("- Re-run the scenario below if you need to confirm the failure pattern.")

    if analysis.reproduction_hint:
        lines.extend([
            "",
            "**Reproduction**",
            "```",
            analysis.reproduction_hint,
            "```",
        ])

    # Deduplicate anomalies: skip generic titles whose evidence repeats
    # a specific titled finding.
    specific = [a for a in analysis.anomalies if a.title not in _GENERIC_TITLES]
    generic = [a for a in analysis.anomalies if a.title in _GENERIC_TITLES]
    specific_evidence = {a.evidence.strip().lower() for a in specific}
    deduped_generic = [
        a for a in generic
        if a.evidence.strip().lower() not in specific_evidence
    ]
    deduped = specific + deduped_generic

    if deduped:
        critical = [a for a in deduped if a.severity == "critical"]
        warnings = [a for a in deduped if a.severity != "critical"]

        lines.extend(["", "### Findings", ""])
        for anomaly in critical:
            lines.append(f"- Critical: **{anomaly.title}**. {anomaly.evidence}")
        for anomaly in warnings:
            lines.append(f"- Warning: **{anomaly.title}**. {anomaly.evidence}")

    if analysis.normal_signals:
        lines.extend([
            "",
            "<details>",
            f"<summary>Normal signals ({len(analysis.normal_signals)})</summary>",
            "",
        ])
        for signal in analysis.normal_signals:
            lines.append(f"- {signal}")
        lines.extend(["", "</details>"])

    lines.extend([
        "",
        "---",
        "*Automated by `valkey-ci-agent`*",
        "",
    ])
    return "\n".join(lines)


def _render_occurrence_comment(analysis: FuzzerRunAnalysis, *, occurrences: int) -> str:
    """Render a comment body for a new occurrence on an existing issue."""
    lines = [
        f"## Occurrence #{occurrences}",
        "",
        _issue_verdict(analysis),
        "",
        "### Metadata",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Run | [{analysis.run_id}]({analysis.run_url}) |",
        f"| Conclusion | `{analysis.conclusion or 'unknown'}` |",
        f"| Status | `{analysis.overall_status}` |",
        f"| Triage verdict | `{_escape_table_cell(analysis.triage_verdict)}` |",
    ]
    if analysis.root_cause_category:
        lines.append(
            f"| Root cause | `{_escape_table_cell(analysis.root_cause_category)}` |"
        )
    lines.extend([
        f"| Scenario | `{_escape_table_cell(analysis.scenario_id or 'unknown')}` |",
        f"| Seed | `{_escape_table_cell(analysis.seed or 'unknown')}` |",
        f"| Evidence source | "
        f"`{'raw job log fallback' if analysis.raw_log_fallback_used else 'artifacts and structured logs'}` |",
        "",
        "### Summary",
        "",
        analysis.summary,
    ])

    # Deduplicate anomalies the same way as the issue body.
    specific = [a for a in analysis.anomalies if a.title not in _GENERIC_TITLES]
    generic = [a for a in analysis.anomalies if a.title in _GENERIC_TITLES]
    specific_evidence = {a.evidence.strip().lower() for a in specific}
    deduped_generic = [
        a for a in generic
        if a.evidence.strip().lower() not in specific_evidence
    ]
    deduped = specific + deduped_generic

    if deduped:
        critical = [a for a in deduped if a.severity == "critical"]
        warnings = [a for a in deduped if a.severity != "critical"]
        lines.extend(["", "### Findings", ""])
        for anomaly in critical:
            lines.append(f"- Critical: **{anomaly.title}**. {anomaly.evidence}")
        for anomaly in warnings:
            lines.append(f"- Warning: **{anomaly.title}**. {anomaly.evidence}")

    if analysis.reproduction_hint:
        lines.extend([
            "",
            "**Reproduction**",
            "```",
            analysis.reproduction_hint,
            "```",
        ])

    lines.extend([
        "",
        "---",
        "*Automated by `valkey-ci-agent`*",
        "",
    ])
    return "\n".join(lines)


def _bump_occurrence_count(body: str, new_count: int) -> str:
    """Replace the occurrence counter in an existing issue body."""
    return _OCCURRENCES_MARKER_RE.sub(
        f"<!-- valkey-ci-agent:occurrences:{new_count} -->",
        body,
    )


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

        When a matching open issue already exists, a new comment is posted
        with the latest run details and the occurrence counter in the issue
        body is bumped.  The original issue body is preserved so the first
        occurrence remains visible.

        Returns ``(action, issue_url)`` where action is ``created`` or ``updated``.
        """
        repo = retry_github_call(
            lambda: self._gh.get_repo(repo_full_name),
            retries=self._retries,
            description=f"load repository {repo_full_name}",
        )
        labels_to_apply = self._resolve_labels(repo, analysis.suggested_labels)
        fingerprint = _fingerprint_for_analysis(analysis)
        marker = _issue_marker(fingerprint)
        issue_title = _issue_title(analysis)
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
                    title=issue_title,
                    body=body,
                ),
                retries=self._retries,
                description=f"create anomaly issue for {repo_full_name}",
            )
            self._apply_labels(issue, labels_to_apply)
            logger.info(
                "Created anomaly issue #%s for fuzzer run %s.",
                issue.number,
                analysis.run_id,
            )
            return "created", issue.html_url

        # Bump occurrence counter in the original issue body.
        occurrences = _extract_occurrence_count(existing.body) + 1
        updated_body = _bump_occurrence_count(existing.body, occurrences)
        edit_kwargs = {"body": updated_body}
        current_title = getattr(existing, "title", None)
        if not isinstance(current_title, str) or current_title != issue_title:
            edit_kwargs["title"] = issue_title
        retry_github_call(
            lambda: existing.edit(**edit_kwargs),
            retries=self._retries,
            description=f"bump occurrence count on issue #{existing.number}",
        )
        self._apply_labels(existing, labels_to_apply)

        # Post a comment with the new run's full details.
        comment_body = _render_occurrence_comment(analysis, occurrences=occurrences)
        retry_github_call(
            lambda: existing.create_comment(body=comment_body),
            retries=self._retries,
            description=f"post occurrence comment on issue #{existing.number}",
        )
        logger.info(
            "Posted occurrence #%d comment on issue #%s for fuzzer run %s.",
            occurrences,
            existing.number,
            analysis.run_id,
        )
        return "updated", existing.html_url

    def _resolve_labels(
        self,
        repo,
        requested_labels: list[str],
    ) -> list[str]:
        """Return only labels that exist in the target repository."""
        resolved: list[str] = []
        repo_name = str(getattr(repo, "full_name", "<repo>"))
        for label in requested_labels:
            if not label:
                continue
            def load_label(label_name: str = label):
                return repo.get_label(label_name)
            try:
                retry_github_call(
                    load_label,
                    retries=self._retries,
                    description=f"load label {label} for {repo_name}",
                )
            except Exception:
                logger.info("Skipping missing label %s on %s.", label, repo_name)
                continue
            resolved.append(label)
        return resolved

    def _apply_labels(self, issue, labels_to_apply: list[str]) -> None:
        """Add missing labels to an issue without removing existing labels."""
        if not labels_to_apply:
            return
        try:
            def list_labels():
                return list(issue.get_labels())
            existing_labels = {
                str(getattr(label, "name", "")).strip()
                for label in retry_github_call(
                    list_labels,
                    retries=self._retries,
                    description=f"list labels for issue #{issue.number}",
                )
            }
        except Exception:
            existing_labels = set()
        missing = [label for label in labels_to_apply if label not in existing_labels]
        if not missing:
            return
        def add_labels() -> None:
            issue.add_to_labels(*missing)
        retry_github_call(
            add_labels,
            retries=self._retries,
            description=f"add labels to issue #{issue.number}",
        )

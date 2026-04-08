"""Deterministic policy notes for PR review runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re

from scripts.models import PullRequestContext

_SECURITY_PATTERNS = (
    re.compile(r"\bsecurity\b", re.IGNORECASE),
    re.compile(r"\bvulnerability\b", re.IGNORECASE),
    re.compile(r"\bcve-\d{4}-\d+\b", re.IGNORECASE),
)


@dataclass
class ReviewPolicyNote:
    """Deterministic maintainer-policy signals for one PR."""

    missing_dco_commits: list[str] = field(default_factory=list)
    needs_core_team: bool = False
    needs_docs: bool = False
    security_sensitive: bool = False
    governance_changed: bool = False

    @property
    def has_notes(self) -> bool:
        """Return whether any policy signal should be surfaced."""
        return any([
            self.missing_dco_commits,
            self.needs_core_team,
            self.needs_docs,
            self.security_sensitive,
            self.governance_changed,
        ])


def _has_signed_off_by(message: str) -> bool:
    """Return whether a commit message includes a DCO trailer."""
    return bool(re.search(r"^Signed-off-by:\s+.+<.+>$", message, flags=re.MULTILINE))


def _needs_core_team(paths: list[str]) -> bool:
    """Return whether the changed paths imply maintainer escalation."""
    for path in paths:
        pure = PurePosixPath(path)
        basename = pure.name
        if path == "GOVERNANCE.md":
            return True
        if basename in {"replication.c", "rdb.c", "aof.c"}:
            return True
        if basename.startswith("cluster") and basename.endswith(".c"):
            return True
    return False


def _needs_docs(paths: list[str]) -> bool:
    """Return whether changed paths likely need a valkey-doc follow-up."""
    for path in paths:
        if path == "valkey.conf":
            return True
        if path.startswith("src/commands/"):
            return True
    return False


def _security_sensitive(pr: PullRequestContext, paths: list[str]) -> bool:
    """Return whether PR metadata looks security sensitive."""
    joined = "\n".join([pr.title, pr.body, *paths])
    return any(pattern.search(joined) for pattern in _SECURITY_PATTERNS)


def collect_review_policy_note(pr: PullRequestContext) -> ReviewPolicyNote:
    """Collect deterministic maintainer-policy signals from PR context."""
    paths = [changed_file.path for changed_file in pr.files]
    missing_dco_commits = [
        commit.sha
        for commit in pr.commits
        if commit.sha and not _has_signed_off_by(commit.message)
    ]
    return ReviewPolicyNote(
        missing_dco_commits=missing_dco_commits,
        needs_core_team=_needs_core_team(paths),
        needs_docs=_needs_docs(paths),
        security_sensitive=_security_sensitive(pr, paths),
        governance_changed="GOVERNANCE.md" in paths,
    )


def render_review_policy_note(note: ReviewPolicyNote) -> str:
    """Render maintainer-policy signals for the PR summary comment."""
    lines = ["### Maintainer Checklist", ""]
    if not note.has_notes:
        lines.append("No deterministic maintainer-policy signals were triggered.")
        return "\n".join(lines)

    if note.missing_dco_commits:
        commits = ", ".join(f"`{sha[:12]}`" for sha in note.missing_dco_commits)
        lines.append(f"- DCO: commit(s) missing `Signed-off-by` trailers: {commits}.")
    if note.governance_changed:
        lines.append("- Governance: `GOVERNANCE.md` changed; request `@core-team` review.")
    elif note.needs_core_team:
        lines.append(
            "- Core review: changed paths suggest `@core-team` review may be needed."
        )
    if note.needs_docs:
        lines.append(
            "- Docs: changed paths look user-facing; check for a valkey-doc "
            "follow-up and `needs-doc-pr` label."
        )
    if note.security_sensitive:
        lines.append(
            "- Security: PR metadata looks security-sensitive; avoid public "
            "exploit details and use the private security process."
        )
    return "\n".join(lines)

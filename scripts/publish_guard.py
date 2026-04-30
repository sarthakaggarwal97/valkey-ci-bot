"""Central kill-switch to prevent the agent from publishing to valkey-io/valkey.

This module is checked at every GitHub write site (PR creation, issue
creation, comment posting). It errs on the side of safety: if any of the
following is true, publishing is blocked:

1. Environment variable ``VALKEY_CI_AGENT_DRY_RUN`` is set to a truthy value.
2. Environment variable ``VALKEY_CI_AGENT_ALLOW_PUBLISH`` is NOT set to a
   truthy value (opt-in required — default is block).
3. The target repo is ``valkey-io/valkey`` or ``valkey-io/valkey-fuzzer``
   and the ``valkey_io_publish_allowed`` override is not present.

The guard is intentionally strict. To enable publishing, the operator must:

    export VALKEY_CI_AGENT_ALLOW_PUBLISH=1
    export VALKEY_CI_AGENT_ALLOWED_REPOS="sarthakaggarwal97/valkey,…"

Or, for the narrow case of publishing to ``valkey-io/*``:

    export VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH=1

This is a defense-in-depth safeguard on top of workflow-level gating.
If the guard fires, a :class:`PublishBlocked` exception is raised so the
caller can log a clear reason instead of silently dropping writes.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
_UPSTREAM_REPOS = {
    "valkey-io/valkey",
    "valkey-io/valkey-fuzzer",
}


class PublishBlocked(RuntimeError):
    """Raised when the publish guard refuses to allow a GitHub write."""


def _env_true(name: str) -> bool:
    return (os.environ.get(name, "") or "").strip().lower() in _TRUTHY


def _allowed_repos() -> set[str]:
    raw = os.environ.get("VALKEY_CI_AGENT_ALLOWED_REPOS", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def is_publishing_enabled() -> bool:
    """Return True only if the top-level allow flag is set and dry-run is not."""
    if _env_true("VALKEY_CI_AGENT_DRY_RUN"):
        return False
    return _env_true("VALKEY_CI_AGENT_ALLOW_PUBLISH")


def check_publish_allowed(
    target_repo: str,
    *,
    action: str = "write",
    context: str = "",
) -> None:
    """Raise :class:`PublishBlocked` if publishing to target_repo is disallowed.

    Args:
        target_repo: ``owner/name`` of the repo receiving the write.
        action: Short description, e.g. ``"create_pull"``, ``"create_issue"``.
        context: Optional extra context (branch name, PR URL, etc.).
    """
    if _env_true("VALKEY_CI_AGENT_DRY_RUN"):
        raise PublishBlocked(
            f"Blocked {action} on {target_repo}: VALKEY_CI_AGENT_DRY_RUN is set"
            + (f" ({context})" if context else "")
        )

    if not _env_true("VALKEY_CI_AGENT_ALLOW_PUBLISH"):
        raise PublishBlocked(
            f"Blocked {action} on {target_repo}: set VALKEY_CI_AGENT_ALLOW_PUBLISH=1 "
            "to enable publishing"
            + (f" ({context})" if context else "")
        )

    # Extra gate for upstream valkey-io repos: require an explicit second
    # opt-in before writing to them.
    if target_repo in _UPSTREAM_REPOS and not _env_true(
        "VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH"
    ):
        raise PublishBlocked(
            f"Blocked {action} on {target_repo}: upstream publishing requires "
            "VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH=1"
            + (f" ({context})" if context else "")
        )

    allowed = _allowed_repos()
    if allowed and target_repo not in allowed:
        raise PublishBlocked(
            f"Blocked {action} on {target_repo}: not in VALKEY_CI_AGENT_ALLOWED_REPOS "
            f"({sorted(allowed)})"
            + (f" ({context})" if context else "")
        )

    logger.info("Publish guard OK: %s on %s (%s)", action, target_repo, context)

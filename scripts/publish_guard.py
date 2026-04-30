"""Hard block on writes to upstream valkey-io repositories.

The only thing this module protects against is accidentally writing to
``valkey-io/valkey`` or ``valkey-io/valkey-fuzzer``. Fork publishing is
allowed without any environment setup — the ``VALKEY_FORK_REPO`` workflow
variable already controls which repo receives writes.

Writing to an upstream repo requires an explicit opt-in:

    export VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH=1

This is defense-in-depth against a ``VALKEY_FORK_REPO`` misconfiguration
(the workflow default falls back to ``valkey-io/valkey`` when the variable
is unset). If the guard fires, a :class:`PublishBlocked` exception is
raised so the caller can log a clear reason.
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


def check_publish_allowed(
    target_repo: str,
    *,
    action: str = "write",
    context: str = "",
) -> None:
    """Raise :class:`PublishBlocked` if ``target_repo`` is an upstream repo
    and ``VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH`` is not set.

    Writes to any non-upstream repo (including forks) pass through.

    Args:
        target_repo: ``owner/name`` of the repo receiving the write.
        action: Short description, e.g. ``"create_pull"``, ``"create_issue"``.
        context: Optional extra context (branch name, PR URL, etc.).
    """
    if target_repo in _UPSTREAM_REPOS and not _env_true(
        "VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH"
    ):
        raise PublishBlocked(
            f"Blocked {action} on {target_repo}: upstream publishing requires "
            "VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH=1"
            + (f" ({context})" if context else "")
        )
    logger.debug("Publish guard OK: %s on %s (%s)", action, target_repo, context)

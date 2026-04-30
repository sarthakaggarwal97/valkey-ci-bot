"""Helpers for DCO-friendly commit identities and signoff handling."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CommitSigner:
    """Configured commit signer used for automated PRs and backports."""

    name: str = ""
    email: str = ""

    @property
    def configured(self) -> bool:
        """Return whether both signer name and email are available."""
        return bool(self.name.strip() and self.email.strip())

    @property
    def signoff_line(self) -> str:
        """Render the standard DCO signoff trailer."""
        return f"Signed-off-by: {self.name.strip()} <{self.email.strip()}>"


def _env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean-like environment variable."""
    value = os.environ.get(name, "")
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_signer_from_env() -> CommitSigner:
    """Load the configured commit signer from environment variables."""
    return CommitSigner(
        name=os.environ.get("CI_BOT_COMMIT_NAME", "").strip(),
        email=os.environ.get("CI_BOT_COMMIT_EMAIL", "").strip(),
    )


def require_dco_signoff_from_env() -> bool:
    """Return whether automated commit creation must enforce DCO signoff."""
    return _env_flag("CI_BOT_REQUIRE_DCO_SIGNOFF", default=False)


def append_signoff(
    message: str,
    signer: CommitSigner,
    *,
    require_signoff: bool = False,
) -> str:
    """Append a DCO signoff trailer when configured.

    Raises ``ValueError`` when signoff is required but no signer identity is
    configured.
    """
    normalized = message.rstrip()
    if not signer.configured:
        if require_signoff:
            raise ValueError(
                "DCO signoff is required, but CI_BOT_COMMIT_NAME or "
                "CI_BOT_COMMIT_EMAIL is not configured."
            )
        return normalized

    trailer = signer.signoff_line
    if trailer in normalized.splitlines():
        return normalized
    return f"{normalized}\n\n{trailer}"

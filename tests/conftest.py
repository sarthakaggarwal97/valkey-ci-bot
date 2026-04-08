"""Shared fixtures for CI failure agent tests."""

import pytest


@pytest.fixture(autouse=True)
def clear_commit_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep signer-related environment variables deterministic in tests."""
    for name in (
        "CI_BOT_COMMIT_NAME",
        "CI_BOT_COMMIT_EMAIL",
        "CI_BOT_REQUIRE_DCO_SIGNOFF",
    ):
        monkeypatch.delenv(name, raising=False)

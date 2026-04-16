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


@pytest.fixture(autouse=True)
def reset_circuit_breaker() -> None:
    """Reset the Bedrock circuit breaker between tests."""
    from scripts.bedrock_client import _circuit_breaker
    _circuit_breaker.reset()

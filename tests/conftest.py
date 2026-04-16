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
    """Disable the Bedrock circuit breaker during tests.

    Hypothesis property tests run many examples within a single test
    function, so a simple reset-once-per-test is not enough — failures
    from earlier examples would trip the breaker for later ones.
    """
    from scripts.bedrock_client import _circuit_breaker
    _circuit_breaker.reset()
    # Monkey-patch to always allow so accumulated failures never trip it
    _original = _circuit_breaker.allow_request
    _circuit_breaker.allow_request = lambda: True  # type: ignore[assignment]
    yield
    _circuit_breaker.allow_request = _original  # type: ignore[assignment]
    _circuit_breaker.reset()

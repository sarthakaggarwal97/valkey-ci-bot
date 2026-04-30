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
def allow_publish_in_tests(monkeypatch: pytest.MonkeyPatch, request) -> None:
    """Auto-enable the publish guard for tests that mock GitHub writes.

    Tests that exercise the guard itself opt out via the ``disable_publish_autouse``
    marker. In all other tests, we set ALLOW_PUBLISH and ALLOW_VALKEY_IO_PUBLISH
    so tests that mock ``create_pull`` / ``create_issue`` / etc. don't get
    blocked by the guard. Production code is unaffected; the guard still
    enforces safety when these env vars are absent.
    """
    if "disable_publish_autouse" in request.keywords:
        return
    monkeypatch.setenv("VALKEY_CI_AGENT_ALLOW_PUBLISH", "1")
    monkeypatch.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", "1")
    monkeypatch.delenv("VALKEY_CI_AGENT_DRY_RUN", raising=False)
    monkeypatch.delenv("VALKEY_CI_AGENT_ALLOWED_REPOS", raising=False)

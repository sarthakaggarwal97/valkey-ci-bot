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
def allow_upstream_publish_in_tests(
    monkeypatch: pytest.MonkeyPatch, request
) -> None:
    """Auto-enable upstream publishing for tests that mock GitHub writes.

    The publish guard blocks writes to valkey-io/valkey by default. Tests
    that mock PRManager.create_pull, repo.create_issue, etc., would
    otherwise fail with PublishBlocked. Set the opt-in env var so those
    tests run as intended.

    Tests that exercise the guard itself opt out via the
    ``disable_publish_autouse`` marker.
    """
    if "disable_publish_autouse" in request.keywords:
        return
    monkeypatch.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", "1")

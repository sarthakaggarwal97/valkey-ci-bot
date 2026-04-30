"""Tests for scripts/publish_guard.py — the upstream-only kill-switch.

The guard has a single job: block writes to valkey-io/valkey and
valkey-io/valkey-fuzzer unless VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH=1.
Writes to any other repo (including forks) pass through unconditionally.
"""

from __future__ import annotations

import pytest

from scripts.publish_guard import PublishBlocked, check_publish_allowed

# All tests here opt out of the autouse fixture in conftest — the fixture
# sets env vars that would otherwise mask the guard's default behavior.
pytestmark = pytest.mark.disable_publish_autouse


@pytest.fixture
def clean_env(monkeypatch):
    """Clear the upstream-opt-in env var for a clean test."""
    monkeypatch.delenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", raising=False)
    return monkeypatch


# --- Fork writes are always allowed ---

def test_fork_write_allowed_by_default(clean_env):
    check_publish_allowed("sarthakaggarwal97/valkey", action="create_pull")


def test_any_non_upstream_repo_allowed(clean_env):
    check_publish_allowed("some-org/some-repo", action="create_issue")


def test_fork_write_still_allowed_with_opt_in_set(clean_env):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", "1")
    check_publish_allowed("sarthakaggarwal97/valkey", action="create_pull")


# --- Upstream writes are blocked by default ---

def test_upstream_valkey_write_blocked_by_default(clean_env):
    with pytest.raises(PublishBlocked, match="ALLOW_VALKEY_IO_PUBLISH"):
        check_publish_allowed("valkey-io/valkey", action="create_pull")


def test_upstream_fuzzer_write_blocked_by_default(clean_env):
    with pytest.raises(PublishBlocked, match="ALLOW_VALKEY_IO_PUBLISH"):
        check_publish_allowed("valkey-io/valkey-fuzzer", action="create_issue")


# --- Opt-in unblocks upstream ---

def test_upstream_write_allowed_with_opt_in(clean_env):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", "1")
    check_publish_allowed("valkey-io/valkey", action="create_pull")


def test_opt_in_case_insensitive(clean_env):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", "TRUE")
    check_publish_allowed("valkey-io/valkey", action="create_pull")


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_values_accepted(clean_env, value):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", value)
    check_publish_allowed("valkey-io/valkey", action="create_pull")


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_falsy_values_reject(clean_env, value):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", value)
    with pytest.raises(PublishBlocked):
        check_publish_allowed("valkey-io/valkey", action="create_pull")


# --- Error message carries context ---

def test_error_message_includes_action_and_repo(clean_env):
    try:
        check_publish_allowed(
            "valkey-io/valkey", action="create_issue", context="issue #42",
        )
    except PublishBlocked as exc:
        msg = str(exc)
        assert "create_issue" in msg
        assert "valkey-io/valkey" in msg
        assert "issue #42" in msg
    else:
        pytest.fail("Expected PublishBlocked")

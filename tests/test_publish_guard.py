"""Tests for scripts/publish_guard.py — the kill-switch for GitHub writes."""

from __future__ import annotations

import pytest

from scripts.publish_guard import (
    PublishBlocked,
    check_publish_allowed,
    is_publishing_enabled,
)

# All tests in this module opt out of the autouse fixture in conftest that
# sets ALLOW_PUBLISH; these tests verify the guard's default-block behavior.
pytestmark = pytest.mark.disable_publish_autouse


@pytest.fixture
def clean_env(monkeypatch):
    """Clear all publish-guard-related env vars for a clean test."""
    for name in (
        "VALKEY_CI_AGENT_DRY_RUN",
        "VALKEY_CI_AGENT_ALLOW_PUBLISH",
        "VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH",
        "VALKEY_CI_AGENT_ALLOWED_REPOS",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# --- is_publishing_enabled ---

def test_publishing_disabled_by_default(clean_env):
    assert is_publishing_enabled() is False


def test_publishing_enabled_with_allow_flag(clean_env):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_PUBLISH", "1")
    assert is_publishing_enabled() is True


def test_dry_run_overrides_allow(clean_env):
    """Dry-run takes precedence over allow-publish."""
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_PUBLISH", "1")
    clean_env.setenv("VALKEY_CI_AGENT_DRY_RUN", "1")
    assert is_publishing_enabled() is False


# --- check_publish_allowed — default blocks everything ---

def test_default_blocks_any_repo(clean_env):
    with pytest.raises(PublishBlocked, match="ALLOW_PUBLISH"):
        check_publish_allowed("sarthakaggarwal97/valkey", action="create_pull")


def test_default_blocks_valkey_io(clean_env):
    with pytest.raises(PublishBlocked, match="ALLOW_PUBLISH"):
        check_publish_allowed("valkey-io/valkey", action="create_pull")


# --- Dry-run flag ---

def test_dry_run_blocks_with_clear_reason(clean_env):
    clean_env.setenv("VALKEY_CI_AGENT_DRY_RUN", "1")
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_PUBLISH", "1")
    with pytest.raises(PublishBlocked, match="DRY_RUN"):
        check_publish_allowed("sarthakaggarwal97/valkey", action="create_pull")


# --- Valkey-io upstream gate ---

def test_allow_publish_alone_blocks_valkey_io(clean_env):
    """Even with ALLOW_PUBLISH, valkey-io/valkey requires extra opt-in."""
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_PUBLISH", "1")
    with pytest.raises(PublishBlocked, match="ALLOW_VALKEY_IO_PUBLISH"):
        check_publish_allowed("valkey-io/valkey", action="create_pull")


def test_allow_publish_alone_blocks_valkey_fuzzer(clean_env):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_PUBLISH", "1")
    with pytest.raises(PublishBlocked, match="ALLOW_VALKEY_IO_PUBLISH"):
        check_publish_allowed("valkey-io/valkey-fuzzer", action="create_issue")


def test_both_flags_allow_valkey_io_publish(clean_env):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_PUBLISH", "1")
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", "1")
    check_publish_allowed("valkey-io/valkey", action="create_pull")  # should not raise


# --- Allow-list ---

def test_allowed_repos_restricts(clean_env):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_PUBLISH", "1")
    clean_env.setenv("VALKEY_CI_AGENT_ALLOWED_REPOS", "sarthakaggarwal97/valkey")
    # Allowed repo passes
    check_publish_allowed("sarthakaggarwal97/valkey", action="create_pull")
    # Other repo is blocked
    with pytest.raises(PublishBlocked, match="ALLOWED_REPOS"):
        check_publish_allowed("other-user/valkey", action="create_pull")


def test_allowed_repos_multiple(clean_env):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_PUBLISH", "1")
    clean_env.setenv(
        "VALKEY_CI_AGENT_ALLOWED_REPOS",
        "sarthakaggarwal97/valkey, test-org/fork",
    )
    check_publish_allowed("sarthakaggarwal97/valkey", action="create_pull")
    check_publish_allowed("test-org/fork", action="create_pull")


def test_empty_allowed_repos_means_any(clean_env):
    """Empty allow-list means no restriction (other flags still apply)."""
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_PUBLISH", "1")
    check_publish_allowed("any-user/any-repo", action="create_pull")


# --- Error messages include context ---

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


# --- Truthy parsing ---

@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_values_are_accepted(clean_env, value):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_PUBLISH", value)
    assert is_publishing_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_falsy_values_reject(clean_env, value):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_PUBLISH", value)
    assert is_publishing_enabled() is False

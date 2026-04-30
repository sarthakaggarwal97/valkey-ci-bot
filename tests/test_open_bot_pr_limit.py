# Feature: valkey-ci-agent, Property 23: Open bot PR limit
"""Property tests for open bot PR limit.

Property 23: For any state where the target branch has max_open_bot_prs or
more open bot-generated PRs, the bot should not create new PRs.

IF the Bot detects that the unstable branch has more than 3 open bot-generated
PRs, THEN THE Bot SHALL pause all new PR creation until existing PRs are
resolved.

**Validates: Requirements 10.5**
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from scripts.config import BotConfig
from scripts.rate_limiter import RateLimiter

# --- Strategies ---

max_open_prs_st = st.integers(min_value=1, max_value=20)

# Number of open bot PRs currently on the branch
open_bot_pr_count_st = st.integers(min_value=0, max_value=40)


# --- Helpers ---


def _make_mock_pr(is_bot: bool) -> MagicMock:
    """Create a mock PR object with or without the 'bot-fix' label."""
    pr = MagicMock()
    if is_bot:
        label = MagicMock()
        label.name = "bot-fix"
        pr.labels = [label]
    else:
        pr.labels = []
    return pr


def _build_limiter_with_open_prs(
    max_open: int,
    bot_pr_count: int,
) -> RateLimiter:
    """Build a RateLimiter with a mocked GitHub client returning bot_pr_count open bot PRs."""
    config = BotConfig(max_open_bot_prs=max_open)

    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    # Build a list of mock PRs: bot_pr_count with 'bot-fix' label
    bot_prs = [_make_mock_pr(is_bot=True) for _ in range(bot_pr_count)]
    mock_repo.get_pulls.return_value = bot_prs

    limiter = RateLimiter(config, github_client=mock_gh, repo_full_name="owner/repo")
    return limiter


# --- Property Tests ---


@settings(max_examples=100)
@given(max_open=max_open_prs_st, bot_pr_count=open_bot_pr_count_st)
def test_pr_creation_blocked_when_at_or_above_limit(
    max_open: int,
    bot_pr_count: int,
) -> None:
    """When open bot PRs >= max_open_bot_prs, can_create_pr() returns False.

    For any max_open_bot_prs value N, when there are N or more open
    bot-generated PRs, the bot must not create new PRs.

    **Validates: Requirements 10.5**
    """
    assume(bot_pr_count >= max_open)

    limiter = _build_limiter_with_open_prs(max_open, bot_pr_count)
    assert limiter.can_create_pr() is False, (
        f"can_create_pr() returned True with {bot_pr_count} open bot PRs "
        f"(limit is {max_open})"
    )


@settings(max_examples=100)
@given(max_open=max_open_prs_st, bot_pr_count=open_bot_pr_count_st)
def test_pr_creation_allowed_when_below_limit(
    max_open: int,
    bot_pr_count: int,
) -> None:
    """When open bot PRs < max_open_bot_prs and daily limit not reached, can_create_pr() returns True.

    For any max_open_bot_prs value N, when there are fewer than N open
    bot-generated PRs, the bot should allow PR creation (assuming the
    daily limit is not reached).

    **Validates: Requirements 10.5**
    """
    assume(bot_pr_count < max_open)

    limiter = _build_limiter_with_open_prs(max_open, bot_pr_count)
    assert limiter.can_create_pr() is True, (
        f"can_create_pr() returned False with {bot_pr_count} open bot PRs "
        f"(limit is {max_open})"
    )


@settings(max_examples=100)
@given(max_open=max_open_prs_st, bot_pr_count=open_bot_pr_count_st)
def test_open_pr_limit_independent_of_daily_count(
    max_open: int,
    bot_pr_count: int,
) -> None:
    """The open PR limit blocks creation even when the daily PR count is zero.

    This verifies that the open bot PR check is an independent gate —
    even with no PRs created today, exceeding the open PR cap still blocks.

    **Validates: Requirements 10.5**
    """
    assume(bot_pr_count >= max_open)

    limiter = _build_limiter_with_open_prs(max_open, bot_pr_count)
    # Daily count is 0 (fresh limiter), but open PR limit should still block
    assert limiter.get_daily_pr_count() == 0
    assert limiter.can_create_pr() is False


@settings(max_examples=100)
@given(max_open=max_open_prs_st)
def test_no_github_client_does_not_block(
    max_open: int,
) -> None:
    """Without a GitHub client, the open PR check is skipped and does not block.

    When no GitHub client is configured, _exceeds_open_pr_limit() returns
    False, so only the daily limit applies.

    **Validates: Requirements 10.5**
    """
    config = BotConfig(max_open_bot_prs=max_open)
    limiter = RateLimiter(config)  # no github_client

    assert limiter.can_create_pr() is True

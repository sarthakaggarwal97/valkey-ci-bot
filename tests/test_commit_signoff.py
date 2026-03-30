"""Tests for DCO signoff helpers."""

from __future__ import annotations

import pytest

from scripts.commit_signoff import (
    CommitSigner,
    append_signoff,
    load_signer_from_env,
    require_dco_signoff_from_env,
)


def test_append_signoff_adds_trailer_when_signer_is_configured() -> None:
    message = append_signoff(
        "fix: test\nJob: daily\nRoot cause: example",
        CommitSigner(name="Val Key", email="valkey@example.com"),
    )

    assert "Signed-off-by: Val Key <valkey@example.com>" in message


def test_append_signoff_raises_when_required_but_signer_missing() -> None:
    with pytest.raises(ValueError):
        append_signoff("fix: test", CommitSigner(), require_signoff=True)


def test_load_signer_from_env_reads_expected_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI_BOT_COMMIT_NAME", "Val Key")
    monkeypatch.setenv("CI_BOT_COMMIT_EMAIL", "valkey@example.com")

    signer = load_signer_from_env()

    assert signer == CommitSigner(name="Val Key", email="valkey@example.com")


def test_require_dco_signoff_from_env_parses_truthy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI_BOT_REQUIRE_DCO_SIGNOFF", "true")

    assert require_dco_signoff_from_env() is True


def test_require_dco_signoff_from_env_defaults_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CI_BOT_REQUIRE_DCO_SIGNOFF", raising=False)

    assert require_dco_signoff_from_env() is False

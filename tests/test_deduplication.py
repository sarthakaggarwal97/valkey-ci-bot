# Feature: valkey-ci-bot, Property 2: Deduplication skips known failures and allows reprocessing of abandoned
"""Property tests for deduplication logic.

Property 2: For any failure fingerprint that exists in the Failure_Store with
status "open" or "merged", the bot should skip processing and return a skip
result. For any fingerprint with status "abandoned" or not present, processing
should proceed. After reconciliation observes that a bot PR was closed without
merging, the corresponding fingerprint should transition to "abandoned".

**Validates: Requirements 1.5, 9.2, 9.3, 9.4**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.failure_store import FailureStore

# --- Strategies ---

_fingerprint = st.text(min_size=1, max_size=64)
_identifier = st.text(min_size=1, max_size=80)
_error_sig = st.text(min_size=1, max_size=100)
_file_path = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=80,
)

_skip_status = st.sampled_from(["open", "merged"])
_proceed_status = st.sampled_from(["abandoned", "processing"])


# --- Property Tests ---


@settings(max_examples=100)
@given(
    fingerprint=_fingerprint,
    failure_id=_identifier,
    error_sig=_error_sig,
    file_path=_file_path,
    status=_skip_status,
)
def test_has_open_pr_returns_true_for_open_and_merged(
    fingerprint: str,
    failure_id: str,
    error_sig: str,
    file_path: str,
    status: str,
) -> None:
    """has_open_pr returns True when the entry status is 'open' or 'merged'.

    **Validates: Requirements 1.5, 9.3**
    """
    store = FailureStore()
    store.record(
        fingerprint=fingerprint,
        failure_identifier=failure_id,
        error_signature=error_sig,
        file_path=file_path,
        status=status,
    )
    assert store.has_open_pr(fingerprint) is True


@settings(max_examples=100)
@given(
    fingerprint=_fingerprint,
    failure_id=_identifier,
    error_sig=_error_sig,
    file_path=_file_path,
    status=_proceed_status,
)
def test_has_open_pr_returns_false_for_abandoned_and_processing(
    fingerprint: str,
    failure_id: str,
    error_sig: str,
    file_path: str,
    status: str,
) -> None:
    """has_open_pr returns False when the entry status is 'abandoned' or 'processing'.

    **Validates: Requirements 9.2, 9.4**
    """
    store = FailureStore()
    store.record(
        fingerprint=fingerprint,
        failure_identifier=failure_id,
        error_signature=error_sig,
        file_path=file_path,
        status=status,
    )
    assert store.has_open_pr(fingerprint) is False


@settings(max_examples=100)
@given(fingerprint=_fingerprint)
def test_has_open_pr_returns_false_for_unknown_fingerprint(
    fingerprint: str,
) -> None:
    """has_open_pr returns False when the fingerprint is not in the store.

    **Validates: Requirements 9.2**
    """
    store = FailureStore()
    assert store.has_open_pr(fingerprint) is False


@settings(max_examples=100)
@given(
    fingerprint=_fingerprint,
    failure_id=_identifier,
    error_sig=_error_sig,
    file_path=_file_path,
    initial_status=st.sampled_from(["open", "merged", "processing"]),
)
def test_mark_abandoned_transitions_status(
    fingerprint: str,
    failure_id: str,
    error_sig: str,
    file_path: str,
    initial_status: str,
) -> None:
    """mark_abandoned transitions any existing entry to 'abandoned', enabling reprocessing.

    **Validates: Requirements 9.4**
    """
    store = FailureStore()
    store.record(
        fingerprint=fingerprint,
        failure_identifier=failure_id,
        error_signature=error_sig,
        file_path=file_path,
        status=initial_status,
    )
    store.mark_abandoned(fingerprint)

    entry = store.entries[fingerprint]
    assert entry.status == "abandoned"
    # After abandonment, has_open_pr should return False (reprocessing allowed)
    assert store.has_open_pr(fingerprint) is False

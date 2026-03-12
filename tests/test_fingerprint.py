# Feature: valkey-ci-bot, Property 19: Fingerprint determinism
"""Property tests for failure fingerprint determinism.

Property 19: For any two failures with identical (failure_identifier,
error_signature, file_path) tuples, the computed fingerprints should be equal.
For any two failures with different tuples, the fingerprints should differ
(with high probability).

**Validates: Requirements 9.1**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.failure_store import FailureStore

# --- Strategies ---

# Use printable text that exercises a range of characters including null bytes,
# unicode, and typical path/identifier characters.
failure_text = st.text(min_size=0, max_size=100)

file_path_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z"), min_codepoint=32, max_codepoint=126),
    min_size=0,
    max_size=80,
)


# --- Property Tests ---


@settings(max_examples=100)
@given(
    failure_identifier=failure_text,
    error_signature=failure_text,
    file_path=file_path_text,
)
def test_fingerprint_determinism_same_inputs(
    failure_identifier: str,
    error_signature: str,
    file_path: str,
) -> None:
    """Property 19 (part 1): Same inputs always produce the same fingerprint.

    **Validates: Requirements 9.1**
    """
    fp1 = FailureStore.compute_fingerprint(failure_identifier, error_signature, file_path)
    fp2 = FailureStore.compute_fingerprint(failure_identifier, error_signature, file_path)
    assert fp1 == fp2


@settings(max_examples=100)
@given(
    id_a=failure_text,
    sig_a=failure_text,
    path_a=file_path_text,
    id_b=failure_text,
    sig_b=failure_text,
    path_b=file_path_text,
)
def test_fingerprint_collision_resistance(
    id_a: str,
    sig_a: str,
    path_a: str,
    id_b: str,
    sig_b: str,
    path_b: str,
) -> None:
    """Property 19 (part 2): Different input tuples produce different fingerprints.

    **Validates: Requirements 9.1**
    """
    # Only assert when the tuples actually differ
    if (id_a, sig_a, path_a) == (id_b, sig_b, path_b):
        return

    fp_a = FailureStore.compute_fingerprint(id_a, sig_a, path_a)
    fp_b = FailureStore.compute_fingerprint(id_b, sig_b, path_b)
    assert fp_a != fp_b

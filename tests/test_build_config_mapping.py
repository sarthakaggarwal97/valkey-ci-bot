# Feature: valkey-ci-agent, Property 11: Validation build configuration mapping
"""Property test for validation build configuration mapping.

Property 11: For any failing job name and matrix parameters, the
Validation_Runner should select exactly one ValidationProfile and produce
build flags and test commands that match the original CI job's configuration
(SANITIZER, BUILD_TLS, MALLOC, architecture flags).

**Validates: Requirements 5.2, 5.3**
"""

from __future__ import annotations

import re

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from scripts.config import ValidationProfile
from scripts.validation_runner import _match_profile

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Simple safe text for env values, commands, etc.
safe_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"), min_codepoint=48, max_codepoint=122,
    ),
    min_size=1,
    max_size=20,
)

# Strategy for env dicts (CI build flags like SANITIZER, BUILD_TLS, MALLOC)
env_strategy = st.fixed_dictionaries({}, optional={
    "SANITIZER": st.sampled_from(["address", "undefined", "thread", "memory"]),
    "BUILD_TLS": st.sampled_from(["yes", "no"]),
    "MALLOC": st.sampled_from(["libc", "jemalloc"]),
    "ARCH": st.sampled_from(["x64", "arm64", "i386"]),
})

# Strategy for matrix params
matrix_param_strategy = st.fixed_dictionaries({}, optional={
    "os": st.sampled_from(["ubuntu-latest", "macos-latest", "debian-11", "almalinux-9"]),
    "arch": st.sampled_from(["x64", "arm64", "i386"]),
    "build_tls": st.sampled_from(["yes", "no"]),
})

# Strategy for a list of shell commands
command_list_strategy = st.lists(
    st.sampled_from([
        "make -j",
        "cmake -S . -B build",
        "cmake --build build -j",
        "make -j BUILD_TLS=yes",
        "./runtest --test {test_name}",
        "./runtest --tls --test {test_name}",
        "make test",
    ]),
    min_size=1,
    max_size=3,
)

# Literal job name segments used to build both patterns and matching names
_JOB_SEGMENTS = [
    "test-sanitizer-address",
    "test-sanitizer-undefined",
    "test-ubuntu-latest-cmake-tls",
    "test-ubuntu-latest-cmake",
    "test-macos-latest",
    "build-debian-11",
    "build-almalinux-9",
    "test-32bit",
    "test-rdma",
    "test-unit",
    "test-cluster",
    "test-sentinel",
]


@st.composite
def profile_and_matching_job(draw):
    """Generate a ValidationProfile together with a job name and matrix
    params that are guaranteed to match it.

    The strategy picks a literal job name, uses it as both the regex
    pattern (anchored) and the job name to query with.  Matrix params
    on the profile are always a subset of the job's matrix params.
    """
    job_name = draw(st.sampled_from(_JOB_SEGMENTS))
    pattern = f"^{re.escape(job_name)}$"

    profile_matrix = draw(matrix_param_strategy)
    # Job matrix is a superset of profile matrix (may have extra keys)
    extra_matrix = draw(matrix_param_strategy)
    job_matrix = {**extra_matrix, **profile_matrix}

    env = draw(env_strategy)
    build_cmds = draw(command_list_strategy)
    test_cmds = draw(st.one_of(
        st.just([]),  # build-only
        command_list_strategy,
    ))
    install_cmds = draw(st.one_of(
        st.just([]),
        st.lists(st.sampled_from([
            "apt-get install -y libssl-dev",
            "yum install -y openssl-devel",
        ]), min_size=1, max_size=2),
    ))

    profile = ValidationProfile(
        job_name_pattern=pattern,
        matrix_params=profile_matrix,
        env=env,
        install_commands=install_cmds,
        build_commands=build_cmds,
        test_commands=test_cmds,
    )

    return profile, job_name, job_matrix


@st.composite
def profile_and_non_matching_job(draw):
    """Generate a ValidationProfile and a job name that does NOT match it."""
    segments = list(_JOB_SEGMENTS)
    idx = draw(st.integers(min_value=0, max_value=len(segments) - 1))
    profile_segment = segments[idx]
    # Pick a different segment for the job name
    other_segments = [s for s in segments if s != profile_segment]
    job_name = draw(st.sampled_from(other_segments))

    pattern = f"^{re.escape(profile_segment)}$"
    profile = ValidationProfile(
        job_name_pattern=pattern,
        matrix_params={},
        env=draw(env_strategy),
        build_commands=draw(command_list_strategy),
        test_commands=draw(command_list_strategy),
    )
    return profile, job_name


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

@given(data=profile_and_matching_job())
@settings(max_examples=100)
def test_matching_job_selects_exactly_one_profile_with_correct_config(data):
    """Property 11: For any failing job name and matrix parameters that
    match a profile's regex and matrix subset, _match_profile returns
    that profile with the correct env, build_commands, and test_commands.

    **Validates: Requirements 5.2, 5.3**
    """
    profile, job_name, job_matrix = data

    result = _match_profile(job_name, job_matrix, [profile])

    # Exactly one profile is selected (the one we created)
    assert result is not None
    assert result is profile

    # The selected profile carries the correct build configuration
    assert result.env == profile.env
    assert result.build_commands == profile.build_commands
    assert result.test_commands == profile.test_commands
    assert result.install_commands == profile.install_commands
    assert result.matrix_params == profile.matrix_params


@given(data=profile_and_matching_job())
@settings(max_examples=100)
def test_first_matching_profile_wins_among_multiple(data):
    """When multiple profiles could match, the first one in the list is
    selected — ensuring deterministic, exactly-one selection.

    **Validates: Requirements 5.2, 5.3**
    """
    target_profile, job_name, job_matrix = data

    # Create a second catch-all profile that would also match
    catchall = ValidationProfile(
        job_name_pattern=".*",
        matrix_params={},
        env={"CATCHALL": "true"},
        build_commands=["make catchall"],
        test_commands=["./run-catchall"],
    )

    result = _match_profile(job_name, job_matrix, [target_profile, catchall])

    # The first matching profile must be selected
    assert result is target_profile
    assert result.env == target_profile.env
    assert result.build_commands == target_profile.build_commands


@given(data=profile_and_non_matching_job())
@settings(max_examples=100)
def test_non_matching_job_returns_none(data):
    """When the job name does not match any profile's regex, _match_profile
    returns None — no spurious profile selection.

    **Validates: Requirements 5.2, 5.3**
    """
    profile, job_name = data

    result = _match_profile(job_name, {}, [profile])
    assert result is None


@given(
    profile_data=profile_and_matching_job(),
    mismatched_value=st.sampled_from(["ubuntu-latest", "macos-latest", "debian-11"]),
)
@settings(max_examples=100)
def test_matrix_subset_mismatch_rejects_profile(profile_data, mismatched_value):
    """When the profile requires matrix params that the job does NOT have
    (or has different values), the profile is not selected.

    **Validates: Requirements 5.2, 5.3**
    """
    profile, job_name, _ = profile_data

    # Only test when the profile actually requires matrix params
    assume(len(profile.matrix_params) > 0)

    # Build a job matrix where one of the profile's required keys has a
    # different value
    first_key = next(iter(profile.matrix_params))
    original_value = profile.matrix_params[first_key]
    assume(mismatched_value != original_value)

    bad_matrix = {first_key: mismatched_value}

    result = _match_profile(job_name, bad_matrix, [profile])
    assert result is None

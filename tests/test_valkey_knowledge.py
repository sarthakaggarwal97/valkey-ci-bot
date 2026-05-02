"""Tests for scripts.valkey_knowledge.

These tests verify:
- `infer_subsystems` maps real Valkey source paths to the expected tags.
- `get_subsystem_context` returns non-empty prose for every mapped
  subsystem and an empty string when no path matches.
- `get_divergence_block` carries the key symbol renames so Claude sees
  them in every review.
"""

from __future__ import annotations

from scripts.valkey_knowledge import (
    _PATH_TO_SUBSYSTEM,
    _SUBSYSTEM_CONTEXT,
    get_divergence_block,
    get_subsystem_context,
    infer_subsystems,
)


def test_infer_subsystems_maps_cluster_paths():
    subsystems = infer_subsystems(
        [
            "src/cluster.c",
            "src/cluster_legacy.c",
            "src/cluster_migrateslots.c",
        ]
    )
    assert subsystems == {"cluster"}


def test_infer_subsystems_maps_io_threads_to_networking():
    # io_threads is listed under the networking subsystem.
    subsystems = infer_subsystems(["src/io_threads.c", "src/networking.c"])
    assert subsystems == {"networking"}


def test_infer_subsystems_maps_data_structure_paths():
    subsystems = infer_subsystems(
        [
            "src/t_hash.c",
            "src/t_list.c",
            "src/hashtable.c",
            "src/kvstore.c",
            "src/listpack.c",
            "src/quicklist.c",
            "src/vset.c",
            "src/object.c",
        ]
    )
    assert subsystems == {"data-structures"}


def test_infer_subsystems_maps_memory_paths():
    subsystems = infer_subsystems(
        ["src/zmalloc.c", "src/evict.c", "src/defrag.c", "src/lazyfree.c"]
    )
    assert subsystems == {"memory"}


def test_infer_subsystems_maps_module_paths():
    subsystems = infer_subsystems(
        ["src/module.c", "src/valkeymodule.h", "src/redismodule.h"]
    )
    assert subsystems == {"modules"}


def test_infer_subsystems_maps_persistence_paths():
    subsystems = infer_subsystems(["src/rdb.c", "src/aof.c"])
    assert subsystems == {"persistence"}


def test_infer_subsystems_maps_scripting_paths():
    subsystems = infer_subsystems(
        ["src/eval.c", "src/functions.c", "src/script.c", "src/scripting_engine.c"]
    )
    assert subsystems == {"scripting"}


def test_infer_subsystems_maps_test_paths():
    subsystems = infer_subsystems(
        ["tests/unit/type/hash.tcl", "tests/integration/replication.tcl"]
    )
    assert subsystems == {"testing"}


def test_infer_subsystems_multiple_subsystems():
    subsystems = infer_subsystems(
        ["src/cluster.c", "src/replication.c", "src/zmalloc.c"]
    )
    assert subsystems == {"cluster", "replication", "memory"}


def test_infer_subsystems_ignores_unmatched_paths():
    subsystems = infer_subsystems(
        ["README.md", "CONTRIBUTING.md", "some-random-file.txt"]
    )
    assert subsystems == set()


def test_infer_subsystems_empty_input():
    assert infer_subsystems([]) == set()


def test_get_subsystem_context_non_empty_for_cluster():
    ctx = get_subsystem_context(["src/cluster.c"])
    assert ctx
    assert "Cluster" in ctx
    # Should mention the file split that Redis-trained models get wrong.
    assert "cluster_legacy" in ctx


def test_get_subsystem_context_non_empty_for_each_subsystem():
    # Pick one representative path per subsystem tag and confirm the
    # block is actually rendered (catches a missing key in
    # `_SUBSYSTEM_CONTEXT`).
    representative_paths = {
        "cluster": "src/cluster.c",
        "sentinel": "src/sentinel.c",
        "networking": "src/networking.c",
        "replication": "src/replication.c",
        "persistence": "src/rdb.c",
        "modules": "src/module.c",
        "scripting": "src/eval.c",
        "security": "src/acl.c",
        "data-structures": "src/hashtable.c",
        "memory": "src/zmalloc.c",
        "testing": "tests/unit/type/hash.tcl",
    }
    for subsystem, path in representative_paths.items():
        ctx = get_subsystem_context([path])
        assert ctx, f"missing context for {subsystem}"
        # `_SUBSYSTEM_CONTEXT` must have an entry for every subsystem
        # that `_PATH_TO_SUBSYSTEM` can emit.
        assert subsystem in _SUBSYSTEM_CONTEXT


def test_get_subsystem_context_deterministic_ordering():
    # Same inputs → identical output, regardless of insertion order.
    a = get_subsystem_context(["src/cluster.c", "src/zmalloc.c"])
    b = get_subsystem_context(["src/zmalloc.c", "src/cluster.c"])
    assert a == b
    assert a  # non-empty


def test_get_subsystem_context_empty_when_no_match():
    assert get_subsystem_context([]) == ""
    assert get_subsystem_context(["README.md", "CHANGELOG.md"]) == ""


def test_every_subsystem_tag_has_a_context_block():
    # The tags referenced by `_PATH_TO_SUBSYSTEM` must all have blocks.
    tags = set(_PATH_TO_SUBSYSTEM.values())
    missing = tags - set(_SUBSYSTEM_CONTEXT.keys())
    assert not missing, f"subsystem tags without context blocks: {missing}"


def test_get_divergence_block_mentions_renamed_symbols():
    block = get_divergence_block()
    # Key symbol renames — Claude must not "correct" these back to Redis names.
    assert "serverCommand" in block
    assert "redisCommand" in block  # Mentioned as the old name
    assert "ValkeyModule_" in block
    assert "RedisModule_" in block  # Mentioned as the old / compat name
    assert "valkey_malloc" in block
    assert "zmalloc" in block
    assert "replicaof" in block
    assert "slaveof" in block
    assert "primary" in block
    assert "replica" in block


def test_get_divergence_block_mentions_structural_changes():
    block = get_divergence_block()
    assert "hashtable" in block
    assert "kvstore" in block
    assert "shouldEmbedStringObject" in block
    assert "IOThreadsBeforeSleep" in block or "IOThreadsAfterSleep" in block


def test_get_divergence_block_mentions_deprecated_configs():
    block = get_divergence_block()
    assert "events-per-io-thread" in block
    assert "io-threads-do-reads" in block


def test_get_divergence_block_is_non_empty_and_markdown():
    block = get_divergence_block()
    assert block.strip()
    # Written as a markdown block so it slots cleanly into the prompt.
    assert block.lstrip().startswith("##")

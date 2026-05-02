"""Valkey-specific knowledge injected into Claude review prompts.

Claude's training data is dominated by Redis OSS (pre-fork). In Valkey,
a number of symbols, configs, and subsystems have been renamed, rewritten,
or replaced. This module carries a compact, verified-against-source summary
of those divergences plus per-subsystem context blocks that are only loaded
when the diff touches the matching code path.

All facts in this module were verified against the Valkey source tree
under /src at the time of writing. Keep the bullets concise — they are
prompt tokens, not documentation.
"""

from __future__ import annotations

_VALKEY_DIVERGENCE_BLOCK = """\
## Valkey vs Redis — what the model's training data gets wrong

Valkey forked from Redis OSS 7.2.4 and has diverged since. Do not flag
Valkey-current symbols as "wrong" because Redis used a different name.
The items below are verified against the current Valkey source tree.

### Renamed symbols (Redis name → Valkey name)
- `struct redisCommand` → `struct serverCommand` (src/server.h). `lookupCommand()`,
  `commandlogPushCurrentCommand()`, ACL checks all take `struct serverCommand *`.
- `RedisModule_*` API → `ValkeyModule_*` API. `valkeymodule.h` is canonical;
  `redismodule.h` is a thin compatibility shim that re-defines the old
  `REDISMODULE_*` macros onto `VALKEYMODULE_*`. Both prefixes are registered
  at load time, and modules may export either `ValkeyModule_OnLoad` or
  `RedisModule_OnLoad`.
- `zmalloc`/`zcalloc`/`zrealloc`/`zfree` are `#define`-aliases for
  `valkey_malloc`/`valkey_calloc`/`valkey_realloc`/`valkey_free` in
  `src/zmalloc.h`. Either name is valid; prefer matching surrounding code.
  Allocation failure is fatal by default — reviewers should NOT demand a
  NULL-check on `zmalloc` return. Use `ztrymalloc`/`ztrycalloc`/`ztryrealloc`
  when a caller is expected to handle OOM.
- `master`/`slave` terminology replaced by `primary`/`replica` throughout
  networking, replication, and server state (e.g. `server.primary`,
  `server.replicas`, `c->flag.primary`, `c->flag.replica`, `CLIENT_TYPE_PRIMARY`,
  `CLIENT_TYPE_REPLICA`). The legacy `master`/`slave` names may still appear
  in wire-protocol replies and a few compat shims; do not flag those.

### Renamed / deprecated configs
- `slaveof` → `replicaof` (both accepted; `replicaof` is canonical, registered
  via `createSpecialConfig("replicaof", "slaveof", ...)` in src/config.c).
- Deprecated, silently-ignored configs (see `deprecated_configs[]` in
  `loadServerConfigFromString`): `list-max-ziplist-entries`,
  `list-max-ziplist-value`, `lua-replicate-commands`, `io-threads-do-reads`,
  `dynamic-hz`, `events-per-io-thread`. Do NOT flag diffs that remove these.

### Structural changes
- Main keyspace is `kvstore *keys` (and `kvstore *expires`,
  `kvstore *keys_with_volatile_items`) on `serverDb`, not a raw `dict *`.
  `kvstore` is built on `hashtable` (`src/hashtable.c`), the Valkey-native
  replacement for Redis's `dict`. `dict` still exists and is used for
  smaller maps (scripts, info sections, pub/sub client lists use `hashtable`
  via `getClientPubSubChannels`).
- String embedding: `shouldEmbedStringObject()` (src/object.c) decides
  embed-vs-raw based on a 128-byte budget covering `robj` + optional key
  + optional expire + sds header + value. Embedded strings may carry an
  embedded key and expire in the same allocation — this is Valkey-only
  and not present in Redis 7.2.
- IO-threading was rewritten. `src/io_threads.c` uses an ignition/scaling
  policy: `IOThreadsBeforeSleep()` / `IOThreadsAfterSleep()` drive jobs
  through a shared SPMC inbox, an MPSC outbox, and per-thread SPSC private
  inboxes. Job requests are tagged pointers (`JOB_REQ_READ_CLIENT`,
  `JOB_REQ_WRITE_CLIENT`, `JOB_REQ_FREE_ARGV`, `JOB_REQ_FREE_OBJ`,
  `JOB_REQ_POLL`, `JOB_REQ_ACCEPT`). There is no `io-threads-do-reads`
  knob and no `events-per-io-thread` knob anymore.
- RDB version is 80 (`RDB_VERSION` in src/rdb.h); Valkey accepts a range
  of foreign RDB versions via `RDB_VERSION_MAP`. Redis-compat version is
  pinned at `REDIS_VERSION "7.2.4"` in src/version.h — do not "update" it.

### Critical review rules
- Do not rename `server->primary` / `server->replicas` back to `master`/
  `slaves`. Do not "fix" `serverCommand` to `redisCommand`. Do not "fix"
  `ValkeyModule_*` to `RedisModule_*`.
- Do not require NULL checks on plain `zmalloc`/`zcalloc`/`zrealloc`
  callers — those abort on OOM.
- New modules should include `valkeymodule.h`. Flagging use of
  `redismodule.h` in existing third-party modules is fine; flagging it in
  the compat shim itself is not.
- `dict`-based code is NOT automatically wrong. `hashtable` replaced `dict`
  only for the main keyspace/pubsub; many smaller dictionaries still use
  `dict` intentionally.
"""

# Map file path prefixes to subsystem tags. First matching prefix wins;
# ordering matters for more-specific prefixes (e.g. "src/cluster_" before
# "src/cluster"). These must match real directory/file layout under
# valkey/src.
_PATH_TO_SUBSYSTEM: dict[str, str] = {
    "src/cluster": "cluster",
    "src/sentinel": "sentinel",
    "src/networking": "networking",
    "src/replication": "replication",
    "src/aof": "persistence",
    "src/rdb": "persistence",
    "src/module": "modules",
    "src/valkeymodule": "modules",
    "src/redismodule": "modules",
    "src/eval": "scripting",
    "src/script": "scripting",
    "src/functions": "scripting",
    "src/scripting_engine": "scripting",
    "src/acl": "security",
    "src/io_threads": "networking",
    "src/t_": "data-structures",
    "src/hashtable": "data-structures",
    "src/kvstore": "data-structures",
    "src/listpack": "data-structures",
    "src/quicklist": "data-structures",
    "src/vset": "data-structures",
    "src/object": "data-structures",
    "src/zmalloc": "memory",
    "src/evict": "memory",
    "src/defrag": "memory",
    "src/lazyfree": "memory",
    "tests/": "testing",
}

# Per-subsystem context blocks (only loaded when relevant). Each block is
# 5-10 lines of genuinely non-obvious facts that a Redis-trained model
# would get wrong. Keep these tight — they are always appended to the
# divergence block above.
_SUBSYSTEM_CONTEXT: dict[str, str] = {
    "cluster": (
        "### Cluster\n"
        "- Cluster code is split across `src/cluster.c` (common entry points, "
        "slot routing via `clusterSlotByCommand`), `src/cluster_legacy.c` "
        "(the gossip-based legacy bus with `clusterLink`), "
        "`src/cluster_migrateslots.c`, and `src/cluster_slot_stats.c`.\n"
        "- `clusterNode` is an opaque forward-declared type in `src/cluster.h` "
        "(`typedef struct _clusterNode clusterNode`); the definition lives in "
        "`cluster_legacy.h`. Do not expect Redis-style direct field access.\n"
        "- Slot-to-keys indexing uses `kvstore` (per-slot hashtables), not the "
        "Redis 7 `clusterSlotToKeyAdd`-on-a-radix-tree model."
    ),
    "sentinel": (
        "### Sentinel\n"
        "- `src/sentinel.c` retains the Redis 2009-2012 copyright header and "
        "much of the original Redis terminology internally. External-facing "
        "output still uses `master`/`slave` in some places for wire "
        "compatibility — flagging those as 'should be primary/replica' is "
        "usually wrong."
    ),
    "networking": (
        "### Networking / IO threads\n"
        "- `struct client` lives in `src/server.h`. Client flags are a bitfield "
        "struct (`c->flag.primary`, `c->flag.replica`, `c->flag.monitor`, "
        "`c->flag.pubsub`), not `c->flags & CLIENT_MASTER`.\n"
        "- IO threading uses queues from `src/queues.h`: `spmcQueue` (shared "
        "inbox main→io), `mpscQueue` (shared outbox io→main), and per-thread "
        "`spscQueue` private inboxes. Jobs are tagged pointers using the low "
        "3 bits. Touching this requires matching the lock-free discipline — "
        "comment on memory ordering, not style.\n"
        "- `IOThreadsBeforeSleep`/`IOThreadsAfterSleep` drive the "
        "ignition/scaling policy; thresholds are `IO_IGNITION_CPU_SYS`, "
        "`IO_IGNITION_CPU_USER`, `IO_IGNITION_EVENTS`, `IO_SAMPLE_RATE_MS`.\n"
        "- There is NO `io-threads-do-reads` or `events-per-io-thread` config "
        "anymore (both are in `deprecated_configs[]`)."
    ),
    "replication": (
        "### Replication\n"
        "- Server-side fields use `primary`/`replicas` naming: "
        "`server.primary` (the primary-connection client), "
        "`server.primary_repl_offset`, `server.replicas`, "
        "`server.repl_backlog`. Protocol-level tokens and some legacy "
        "field names may still say `master`.\n"
        "- Partial resync still uses PSYNC/replid/offset semantics; the "
        "`repl_backlog` is a single circular buffer shared across replicas."
    ),
    "persistence": (
        "### Persistence (RDB / AOF)\n"
        "- `RDB_VERSION` is 80 (`src/rdb.h`). `RDB_VERSION_MAP` / "
        "`RDB_FOREIGN_VERSION_MIN`/`MAX` gate which foreign (Redis) RDB "
        "versions are accepted for load — bumping `RDB_VERSION` without "
        "updating the map is a bug.\n"
        "- AOF preamble writes an RDB using `rdbSaveRio(REPLICA_REQ_NONE, "
        "RDB_VERSION, ...)` — keep the version argument wired to the macro, "
        "not hard-coded.\n"
        "- `REDIS_VERSION` in `src/version.h` is pinned at `7.2.4` for "
        "wire-protocol compatibility. Do not 'update' it."
    ),
    "modules": (
        "### Modules\n"
        "- Canonical API is `ValkeyModule_*` in `src/valkeymodule.h`. "
        "`src/redismodule.h` is a documented compatibility snapshot of the "
        "Redis 7.2.4 module API and simply includes `valkeymodule.h`.\n"
        "- `moduleRegisterApi` in `src/module.c` registers every API entry "
        "under both `RedisModule_<name>` and `ValkeyModule_<name>` — "
        "existing modules linking against either prefix keep working.\n"
        "- A module may export `ValkeyModule_OnLoad`, `RedisModule_OnLoad`, "
        "`ValkeyModule_OnUnload`, or `RedisModule_OnUnload`. Not finding a "
        "Valkey-prefixed entrypoint is not an error on its own."
    ),
    "scripting": (
        "### Scripting\n"
        "- `src/eval.c` is Lua (EVAL/EVALSHA). `src/functions.c` is the "
        "FUNCTION command family. `src/script.c` holds cross-cutting script "
        "context. `src/scripting_engine.c` is the pluggable-engine layer "
        "(Valkey-specific; not in Redis 7.2).\n"
        "- `lua-replicate-commands` is in `deprecated_configs[]` — do not "
        "flag removal."
    ),
    "security": (
        "### ACL\n"
        "- ACL checks go through `ACLCheckAllUserCommandPerm(user *u, "
        "struct serverCommand *cmd, ...)` — note `struct serverCommand`, "
        "not `struct redisCommand`.\n"
        "- User objects are still `user *`; the ACL wire format / CATEGORY "
        "keywords match Redis 7.2."
    ),
    "data-structures": (
        "### Data structures\n"
        "- `hashtable` (`src/hashtable.c`) is the Valkey-native replacement "
        "for `dict` on hot paths. Primary API: `hashtableCreate`, "
        "`hashtableRelease`, `hashtableCreateIterator`. `kvstore` "
        "(`src/kvstore.c`, `src/kvstore.h`) wraps many small `hashtable` "
        "shards to support keyspace sharding per cluster slot.\n"
        "- `dict` still exists (`src/dict.h` — `typedef struct dictEntry`) "
        "and is used for smaller maps. Do NOT flag `dict` usage as "
        "'should be hashtable' without checking the call site.\n"
        "- `src/vset.c` / `vset.h` is 'Volatile Set' — a Valkey-specific "
        "adaptive, expiry-aware set container built on `hashtable` + `rax`. "
        "It is NOT the Redis Stack vector-set module. Do not expect "
        "`VADD`/`VSIM` commands or vector semantics.\n"
        "- `listpack` replaces `ziplist` for hot paths, but `ziplist.c` still "
        "exists for RDB-load backward compatibility. Encoding transitions "
        "happen in the type-specific files (`src/t_hash.c`, `src/t_list.c`, "
        "`src/t_zset.c`, etc.).\n"
        "- String objects: `shouldEmbedStringObject` uses a 128-byte total "
        "budget that includes an optional embedded key and expire. Changing "
        "this constant without updating the struct-layout asserts breaks "
        "the encoding."
    ),
    "memory": (
        "### Memory\n"
        "- `zmalloc`/`zcalloc`/`zrealloc`/`zfree` are `#define` aliases for "
        "`valkey_malloc` / `valkey_calloc` / `valkey_realloc` / `valkey_free` "
        "(src/zmalloc.h). Plain `zmalloc` aborts on OOM; use the `ztry*` "
        "variants when the caller must handle allocation failure.\n"
        "- `performEvictions()` (src/evict.c) runs the maxmemory policy. "
        "`lazyfreeFreeObject` / `lazyfreeFreeDatabase` (src/lazyfree.c) "
        "enqueue work onto the BIO lazy-free thread via "
        "`bioCreateLazyFreeJob`.\n"
        "- Active defrag (`src/defrag.c`) walks via `defragKeysCtx` and "
        "calls per-type `defragKey` functions. Reviewers should check that "
        "new data-structure code exposes a matching defrag callback."
    ),
    "testing": (
        "### Testing\n"
        "- Integration tests are TCL under `tests/unit/*.tcl`, "
        "`tests/integration/`, `tests/cluster/`, `tests/sentinel/`. "
        "The harness is `tests/test_helper.tcl` driven by `runtest`.\n"
        "- C unit tests live under `tests/unit/` (C++ for hashtable) and "
        "module tests under `tests/modules/`.\n"
        "- When a C change lands without a TCL or unit-test update, the "
        "default stance is to ask for coverage rather than block — most "
        "existing changes add tests in the same PR."
    ),
}


def get_divergence_block() -> str:
    """Return the always-on Valkey-vs-Redis divergence block."""
    return _VALKEY_DIVERGENCE_BLOCK


def infer_subsystems(changed_paths: list[str]) -> set[str]:
    """Infer which subsystem context blocks are relevant for a diff.

    Matches each path against `_PATH_TO_SUBSYSTEM` prefixes. The first
    matching prefix wins per path; paths with no match are ignored.
    """
    subsystems: set[str] = set()
    for path in changed_paths:
        for prefix, subsystem in _PATH_TO_SUBSYSTEM.items():
            if path.startswith(prefix):
                subsystems.add(subsystem)
                break
    return subsystems


def get_subsystem_context(changed_paths: list[str]) -> str:
    """Return concatenated subsystem-specific context blocks for a diff.

    Returns an empty string if no subsystem matched. Blocks are returned
    in sorted order so the prompt is deterministic across runs.
    """
    subsystems = infer_subsystems(changed_paths)
    if not subsystems:
        return ""
    blocks = []
    for sub in sorted(subsystems):
        if sub in _SUBSYSTEM_CONTEXT:
            blocks.append(_SUBSYSTEM_CONTEXT[sub])
    return "\n\n".join(blocks)

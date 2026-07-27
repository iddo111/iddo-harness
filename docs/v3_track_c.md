# Iddo Harness v3 — Track C: Agent-Native Capabilities

**Status:** implemented on `feat/v3-track-c`
**Standard:** Almaware Protocol (AMP) v1.0 (`agent/amp.py`, unchanged)
**Owner:** iddo111
**Supersedes:** nothing. Track C is purely **additive** — every v1 and v2 task
packet keeps working, byte-for-byte.

---

## 0. Why Track C

v1 gave the harness four verbs. v2 ("Agent Fabric") added eleven more and made
long-running work streamable. Both share one assumption: **a task packet is a
single action, and the producer decides what happens next.** Every branch, every
retry, every "and then, depending on the output…" costs a network round trip to
whoever holds the bridge token.

That assumption is what makes the harness a pair of hands rather than an agent.
The gaps it leaves:

| # | v2 gap | Consequence |
|---|--------|-------------|
| 1 | One packet, one action | A ten-step job is ten round trips, each paying full latency |
| 2 | No fan-out | Four independent commands run one after another for no reason |
| 3 | No dependency ordering | The producer has to sequence the steps and hold the intermediate state |
| 4 | Nothing survives a task | Every packet starts from zero; the producer is the only memory |
| 5 | Nothing happens unattended | "Every morning at 6" means a producer that never sleeps |
| 6 | No in-loop reasoning | "Figure out why the build broke" cannot be delegated, only scripted |
| 7 | Capabilities are undiscoverable | A producer learns a kind is missing by having it fail |
| 8 | Common jobs are re-specified every time | "git status report" is fifteen lines of JSON, every time |

Track C closes all eight with **14 new task kinds** across seven capabilities.

---

## 1. Design principles

1. **Additive only.** `agent/executor.py` keeps its v1 path untouched; the
   v3 router is three lines at the top of `Executor.run()`, a faithful sibling
   of the existing v2 hook. A v1 packet never even constructs `ExecutorV3`.
2. **One execution path.** Nothing in Track C executes anything itself. A
   sub-task, a workflow node, a scheduled firing, an LLM tool call and a
   rendered template all become ordinary tasks that re-enter at
   `Executor.run()`. This is why every collaborator takes `execute` as an
   injected callable and why `ExecutorV3` receives the *parent* `Executor`,
   not itself, as `parent_executor`.
3. **Policy is per-child, never per-envelope.** Gating a `workflow` would ask
   the owner to approve a container whose contents they cannot see. Composite
   kinds are therefore not gated; each child meets `PolicyEngine.decide()` on
   its own terms — which is the decision the owner can actually make.
4. **No auto-approval, ever.** A model or a workflow that could walk through a
   confirmation gate would defeat the gate. `llm_task` *stops* on
   `confirm_required` and reports what is waiting.
5. **Graceful degradation.** `croniter` is optional; a built-in 5-field cron
   parser takes over when it is absent, and `validate_cron` gives the same
   answer either way.
6. **Testable without infrastructure.** A task kind whose tests need a running
   inference server is a task kind that is not tested. The `llm_task` providers
   are mocks by default, and `HttpProvider` plugs the real backend in.

---

## 2. The 14 new task kinds

`executor_v3.V3_KINDS`:

| Capability | Kinds |
|---|---|
| Sub-tasks | `spawn_task`, `await_tasks` |
| Workflow DAG | `workflow` |
| Memory store | `memory_set`, `memory_get`, `memory_list`, `memory_delete` |
| Scheduler | `schedule_task`, `schedule_list`, `schedule_cancel` |
| LLM tool loop | `llm_task` |
| Handshake | `handshake` |
| Templates | `run_template`, `template_list` |

Six are read-only and skip the gate (`executor_v3._UNGATED_KINDS`):
`await_tasks`, `handshake`, `memory_get`, `memory_list`, `schedule_list`,
`template_list`.

---

## 3. Sub-tasks — `spawn_task` / `await_tasks`

`agent/subtasks.py`. Fire-and-forget fan-out plus a join.

```jsonc
{
  "id": "t-3001",
  "kind": "spawn_task",
  "payload": {
    "tasks": [
      {"kind": "shell", "command": "npm run lint"},
      {"kind": "shell", "command": "npm run typecheck"}
    ]
  }
}
```

Children get derived ids (`t-3001-sub-0`, `t-3001-sub-1`) and inherit the
parent's AMP envelope, so results correlate back without the producer tracking
anything. The result carries `task_ids`; `await_tasks` then joins:

```jsonc
{"id": "t-3002", "kind": "await_tasks",
 "payload": {"task_ids": ["t-3001-sub-0", "t-3001-sub-1"],
             "timeout_sec": 300, "require_all": true}}
```

Guards that matter: a depth limit (a sub-task that spawns sub-tasks that spawn
sub-tasks is a fork bomb with a JSON syntax), a `max_active` in-flight cap, and
refusal of a duplicate id. `require_all: false` reports what finished rather
than failing the join. Spawning is **fire-and-forget** — a child's failure
surfaces on the await, not on the spawn.

---

## 4. Workflow DAG — `workflow`

`agent/workflow.py`. The centrepiece: a dependency graph the harness resolves
itself.

```jsonc
{
  "id": "t-3010",
  "kind": "workflow",
  "payload": {
    "nodes": [
      {"id": "build", "kind": "shell", "payload": {"command": "make"}},
      {"id": "test", "kind": "shell", "depends_on": ["build"],
       "payload": {"command": "make test"}},
      {"id": "report", "kind": "write_file", "depends_on": ["test"],
       "payload": {"path": "/tmp/out.txt", "content": "${nodes.test.stdout}"}}
    ],
    "max_parallel": 4,
    "fail_fast": true
  }
}
```

- **Ordering** is Kahn's algorithm with declaration-order tie-breaking, so a
  given graph always runs in the same order.
- **Parallelism** is a `ThreadPoolExecutor` capped by `max_parallel`, waking on
  `FIRST_COMPLETED` so a finished node's dependents launch immediately.
- **Output flow** is `${nodes.<id>.<field>}` over `stdout`, `stderr`,
  `exit_code`, `ok`. A whole-string reference keeps its native type
  (`"${nodes.a.exit_code}"` → `0`, not `"0"`); an embedded one is stringified.
- **Validation is up front**: duplicate ids, cycles, unknown dependencies,
  self-dependency, unknown reference fields, and referencing a node you did not
  declare a dependency on are all rejected before anything runs.
- **A skipped node says why** — `"dependency build failed"`, not just
  `"skipped"` — under both `fail_fast` settings.
- `continue_on_error` on a node lets its dependents run anyway.

---

## 5. Memory store — `memory_set` / `get` / `list` / `delete`

`agent/memory.py`. SQLite at `~/.iddo-harness/memory.db`, resolved on call so a
test that redirects `Path.home()` gets a redirected database.

```jsonc
{"id": "t-3020", "kind": "memory_set",
 "payload": {"key": "deploy_target", "value": {"host": "srv1", "port": 8080},
             "namespace": "default", "tags": ["infra"], "ttl_sec": 86400}}
```

Values are JSON-encoded, so a dict round-trips as a dict. `created_at` survives
an overwrite. Expiry is enforced on read as well as by an explicit purge, so a
stale value is never returned even if nothing has swept it yet. `memory_list`
filters by key prefix, tag and limit — with LIKE wildcards escaped, so a key
containing `%` is a key and not a pattern.

A `memory_get` miss is a **successful result reporting absence**
(`found: false`), not a failure. "I looked and it is not there" is an answer.

The database is plain SQLite with no ORM, readable by the `sqlite3` CLI — the
owner can audit what the agent remembers about them without running the agent.

---

## 6. Scheduler — `schedule_task` / `schedule_list` / `schedule_cancel`

`agent/scheduler.py`. Cron, interval or one-shot; exactly one trigger per
schedule.

```jsonc
{"id": "t-3030", "kind": "schedule_task",
 "payload": {"cron": "0 6 * * 1-5",
             "task": {"kind": "shell", "payload": {"command": "make nightly"}}}}
```

`croniter` is used when installed; otherwise a built-in 5-field parser handles
`*`, ranges, lists and steps. Both implement the standard's two quirks: the
day-of-month / day-of-week fields are **OR**ed when both are restricted, and
Sunday is both `0` and `7`.

A missed slot — the machine was asleep — **fires once** on the next tick, not
once per slot missed. Naive timestamps are read as UTC. The registry persists
to disk and tolerates a corrupt file rather than refusing to start.

---

## 7. LLM tool loop — `llm_task`

`agent/llm_task.py`. `agent/llm_loop.py` already runs a model against the
executor, but it is an *alternative entry point* — reachable from the CLI, not
from a task packet. This makes the loop a first-class kind, so "figure this out
on the box" arrives over the same bridge, through the same policy engine, with
the same audit trail as `shell`.

Providers (`MOCK_PROVIDERS`): `echo`, `scripted`, `keyword`. `HttpProvider`
exists but is deliberately **not** addressable from a packet — it needs a live
client, which a packet cannot supply.

```jsonc
{"id": "t-3040", "kind": "llm_task",
 "payload": {"prompt": "find the largest file under /var/log",
             "provider": "keyword", "max_iterations": 10,
             "tools": ["shell", "read_file"]}}
```

Default tools are `shell`, `read_file`, `write_file`, `list_dir`, `grep`,
`glob`; iterations default to 10 and clamp at 50. `tools` **narrows** the
allow-list and never widens it, and the narrowing is enforced at dispatch — a
model that calls a tool it was not offered is refused, or the list would be
advice rather than a limit.

`stop_reason` is one of `final`, `max_iterations`, `confirm_required`,
`blocked`, `provider_error`, `no_tools`. Only `final` means the model finished.
On `confirm_required` the loop halts and reports `pending_confirmation`; a tool
that *raises* is observable rather than fatal, and comes back as a tool result
the model can read.

---

## 8. Handshake — `handshake`

`agent/handshake.py`. One round trip that tells a producer what this harness can
do: identity, `HARNESS_VERSION` (3.0.0), AMP version, supported protocols
(`1.0`, `2.0`, `3.0`), every kind grouped by generation, host details, optional
dependencies present, and limits.

Negotiation names what is missing rather than merely counting it, so one round
trip is enough to know a plan will not run:

```jsonc
{"negotiation": {"negotiated_protocol": "3.0", "compatible": false,
                 "unsupported_kinds": ["teleport"], "missing_capabilities": []}}
```

Protocols sort numerically, not lexically (`10.0` > `9.0`).

**The policy summary reports counts, never patterns.** The rule list is a map of
what the owner is protecting; publishing it would tell whoever holds the bridge
token exactly what to go after. This is asserted by
`test_policy_summary_never_leaks_the_patterns`.

---

## 9. Templates — `run_template` / `template_list`

`agent/templates.py`. Five bundled jobs that render to an ordinary workflow —
there is no second execution path, and `parse_workflow` accepts the output.

| Template | Params | Writes |
|---|---|---|
| `git_status_report` | `repo`, `log_count` | no |
| `python_test_run` | `repo`, `test_path`, `python`, `pytest_args` | no |
| `disk_space_report` | `path`, `top` | no |
| `log_error_scan` | `path`, `pattern`, `glob`, `tail_lines` | no |
| `project_bootstrap` | `path`, `name`, `description` | **yes** |

Only `project_bootstrap` writes, and only under a caller-named path — the
easiest thing to invoke by name should not be the thing that writes.

A missing required parameter is named. So is an **unknown** one: a typo'd
parameter that silently does nothing produces a quiet wrong run, which is worse
than an error. `${nodes.x.y}` passes through untouched, because it belongs to
the workflow layer.

---

## 10. Backward compatibility

- `agent/amp.py` is untouched.
- The v1 code path in `agent/executor.py` is untouched; the v3 hook is a router
  check above it.
- `agent/executor_v2.py` is untouched.
- A v1 or v2 packet routes exactly where it always did, and does not construct
  `ExecutorV3` at all (`test_v1_still_routes_to_v1` asserts `_v3 is None`).
- An unknown kind still returns `unknown_kind`.

## 11. Tests

`tests/test_memory.py`, `test_subtasks.py`, `test_workflow.py`,
`test_scheduler.py`, `test_llm_task.py`, `test_handshake.py`,
`test_templates.py`, `test_executor_v3.py` — 424 tests, on top of the
pre-existing 124. Full suite: 548 passing.

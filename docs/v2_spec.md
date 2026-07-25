# Iddo Harness v2 — Agent Fabric SPEC

**Status:** Draft → implemented on `feat/v2-agent-fabric`
**Standard:** Almaware Protocol (AMP) v1.0 (`agent/amp.py`, unchanged)
**Owner:** iddo111
**Supersedes:** nothing. v2 is purely **additive** — every v1 task packet keeps working.

---

## 0. Why v2

v1 gave the harness four verbs: `shell`, `read_file`, `write_file`, `list_dir`.
That is enough to *touch* a machine but not enough to *work* on one. The gaps
that hurt in practice:

| # | v1 gap | Consequence |
|---|--------|-------------|
| 1 | `subprocess.run` blocks until exit | A 10-minute build reports nothing for 10 minutes, then dumps everything |
| 2 | Every shell task is one-shot | Cannot drive `python -i`, `ssh`, `psql`, or any REPL |
| 3 | `write_file` replaces the whole file | Cannot make a surgical 3-line edit without reading + rewriting megabytes |
| 4 | No recursive search | `list_dir` is one level; no grep, no glob |
| 5 | No process control | Cannot see or stop a runaway dev server |
| 6 | No file watching | Cannot react to a rebuild or a log append |
| 7 | No local HTTP | The agent's `localhost` is not the user's `localhost` |
| 8 | 5 s poll interval | Round-trip latency floor |
| 9 | Single result, stdout truncated to 16 000 bytes | Large outputs silently lost |

v2 ("Agent Fabric") closes all nine with **11 new task kinds**, a **chunked
streaming result protocol**, and a **live session model**.

---

## 1. Design principles

1. **Additive only.** `agent/executor.py` keeps its v1 code path byte-for-byte.
   A thin router at the top of `Executor.run()` delegates the 11 new kinds to
   `agent/executor_v2.py`. Unknown kinds still return `unknown_kind`.
2. **AMP-native.** Every chunk is a full AMP v1.0 `harness_result` envelope when
   the inbound task was AMP-shaped; legacy plain-dict results when it was not.
   `agent/amp.py` is used as-is and is not modified.
3. **Policy-gated.** Every new kind goes through `PolicyEngine.decide()`. Writes
   and destructive verbs (`patch_file`, `process_kill`) are `confirm` by default.
4. **Cross-platform.** No shelling out to `grep`/`find`/`tasklist` for the search
   and process kinds — pure Python (`re`, `pathlib`, `psutil`) so Windows and
   Linux behave identically.
5. **Graceful degradation.** `watchdog` and `psutil` are *optional*. If absent,
   polling and `ps`/`tasklist` fallbacks kick in. The harness never hard-fails on
   a missing optional dependency.

---

## 2. The 11 new capabilities (14 task kinds)

Watching and process control each need more than one verb, so the 11
capabilities below are exposed as 14 entries in `executor_v2.V2_KINDS`.

### 2.1 `shell_stream`

Run a command and stream stdout/stderr back as they are produced.

```jsonc
{
  "id": "t-2001",
  "kind": "shell_stream",
  "payload": {
    "command": "npm run build",
    "cwd": "D:\\CLAUDE\\shiri",
    "timeout_sec": 600,
    "flush_interval_ms": 500,   // max time a partial line sits in the buffer
    "max_chunk_bytes": 16000
  }
}
```

**Semantics.** `subprocess.Popen` with piped stdout/stderr. One reader thread per
pipe pushes lines onto a queue; the emitter drains the queue and writes a chunk
whenever (a) a newline was seen, or (b) `flush_interval_ms` elapsed with a
non-empty buffer, or (c) the buffer exceeds `max_chunk_bytes`. On exit a final
chunk carries `is_final: true` and `exit_code`.

**Chunk body:**

```jsonc
{ "task_id": "t-2001", "seq": 7, "is_final": false,
  "stream": "stdout", "text": "webpack 5.90.0 compiled\n" }
```

Final chunk:

```jsonc
{ "task_id": "t-2001", "seq": 42, "is_final": true,
  "ok": true, "exit_code": 0, "decision": "auto",
  "stats": { "chunks": 42, "stdout_bytes": 91233, "stderr_bytes": 0 } }
```

**Policy:** identical to `shell` — `decide(command, paths)`.

---

### 2.2 `shell_session_open`

Open a long-lived interactive process and keep it alive across tasks.

```jsonc
{ "kind": "shell_session_open",
  "payload": { "command": "python -i", "cwd": "D:\\CLAUDE", "idle_timeout_sec": 900 } }
```

**Result body:** `{ "session_id": "s-4f3c…", "pid": 18244, "command": "python -i" }`

If `command` is omitted, the platform default shell is used
(`cmd.exe` on Windows, `$SHELL` or `/bin/sh` elsewhere).

**Policy:** `decide(command, paths)`, same rules as `shell`.

---

### 2.3 `shell_session_write`

Send stdin to a live session and read whatever comes back within a window.

```jsonc
{ "kind": "shell_session_write",
  "payload": { "session_id": "s-4f3c…", "input": "print(2+2)\n", "read_timeout_sec": 2.0 } }
```

**Result body:** `{ "session_id": …, "stdout": "4\n", "stderr": "", "alive": true }`

`input` gets a trailing `\n` appended if missing. `read_timeout_sec` is a *drain
window*, not a deadline for the command — the caller polls again with an empty
`input` to read more.

**Policy:** the *input line* is policy-checked as if it were a shell command, so
`rm -rf /` typed into a live session is still blocked.

---

### 2.4 `shell_session_close`

```jsonc
{ "kind": "shell_session_close", "payload": { "session_id": "s-4f3c…" } }
```

Terminates (then, after 3 s, kills) the process and frees the slot. Sessions also
self-reap after `idle_timeout_sec` with no writes.

---

### 2.5 `grep`

Recursive regex search, implemented in pure Python.

```jsonc
{ "kind": "grep",
  "payload": {
    "path": "D:\\CLAUDE\\shiri",
    "pattern": "def\\s+handle_\\w+",
    "include": "*.py",           // glob on the file name; default "*"
    "ignore_case": false,
    "max_results": 500,
    "max_file_bytes": 5000000,
    "context": 0                 // lines of leading/trailing context
  } }
```

**Result body:**

```jsonc
{ "matches": [ { "path": "…/router.py", "line_no": 88, "line": "def handle_task(…):",
                 "before": [], "after": [] } ],
  "count": 12, "files_scanned": 341, "truncated": false }
```

Binary files (NUL byte in the first 8 KiB) are skipped. Directories named
`.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `dist`, `build` are
pruned unless `include_hidden: true`.

**Policy:** read-only → `auto` on allowed read roots.

---

### 2.6 `glob`

```jsonc
{ "kind": "glob",
  "payload": { "path": "D:\\CLAUDE", "pattern": "**/*.test.ts",
               "recursive": true, "max_results": 1000, "files_only": true } }
```

**Result body:** `{ "paths": [...], "count": 87, "truncated": false }`
Results are sorted by mtime descending (most recently touched first), matching
the ergonomics of a Glob tool.

---

### 2.7 `patch_file`

Surgical, diff-style editing. This is the verb that replaces "read 2 MB, rewrite
2 MB" with "replace these 3 lines".

```jsonc
{ "kind": "patch_file",
  "payload": {
    "path": "D:\\CLAUDE\\shiri\\config.py",
    "edits": [
      { "old_string": "TIMEOUT = 30", "new_string": "TIMEOUT = 120" },
      { "old_string": "  log(", "new_string": "  logger.info(", "replace_all": true }
    ],
    "backup": true,
    "dry_run": false
  } }
```

A single edit may also be given flat as `old_string` / `new_string` /
`replace_all` at the payload root.

**Rules (all enforced before anything is written — the whole patch is atomic):**

| Condition | Behaviour |
|-----------|-----------|
| `old_string` not found | fail, nothing written |
| `old_string` occurs N>1 times and `replace_all` is false | **fail** with `occurrences: N` |
| `old_string` occurs N>1 times and `replace_all` is true | all N replaced |
| `old_string == new_string` | fail (no-op edit is a caller bug) |
| any edit fails | **no** edit is applied |

`backup: true` (default) copies the original to
`<path>.bak-<YYYYmmddTHHMMSS>` *before* writing. `dry_run: true` computes and
returns the unified diff without touching disk.

**Result body:**

```jsonc
{ "path": "…", "applied": 2, "replacements": 7, "backup_path": "….bak-20260725T014200",
  "diff": "--- a/config.py\n+++ b/config.py\n@@ …", "bytes_before": 4211, "bytes_after": 4219 }
```

**Policy:** write verb → `confirm` on the standard write roots.
`decide("patch <path>", paths=[path])`.

---

### 2.8 `watch_start` / `watch_poll` / `watch_stop`

Filesystem change notification without a persistent socket — the queue model
polls, so watches buffer events server-side and hand them out on demand.

```jsonc
{ "kind": "watch_start",
  "payload": { "path": "D:\\CLAUDE\\shiri", "recursive": true,
               "patterns": ["*.py", "*.ts"], "max_buffer": 1000 } }
// → { "watch_id": "w-91ab…", "backend": "watchdog" | "polling" }

{ "kind": "watch_poll", "payload": { "watch_id": "w-91ab…", "max_events": 200 } }
// → { "events": [ { "ts": "…", "type": "modified", "path": "…/router.py" } ],
//      "count": 3, "dropped": 0, "alive": true }

{ "kind": "watch_stop", "payload": { "watch_id": "w-91ab…" } }
```

Event types: `created`, `modified`, `deleted`, `moved`. Backend is `watchdog`
when the package is importable, otherwise a 1 s mtime-snapshot polling thread
that produces the same event shape. Buffer is a bounded deque; overflow
increments `dropped` rather than blocking the watcher.

---

### 2.9 `process_list` / `process_kill`

```jsonc
{ "kind": "process_list",
  "payload": { "filter": "node", "sort_by": "memory", "limit": 50 } }
// → { "processes": [ { "pid": 8123, "name": "node.exe", "cmdline": "node server.js",
//                      "cpu_percent": 3.2, "memory_mb": 412.7, "status": "running",
//                      "created": "2026-07-25T00:11:02Z", "username": "iddo" } ],
//      "count": 4, "backend": "psutil" }

{ "kind": "process_kill", "payload": { "pid": 8123, "force": false, "timeout_sec": 5 } }
// → { "pid": 8123, "name": "node.exe", "signal": "SIGTERM", "killed": true }
```

`process_kill` sends SIGTERM (`terminate()`) and waits `timeout_sec`; with
`force: true`, or if the process survives the wait, it escalates to SIGKILL.
Killing PID 0/1 or the harness's own PID is refused outright.

**Policy:** `process_list` → `auto`. `process_kill` → **`confirm`** (it can take
down a dev server or a training run).

---

### 2.10 `http_local`

The agent's `localhost` is not the user's `localhost`. This verb makes an HTTP
request *from the user's machine*.

```jsonc
{ "kind": "http_local",
  "payload": {
    "url": "http://127.0.0.1:8080/api/health",
    "method": "GET",
    "headers": { "Accept": "application/json" },
    "body": null,
    "timeout_sec": 30,
    "max_bytes": 1000000
  } }
```

**Result body:** `{ "status": 200, "headers": {...}, "body": "…", "truncated": false,
"elapsed_ms": 14, "url": "…" }`

**Hard security boundary — enforced in the executor, not only in policy.**
The hostname is resolved and every resulting IP must be in one of:

- loopback: `127.0.0.0/8`, `::1`
- private: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
- link-local: `169.254.0.0/16`, `fe80::/10`
- unique-local IPv6: `fc00::/7`

Anything else (public IPs, DNS names that resolve off-LAN) is rejected with
`error: "host not local: <ip>"` *before* a socket is opened. Only `http`/`https`
schemes; only `GET`/`POST`/`PUT`/`PATCH`/`DELETE`/`HEAD`.

**Policy:** loopback → `auto`; LAN → `confirm`.

---

### 2.11 `read_file_chunked`

Byte-accurate pagination over a file, so a 4 GB log is readable through a queue
that carries small JSON documents.

```jsonc
{ "kind": "read_file_chunked",
  "payload": { "path": "D:\\CLAUDE\\logs\\train.log",
               "offset": 0, "limit_bytes": 65536, "encoding": "utf-8" } }
```

**Result body:**

```jsonc
{ "path": "…", "offset": 0, "bytes_read": 65536, "total_size": 4183220992,
  "next_offset": 65536, "eof": false, "content": "…", "encoding": "utf-8" }
```

`offset` is a **byte** offset, not a character offset, so pagination is exact for
multi-byte content; decoding uses `errors="replace"` so a chunk boundary that
splits a UTF-8 sequence never raises. `eof` is true when
`offset + bytes_read >= total_size`, and `next_offset` is then `null`.

---

## 3. Chunked result protocol

v1 wrote exactly one `results/<id>.json`. v2 adds a streaming form.

**File naming:** `results/<task_id>-chunk-<seq>.json`, `seq` starting at `0`,
strictly increasing, no gaps.

**Envelope:** each chunk file is a full AMP v1.0 `harness_result` outbound
envelope (identical construction to v1's `reporter.py`) whose `payload.body` is:

```jsonc
{
  "task_id": "t-2001",
  "seq": 7,
  "is_final": false,
  // …kind-specific fields (stream/text for shell_stream, etc.)
}
```

For legacy (non-AMP) inbound tasks the chunk file holds the bare body dict, same
backward-compatibility rule as v1.

**Consumer contract:**

1. Chunks for a task may be written across several git pushes. Order by `seq`,
   not by file mtime.
2. `is_final: true` marks the end of the stream. Exactly one chunk per task has
   it. The final chunk always carries `ok`, `decision`, and (for shell kinds)
   `exit_code`.
3. A task that never produces a final chunk within its timeout should be treated
   as failed; the harness emits a final chunk with `ok: false, error: "timeout"`
   in the normal case.
4. Consumers must tolerate the *absence* of intermediate chunks — a non-streaming
   kind emits exactly one chunk with `seq: 0, is_final: true`.

**Push batching.** Pushing to git per chunk is correct but slow under burst. The
reporter batches: it accumulates up to `batch_size` (default 3) chunk files, or
`batch_interval_ms` (default 1000) of wall time, then does one
`git add / commit / push`. A chunk with `is_final: true` always forces an
immediate flush.

---

## 4. Session model

```
shell_session_open  ──▶  session_id  ──▶  shell_session_write × N  ──▶  shell_session_close
                              │                                              ▲
                              └──────── idle_timeout_sec elapsed ────────────┘ (auto-reap)
```

- Sessions live in the `ExecutorV2` instance (`dict[str, ShellSession]`), which
  the router caches on the `Executor`, so they survive across polling cycles for
  the lifetime of the agent process.
- Each session owns its `Popen` (stdin/stdout/stderr piped), plus two reader
  threads appending to per-stream buffers. `shell_session_write` drains those
  buffers.
- Reaping happens at the start of every session operation: any session whose
  `last_activity` is older than its `idle_timeout_sec`, or whose process has
  exited, is closed and removed.
- `max_sessions` (default 16) caps concurrency; opening beyond the cap fails
  rather than exhausting the machine's process table.
- Agent shutdown closes all sessions (`ExecutorV2.shutdown()`).

---

## 5. Policy additions

New patterns in `policy.yaml`:

| Verb form passed to `decide()` | Bucket |
|--------------------------------|--------|
| the raw command (for `shell_stream`, `shell_session_*`) | existing shell rules |
| `grep <path>`, `glob <path>`, `read_chunk <path>`, `list <path>` | `auto_allow` |
| `process_list*`, `watch_poll*`, `watch_stop*` | `auto_allow` |
| `http_local GET http://127.0.0.1*`, `…localhost*` | `auto_allow` |
| `patch <path>` | `require_confirm` |
| `process_kill*` | `require_confirm` |
| `watch_start <path>` | `auto` on read roots, otherwise default `confirm` |
| `http_local * http://192.168.*` / `10.*` / `172.1[6-9].*` … | default `confirm` |

`PolicyEngine.decide()` also gains **path-glob evaluation**, which v1 declared in
`policy.yaml` (`auto_allow.paths.read`, `require_confirm.paths.write`) but never
consulted:

1. block commands → 2. block paths → 3. confirm commands →
4. **write-verb + path matches `require_confirm.paths.write` → CONFIRM** →
5. auto commands → 6. **read-verb + all paths match `auto_allow.paths.read` → AUTO** →
7. default CONFIRM.

Steps 4 and 6 are additive: every v1 command/decision pair yields the same
decision as before (a v1 read verb already matched an `auto_allow.commands`
pattern at step 5; a v1 write verb already fell through to CONFIRM at step 7).

---

## 6. Comparison with Desktop Commander MCP

| Capability | Desktop Commander MCP | Iddo Harness v2 |
|---|---|---|
| Transport | Local stdio MCP, client must be on the same machine | **GitHub bridge repo** — works from anywhere, any client, no tunnel, no open port |
| Streaming shell output | ✅ | ✅ `shell_stream` chunked results |
| Interactive sessions | ✅ | ✅ `shell_session_open/write/close` with idle reaping |
| Surgical edit | ✅ `edit_block` | ✅ `patch_file` — multi-edit, **atomic all-or-nothing**, `replace_all`, `dry_run`, automatic timestamped backup, returns unified diff |
| Recursive search | ✅ | ✅ `grep` (pure-Python regex, context lines, binary skip, dir pruning) |
| Glob | ✅ | ✅ `glob`, mtime-sorted |
| Process list / kill | ✅ | ✅ `process_list` / `process_kill` with SIGTERM→SIGKILL escalation and self-kill guard |
| File watching | ❌ | ✅ `watch_start/poll/stop`, watchdog + polling fallback |
| Local HTTP from the user's machine | ❌ | ✅ `http_local` with an enforced loopback/LAN allowlist |
| Chunked / paginated file reads | partial (line offsets) | ✅ `read_file_chunked`, exact **byte** offsets, `total_size` + `next_offset` |
| Approval / confirmation flow | ❌ (trusts the client) | ✅ policy engine: auto / confirm / block, push-notification confirm loop, audit log |
| Audit trail | ❌ | ✅ every decision logged; every result committed to git — an immutable, reviewable history |
| Protocol | MCP | **AMP v1.0** envelopes, addressable multi-brick routing, `reply.to_address` |
| Multiple concurrent clients | one client per server process | many producers writing into one `tasks/` dir |
| Works when the machine is behind NAT/firewall | ❌ | ✅ (outbound git only) |
| Optional-dependency degradation | n/a | ✅ psutil/watchdog optional, fallbacks built in |

**Where Desktop Commander is still ahead:** sub-second latency (stdio vs. a git
poll) and zero setup. v2 narrows the latency gap with adaptive polling but does
not close it — that is the price of working over a queue from anywhere.

---

## 7. Backward compatibility guarantees

1. `agent/executor.py`'s v1 methods are untouched; only a 6-line router was
   prepended to `run()` and `resume_after_confirm()`.
2. `agent/reporter.py` is untouched. `reporter_v2.py` is a separate class.
3. `agent/amp.py` is untouched.
4. A v1 task packet (`shell` / `read_file` / `write_file` / `list_dir`, plain
   dict or AMP envelope) produces a byte-identical result to v1.
5. `policy.yaml` only gains new patterns; no existing pattern was changed or
   removed.
6. New dependencies (`psutil`, `watchdog`) are optional at runtime.

---

## 8. Files

| File | Status |
|---|---|
| `agent/executor_v2.py` | new — all 11 kinds |
| `agent/reporter_v2.py` | new — chunked streaming reporter |
| `agent/executor.py` | +router only |
| `agent/policy.py` | +path-glob evaluation |
| `policy.yaml` | +v2 patterns |
| `tests/test_executor_v2.py` | new |
| `requirements.txt` | +watchdog, +psutil |
| `docs/v2_spec.md` | this file |
| `docs/task_packet_spec.md` | +11 kind examples |
| `README.md` | +v2 highlights |

— iddo111 / Almaware / 2026-07-25

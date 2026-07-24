# AMP Envelope Examples — Iddo Harness

Three worked examples of AMP v1.0 envelopes as consumed/produced by Iddo
Harness's `agent/amp.py` module: an inbound `shell` task, an inbound
`read_file` task, and the resulting outbound `harness_result`. All three
were validated against `agent.amp.parse_envelope` (see
`tests/test_amp.py` for the automated version of this check).

Related reading: [`docs/amp_alignment.md`](amp_alignment.md) (why/how
Iddo Harness maps onto AMP), [`docs/task_packet_spec.md`](task_packet_spec.md)
(the pre-AMP legacy task-packet shape, still accepted for backward
compatibility), and the full protocol spec,
**The Almaware Protocol (AMP) v1.0** (§2 Core envelope, §6.1 Identity
normalization).

---

## Example 1 — Inbound shell task

A model (ChatGPT/Codex, in this example) asks the harness to run a shell
command. `direction: "inbound"` because, relative to the harness (the
receiving brick), this envelope is arriving *from* the outside world.
`identity` describes the external sender — here, Iddo himself, acting
through the `chatgpt-codex` brick — per AMP §2.2's direction/identity
relationship rule.

```json
{
  "v": 1,
  "id": "01J8Z3K9RXTG3V6P6M8AH1QF7C",
  "ts": "2026-07-23T07:45:00.000Z",
  "direction": "inbound",
  "source": { "brick": "chatgpt-codex", "instance": "session-abc123" },
  "channel": "harness",
  "identity": {
    "self": true,
    "canonical": "user:iddo111",
    "display_name": "Iddo"
  },
  "payload": {
    "type": "harness_task",
    "body": {
      "kind": "shell",
      "command": "git status",
      "paths": ["D:\\shiri"],
      "cwd": "D:\\shiri",
      "timeout_sec": 60
    }
  },
  "reply": { "to_channel": "harness", "to_address": "session:abc123" }
}
```

**Commentary:**
- `identity.canonical` uses the `user:` prefix — Iddo Harness's own identity
  registry (`agent/amp.py::VALID_IDENTITY_PREFIXES`) treats `user:iddo111`
  as the owner identity, matching AMP §6.1a's owner-canonical-identity-set
  model.
- `reply.to_address` is `session:abc123` — a `session:` canonical identity
  the harness will echo back in the outbound result so it routes to the
  right conversation/model session. This is a harness-specific identity
  prefix beyond AMP's core registry (`wa`, `tg`, `sms`, `mail`, `serial`,
  `local`); per §6.1a, non-core prefixes are legal to emit as long as the
  emitting brick's descriptor lists them.
- `payload.body.kind` (`"shell"`) is the pre-AMP task-packet discriminator
  (`docs/task_packet_spec.md`), now nested one level deeper inside AMP's
  own `payload.type: "harness_task"` discriminator. `agent/poller.py`
  unwraps `payload.body` into a legacy-shaped `Task` object so
  `agent/executor.py` (unchanged, AMP-agnostic) keeps working exactly as
  before.
- `ts` uses AMP's required millisecond-precision, trailing-`Z` UTC format
  (§2.3) — `"2026-07-23T07:45:00Z"` without milliseconds would be rejected.

---

## Example 2 — Inbound read_file task

Perplexity Computer asks the harness to read a file. Structurally
identical to Example 1 except for `source.brick`, `payload.body.kind`, and
the body's shape (`read_file` needs only a `path`, no `command`/`timeout_sec`).

```json
{
  "v": 1,
  "id": "01J8Z9Q7T3N4M5P6R7S8T9V0W1",
  "ts": "2026-07-23T21:12:00.000Z",
  "direction": "inbound",
  "source": { "brick": "perplexity-computer", "instance": "session-9f3a21" },
  "channel": "harness",
  "identity": {
    "self": true,
    "canonical": "user:iddo111",
    "display_name": "Iddo"
  },
  "payload": {
    "type": "harness_task",
    "body": {
      "kind": "read_file",
      "path": "D:\\CLAUDE\\RAMCHAT\\RAM_SHARED_MEMORY.md"
    }
  },
  "reply": { "to_channel": "harness", "to_address": "session:9f3a21" }
}
```

**Commentary:**
- Any model brick (Perplexity, Claude, GPT, Gemini — per
  `docs/amp_alignment.md`'s multi-model access diagram) can be the
  `source.brick`; the envelope shape is identical regardless of which
  model produced it. This is the "one stud pattern, infinite bricks"
  principle (AMP §1.2) in practice — the harness's `parse_envelope` does
  not special-case any particular `source.brick` value.
- `agent/amp.py::_parse_payload` requires `body.kind` for every
  `harness_task`, but does **not** enforce a closed schema per `kind`
  value (unlike full AMP's closed-union `payload.type` arms, §2.4) — that
  validation still lives in `agent/executor.py`'s per-kind handlers
  (`_read_file`, `_run_shell`, etc.), which is unchanged and out of scope
  for this AMP integration. `# TODO(amp):` marks this gap in `amp.py`.
- Windows-style backslash paths pass through the JSON string unescaped by
  AMP — the envelope layer treats `path` as an opaque string inside
  `payload.body`; path semantics belong to the executor/policy layer.

---

## Example 3 — Outbound result

The harness's response to Example 1's shell task, as written to
`results/01J8Z3K9RXTG3V6P6M8AH1QF7C.json` in the bridge repo by
`agent/reporter.py`. `direction: "outbound"` because this envelope
originates *from* the harness. Per AMP §2.2, `identity` now describes the
**local actor** (the harness itself, not the original human owner), and
`reply.to_address` carries the **external recipient** — copied directly
from the incoming task's own `reply.to_address` so the result round-trips
back to the same session.

```json
{
  "v": 1,
  "id": "8f14e45f-ceea-4e46-9c92-1234567890ab",
  "ts": "2026-07-23T07:45:03.118Z",
  "direction": "outbound",
  "source": { "brick": "iddo-harness", "instance": "iddo111" },
  "channel": "harness",
  "identity": {
    "self": false,
    "canonical": "brick:iddo-harness"
  },
  "payload": {
    "type": "harness_result",
    "body": {
      "task_id": "01J8Z3K9RXTG3V6P6M8AH1QF7C",
      "ok": true,
      "decision": "auto",
      "stdout": "On branch main\nnothing to commit, working tree clean\n",
      "stderr": "",
      "exit_code": 0,
      "error": "",
      "metadata": {}
    }
  },
  "reply": {
    "to_channel": "harness",
    "to_address": "session:abc123",
    "reply_to_id": "01J8Z3K9RXTG3V6P6M8AH1QF7C"
  }
}
```

**Commentary:**
- `identity.self: false` — the harness is not the owner (`user:iddo111`);
  it is a `brick:` actor doing the owner's bidding. This is the correct
  reading of AMP §2.3.1's `self` field on an outbound envelope: "is this
  the owner's own identity," not "is this a self-chat."
- `reply.reply_to_id` is the original task's envelope `id`
  (`01J8Z3K9RXTG3V6P6M8AH1QF7C`), letting any consumer correlate the
  result back to the specific task envelope, per AMP §2.3.2.
- `payload.body` is exactly the harness's pre-existing `Result` dataclass
  (`agent/executor.py`, unchanged) serialized via `dataclasses.asdict` —
  AMP-wrapping happens only at the envelope layer in `agent/reporter.py`;
  the inner result shape is the same one documented in
  `docs/task_packet_spec.md`, now nested at `payload.body` instead of the
  JSON file's top level.
- `agent/reporter.py` only builds this AMP wrapper when the originating
  `Task` carried a validated `AmpEnvelope` (i.e. the inbound task was
  itself AMP-shaped). For legacy, pre-AMP task packets, the result file
  keeps the old flat shape unchanged — see "Backward compatibility" below.

---

## Backward compatibility note

Per `docs/task_packet_spec.md`, older task packets have a top-level `kind`
field and no `v`/`payload` envelope wrapper, e.g.:

```json
{
  "id": "20260723-001-scan-dclaude",
  "kind": "shell",
  "payload": { "command": "dir D:\\CLAUDE /s /b", "timeout_sec": 120 }
}
```

`agent/poller.py` still accepts these: `amp.is_amp_shaped()` returns
`False` (no `v` field), a warning is logged
(`"task packet is not AMP-shaped ... accepting as legacy packet"`), and
the resulting `Task.envelope` is `None`. `agent/reporter.py` checks
`task.envelope` and, when `None`, writes the exact legacy flat result
shape — no AMP wrapping — so older producers keep working unchanged while
new producers get full AMP conformance.

## Sources

- Almaware Protocol v1.0 specification (`/home/user/workspace/memory/sessions/2026-07-20_2026-07-26/0baab2f1/ai_outputs/Almaware-Protocol-v1.0.md`) — §2 (envelope), §2.2 (direction semantics), §2.3 (field definitions), §6.1 (identity normalization).
- Handout ל-ChatGPT v2 — AMP + Iddo Harness (`/home/user/workspace/memory/sessions/2026-07-20_2026-07-26/397bf24b/ai_outputs/Handout-ChatGPT-v2--AMP--Iddo-Harness.md`) — task-packet-as-AMP-envelope example.
- `docs/amp_alignment.md` and `docs/task_packet_spec.md` (this repository).

# AMP Envelope Work — Summary

Task: build a complete AMP (Almaware Protocol v1.0) envelope module and
integrate it into the existing Iddo Harness agent code, per
`docs/amp_alignment.md` and the AMP v1.0 spec.

Status: **done**. 41/41 new tests pass. No regressions in the pre-existing
test suite (`tests/test_confirm.py` unaffected; `tests/test_cli.py`'s 8
failures are pre-existing sandbox-level `OSError(-25)` failures unrelated
to this change — `cli.py` was not touched and is out of scope).

---

## Files changed

| File | Type | LOC added (net) | Notes |
|---|---|---:|---|
| `agent/amp.py` | **New** | 570 | Full AMP v1.0 envelope module: `AmpEnvelope` dataclass + sub-objects (`Source`, `Identity`, `Payload`, `Reply`, `RateLimit`, `Audit`), `parse_envelope`, `build_envelope`, `serialize`, `to_json`, `is_amp_shaped`, `AmpValidationError`. |
| `agent/poller.py` | Updated | ~84 | Added `import amp`; added `Task.envelope: AmpEnvelope \| None` field; replaced inline parsing in `fetch_pending_tasks` with `_task_from_packet` dispatcher, which calls `_task_from_amp` (validates via `amp.parse_envelope`, unwraps `payload.body` into the legacy `Task` shape) or `_task_from_legacy` (pre-AMP fallback, logs a warning) based on `amp.is_amp_shaped`. |
| `agent/reporter.py` | Updated | ~62 | Added `import amp`; added `HARNESS_BRICK_NAME` / `HARNESS_IDENTITY_CANONICAL` constants; added `_build_result_payload` which, when `task.envelope` is set, builds a full `harness_result` outbound envelope via `amp.build_envelope` (replying to `task.envelope.reply.to_address`/`to_channel`, with `reply_to_id` set to the original envelope id) — otherwise returns the legacy flat result dict unchanged. `send`/`send_error` now route through this. `_write`'s log line reads `ok`/`decision` from either shape. |
| `tests/test_amp.py` | **New** | 307 | Pytest suite: valid round-trip (7 tests), missing required fields (7 tests, parametrized over all 9 top-level fields), bad identity format (5 tests, incl. all 5 valid prefixes), wrong direction (3 tests), unknown payload type (2 tests), plus extra coverage for major-version rejection, malformed timestamps, and `is_amp_shaped`. **41 tests total, all passing.** |
| `docs/amp_envelope_examples.md` | **New** | 216 | 3 worked, validated example envelopes (inbound shell task, inbound read_file task, outbound harness_result) with field-by-field commentary, plus a backward-compatibility note and source citations. |
| `AMP_WORK_SUMMARY.md` | **New** | (this file) | This summary. |

**Not touched** (per constraints): `agent/main.py`, `agent/executor.py`, `agent/policy.py`, `agent/config.py`, `policy.yaml`, `installer/`. Also did not touch `agent/cli.py`, `agent/confirm.py`, `agent/llm_client.py`, `agent/llm_router.py` — these existed from concurrent work outside this task's scope and were left as-is; `agent/executor.py`'s public interface (`task.kind`, `task.id`, `task.payload`) is unchanged and consumed identically by the new `Task.envelope`-aware poller/reporter.

---

## What the module supports

- `AmpEnvelope` dataclass matching the AMP v1.0 schema subset Iddo Harness needs: `v`, `id`, `ts`, `direction`, `source{brick,instance}`, `channel`, `identity{self,canonical,display_name,provisional,raw}`, `payload{type,body}`, `reply{to_channel,to_address,reply_to_id}`, optional `rate_limit`, optional `audit`.
- `parse_envelope(dict, id_strict=True) -> AmpEnvelope` — strict, field-scoped validation; raises `AmpValidationError` with clear messages (e.g. `"envelope.identity: field 'canonical' = 'notaprefix' is not a valid identity string..."`).
- `build_envelope(...)` — helper for constructing outbound envelopes programmatically (used by `reporter.py`); always round-trips through `parse_envelope` so it can never emit an invalid envelope.
- `serialize(env) -> dict` and `to_json(env) -> str` (`json.dumps(env.to_dict(), ensure_ascii=False)`), verified lossless round-trip in tests.
- `payload.type` support: `harness_task` (inbound, requires `body.kind`) and `harness_result` (outbound, requires `body.task_id` + `body.ok`).
- Identity canonical prefixes accepted: `user:*`, `wa:*`, `mqtt:*`, `brick:*`, `session:*` (per task spec; a subset of AMP's full §6.1a registry, scoped to what Iddo Harness actually emits/consumes).
- Direction/payload coherence enforced: `inbound` ⇒ `harness_task`, `outbound` ⇒ `harness_result`.
- Major-version rejection (`v != 1`), strict `ts` pattern (ms precision + `Z`), ULID/UUIDv4 `id` pattern (with a documented, narrowly-scoped relaxation for legacy bridge-repo ids arriving inside an AMP wrapper during the migration window).

## Backward compatibility

- `agent/poller.py` still accepts pre-AMP, free-form task packets (top-level `kind`, no `v`/`payload` envelope) exactly as before, via `amp.is_amp_shaped()` detection — a warning is logged and `Task.envelope` stays `None`.
- `agent/reporter.py` writes the exact legacy flat result shape (`docs/task_packet_spec.md`) when `task.envelope is None`; only AMP-shaped inbound tasks get an AMP-wrapped `harness_result` outbound envelope in return.
- `agent/executor.py` required zero changes — it only ever consumed `task.kind` / `task.id` / `task.payload`, which are populated identically regardless of whether the source was AMP or legacy.

## TODOs left (`# TODO(amp):` markers)

All explicitly marked in code, per the task's compliance-signaling requirement:

1. **`agent/amp.py`** — only `harness_task`/`harness_result` payload types are supported; full AMP v1.0's `text`/`command`/`sensor`/`event`/`state` types are not implemented (not needed by Iddo Harness's one channel today).
2. **`agent/amp.py`** — identity prefix whitelist (`user`, `wa`, `mqtt`, `brick`, `session`) is hardcoded rather than loaded from the shared `registry/channels.json` file that full AMP §6.1a describes.
3. **`agent/amp.py`** — `RateLimit` dataclass lets an envelope carry/round-trip `§6.3` rate-limit metadata, but nothing in poller/executor/reporter enforces `ALMAWARE_MIN_INTERVAL_S` / `ALMAWARE_MAX_PER_HOUR` yet — no shared SQLite counter or equivalent.
4. **`agent/amp.py`** — `Audit` dataclass exists for round-tripping audit metadata on the envelope, but the harness's audit trail is still a plaintext log (`agent/policy.py::PolicyEngine.audit` + `agent/main.py`'s logging handler), not the JSONL audit log full AMP conformance expects.
5. **`agent/amp.py`** — unknown top-level envelope fields are currently ignored rather than rejected (spec's `additionalProperties: false`, §2.5) — left lenient while producer implementations (Perplexity/Claude/GPT/Gemini) stabilize.
6. **`agent/reporter.py`** — harness's own AMP identity (`HARNESS_BRICK_NAME`, `HARNESS_IDENTITY_CANONICAL`) is hardcoded rather than sourced from `policy.yaml`/`config.py` (both off-limits for this change).
7. **`agent/poller.py`** — the relaxed `id_strict=False` path for legacy free-form ids arriving inside an AMP wrapper should become strict once every producer mints real ULID/UUIDv4 ids.

None of these block the deliverables in scope; all are deferred, documented gaps rather than silent ones, per AMP §7's "stub ≠ shipped" principle — this module does not claim full AMP v1.0 conformance, only conformance for the `harness_task`/`harness_result` slice Iddo Harness actually uses.

## Test run

```
$ python3 -m pytest tests/test_amp.py -v
...
============================== 41 passed in 0.05s ==============================
```

Full suite (`python3 -m pytest`): 48 passed (including `test_amp.py` + pre-existing `test_confirm.py`), 8 pre-existing failures in `tests/test_cli.py` unrelated to this change (sandbox `OSError(-25)` from Click's `CliRunner`, not a logic error, and in a file this task did not modify).

## Sources consulted

- The Almaware Protocol (AMP) v1.0 — `/home/user/workspace/memory/sessions/2026-07-20_2026-07-26/0baab2f1/ai_outputs/Almaware-Protocol-v1.0.md`
- Handout ל-ChatGPT v2 — AMP + Iddo Harness — `/home/user/workspace/memory/sessions/2026-07-20_2026-07-26/397bf24b/ai_outputs/Handout-ChatGPT-v2--AMP--Iddo-Harness.md`
- `docs/amp_alignment.md`, `docs/task_packet_spec.md` (this repository)

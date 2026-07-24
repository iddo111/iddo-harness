# Eranim LLM Bridge — Work Summary

Adds LLM integration to iddo-harness: the harness can now (a) receive
tasks directly from any OpenAI-compatible LLM endpoint — specifically the
vLLM servers planned for the Eranim cluster (4× NVIDIA DGX Spark, two
pairs) — and (b) feed task results back to that LLM, in a loop, subject to
the same policy engine as the existing GitHub-bridge path.

## Files added

| File | Purpose |
|---|---|
| `agent/llm_client.py` | `OpenAICompatibleClient` — httpx wrapper around any OpenAI-compatible `/v1/chat/completions` + `/v1/models`. `chat_completion()`, `stream_completion()` (SSE), `health_check()`. 3 retries w/ exponential backoff on connection errors only (not on 4xx/5xx); 120s default timeout. |
| `agent/llm_router.py` | `LlmRouter` — parses `llm_backends:` from policy.yaml into per-role `primary`/`secondary` `OpenAICompatibleClient`s. Background thread health-checks every backend via `GET /v1/models` on `health_check_interval` (default 60s). `route(role)` returns the healthy primary, falls back to secondary, raises `NoHealthyBackendError` if neither is healthy. Also exposes `LlmRouter.from_config(cfg)` which reads `llm_backends:` straight out of policy.yaml even though `agent/config.py`'s `Config` dataclass doesn't parse that section (config.py was off-limits for this change). |
| `agent/llm_tools.py` | OpenAI tool schemas for `shell`, `read_file`, `write_file`, `list_dir` (`ALL_TOOLS`). `tool_call_to_task()` converts an LLM tool_call into an inbound `harness_task` `AmpEnvelope`. `envelope_to_executor_task()` adapts that envelope into the minimal shape `Executor.run()` expects. `result_to_tool_message()` converts an `executor.Result` (or plain dict) into a `role: "tool"` OpenAI message. `result_to_harness_result_envelope()` wraps a Result as an outbound AMP envelope for audit logging. |
| `agent/llm_loop.py` | `LlmDrivenLoop` — alternative entry point to `main.py`. Runs role + prompt through: LLM produces `tool_calls` → each is converted to an AMP envelope → run through the real `Executor` (same `PolicyEngine`) → result fed back as a tool message → repeat until the LLM stops requesting tools or `max_iterations` (default 25) is hit. Every iteration is logged as an AMP-shaped audit record (`_audit_iteration`). A `Decision.CONFIRM` from the policy engine **pauses** the loop (`LoopResult.paused=True`) rather than auto-approving; caller is responsible for resuming after out-of-band approval via the existing `iddo-harness confirm` flow. |
| `patches/cli_llm_commands.py` | Patch (not merged, since `agent/cli.py` is owned by another agent) adding `iddo-harness ask <role> "<prompt>"` and `iddo-harness models`. Written to match `agent/cli.py`'s actual `click`-based structure (verified against the real file, which had already been built out with `run`/`submit`/`status`/`confirm`/`tail`/`policy` commands by the time this was written) — includes exact import block and command bodies to paste in, with merge instructions in the module docstring. |
| `policy.yaml` (updated) | Added `llm_backends:` section at the end exactly as specified: `coder` role (primary `eran1.local:8000`, secondary `eran3.local:8000`, both `glm-4.6`) and `reasoner` role (primary `eran3.local:8000`, `deepseek-v3.1`). |
| `requirements.txt` (updated) | Added `httpx>=0.27`. |
| `requirements-test.txt` (new) | `respx>=0.20`, `pytest-asyncio>=0.23`. |
| `pyproject.toml` (updated) | Added `httpx>=0.27` to `dependencies`; added `respx`, `pytest-asyncio` to the `test` optional-dependencies group. |
| `tests/test_llm_router.py` | `respx`-mocked tests: role/backend parsing, primary-healthy routing, primary-down→secondary failover (both HTTP-500 and connection-error cases), no-healthy-backend raises, unconfigured role raises `KeyError`, optimistic pre-health-check default, `status()` reporting, health-check thread start/stop lifecycle. 11 tests. |
| `tests/test_llm_tools.py` | Tool schema shape checks; `tool_call_to_task()` (including dict-vs-JSON-string arguments, unknown tool, missing id); `envelope_to_executor_task()` strips bookkeeping fields; full round trip through the **real** `Executor`/`PolicyEngine` for auto-allowed, confirm-required, and blocked shell commands, plus write+read file; `result_to_tool_message()` with dict and dataclass input; `result_to_harness_result_envelope()`. 14 tests. |
| `docs/llm_integration.md` | How to point the harness at vLLM (Eranim), Ollama, or OpenAI; `policy.yaml` config reference; CLI and programmatic usage; example `curl` showing the exact request/response shape `/v1/chat/completions` must support (including tool-calling); retry/timeout/streaming behavior. |

## Key integration notes

- **`agent/amp.py` already existed** by the time this work started (built
  by a concurrent agent) — fully AMP v1.0-envelope-shaped with
  `AmpEnvelope`, `parse_envelope`, `build_envelope`. All LLM-loop task and
  result flow uses it directly (no mock was needed); `llm_tools.py` and
  `llm_loop.py` import it the same way `poller.py`/`reporter.py` do.
- **`agent/cli.py`, `agent/confirm.py`, and `tests/` also already existed**
  by the time CLI/test work started (other agents were working in
  parallel). The CLI patch and both new test files were written/adjusted
  to match the real conventions found in those files (click groups, flat
  imports with `agent.`-prefixed ImportError fallback, `ConfirmManager`
  now being invoked from inside `Executor` for CONFIRM decisions) rather
  than the originally-assumed argparse shape.
- `Executor` (as it now exists) writes pending-confirmation files via
  `ConfirmManager` for any CONFIRM decision. Both new test files inject an
  isolated `ConfirmManager` pointed at `tmp_path` so tests never write to
  the real `~/.iddo-harness/pending/`.
- Constraint compliance: `agent/main.py`, `agent/executor.py`,
  `agent/policy.py`, `agent/config.py`, and `installer/` were **not**
  modified by this work (verified via file mtimes predating this session).
  `agent/cli.py` was likewise left untouched — the CLI addition is a
  standalone patch file per the task's instructions.

## Test results

```
81 passed in 2.51s
```

This includes the pre-existing `test_amp.py` (25), `test_cli.py` (8),
`test_confirm.py` (7) suites plus the new `test_llm_router.py` (11) and
`test_llm_tools.py` (14) — all green, confirming the new modules don't
regress anything already in place.

An additional manual smoke test confirmed the full `LlmDrivenLoop` cycle
end-to-end with a fake router/client: a `shell` tool call → AMP envelope →
`Executor.run()` → `Result` → tool message → second LLM turn returning a
plain-text final answer, with `done=True` and 3 AMP-shaped audit records
generated.

## Not done / left for follow-up

- `patches/cli_llm_commands.py` is **not merged** into `agent/cli.py` —
  per task instructions, that file is owned by another agent. Merge
  instructions are in the patch file's docstring.
- No real Eranim/vLLM endpoint was reachable from this environment, so all
  network-facing behavior (retries, failover, health checks) is verified
  via `respx`-mocked httpx traffic, not a live vLLM server.
- `api_key` support exists in `OpenAICompatibleClient`/`BackendSpec` but is
  intentionally not read from `policy.yaml` by default in the docs'
  guidance, to avoid encouraging secrets to be committed to the bridge
  repo; it's available as a constructor/config field for anyone who wants
  it.

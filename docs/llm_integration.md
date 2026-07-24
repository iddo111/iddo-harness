# LLM Integration — pointing Iddo Harness at vLLM / Ollama / OpenAI

The harness can now be driven directly by any LLM that speaks the OpenAI
`/v1/chat/completions` dialect — this is the same surface implemented by
**vLLM**, **Ollama** (via its OpenAI-compatible shim), **LM Studio**,
**TGI**, and OpenAI itself. This document covers how to configure and use
that integration against the Eranim cluster (4× NVIDIA DGX Spark, two
pairs) or any other OpenAI-compatible backend.

## Architecture

```
                     policy.yaml: llm_backends:
                              │
                              ▼
                        LlmRouter  ── health-checks GET /v1/models every N s
                         │      │
                    primary   secondary (fallback)
                         │
                         ▼
              OpenAICompatibleClient  (agent/llm_client.py)
                         │
                         ▼
                POST /v1/chat/completions  (tools=[shell, read_file, write_file, list_dir])
                         │
                         ▼
                    tool_calls  ──▶  llm_tools.tool_call_to_task()  ──▶  AmpEnvelope (harness_task)
                                                                              │
                                                                              ▼
                                                                     executor.Executor.run()
                                                                       (same policy engine as
                                                                        the GitHub-bridge path)
                                                                              │
                                                                              ▼
                                                             llm_tools.result_to_tool_message()
                                                                              │
                                                                              ▼
                                                          fed back into the next chat_completion()
```

`LlmDrivenLoop` (`agent/llm_loop.py`) owns this loop end-to-end and is a
second entry point alongside `main.py` — it does not replace the
GitHub-bridge poll loop, and both can run at the same time.

## Configuring backends (`policy.yaml`)

Add an `llm_backends:` section. Each key is a **role** (`coder`,
`reasoner`, `planner`, or any name you like); each role has a `primary`
backend and an optional `secondary` fallback:

```yaml
llm_backends:
  coder:
    primary:
      base_url: http://eran1.local:8000/v1
      model: glm-4.6
    secondary:
      base_url: http://eran3.local:8000/v1
      model: glm-4.6
    health_check_interval: 60   # seconds; default 60
  reasoner:
    primary:
      base_url: http://eran3.local:8000/v1
      model: deepseek-v3.1
```

Mapping to the Eranim cluster's two DGX Spark pairs:

| Pair | Hosts | Suggested role | Suggested model |
|---|---|---|---|
| A | eran1 + eran2 | `coder` | GLM-4.6 or Qwen3-Coder-480B |
| B | eran3 + eran4 | `reasoner` | a reasoning model / fine-tune |

`LlmRouter` health-checks every configured backend (`GET /v1/models`) on a
background thread every `health_check_interval` seconds. `route(role)`
returns the primary if it's healthy; otherwise it automatically falls back
to `secondary` (if configured); if neither is healthy it raises
`NoHealthyBackendError`.

## Using it

### CLI

Once `patches/cli_llm_commands.py` is merged into `agent/cli.py` (see that
file for the exact merge steps):

```bash
# One-shot: ask the "coder" role to do something using the harness's tools
iddo-harness ask coder "list the files in ~/projects and summarize what's there"

# Check backend health
iddo-harness models
```

`ask` runs `LlmDrivenLoop`: the model gets `shell`, `read_file`,
`write_file`, and `list_dir` tools, and loops (default cap 25 iterations)
until it stops requesting tools. If the harness policy engine returns a
CONFIRM decision for any tool call (see `policy.yaml`'s `require_confirm`
section), the loop **pauses** — it does not auto-approve — and prints
instructions for approving via the existing `iddo-harness confirm` flow.

### Programmatic

```python
from llm_router import LlmRouter
from llm_loop import LlmDrivenLoop
from executor import Executor
from policy import PolicyEngine
from config import load_config

cfg = load_config()
router = LlmRouter.from_config(cfg, auto_start=True)
policy = PolicyEngine(cfg)
executor = Executor(policy)

loop = LlmDrivenLoop(router=router, policy=policy, executor=executor, role="coder")
result = loop.run("check disk usage and report anything over 80% full")
print(result.final_message)
router.stop()
```

## Pointing at vLLM

Start vLLM with an OpenAI-compatible server on each Eranim node, e.g.:

```bash
python -m vllm.entrypoints.openai.api_server \
  --model zai-org/GLM-4.6 \
  --port 8000 \
  --host 0.0.0.0
```

vLLM implements `/v1/chat/completions`, `/v1/completions`, and
`/v1/models` out of the box — no extra flags needed for basic tool-calling
support on models that support it (GLM-4.6, Qwen3-Coder, DeepSeek-V3.1 all
support the OpenAI tool/function-calling schema). Then point
`policy.yaml`'s `llm_backends.<role>.primary.base_url` at
`http://<host>:8000/v1`.

## Pointing at Ollama

Ollama exposes an OpenAI-compatible shim at `/v1`:

```bash
ollama serve
# in another terminal:
ollama pull qwen2.5-coder:32b
```

Then set:

```yaml
llm_backends:
  coder:
    primary:
      base_url: http://localhost:11434/v1
      model: qwen2.5-coder:32b
```

Note: Ollama's `/v1/models` returns locally-pulled models; if the exact
`model` name doesn't match what's pulled, `/v1/chat/completions` will 404 —
run `ollama list` to confirm the tag.

## Pointing at OpenAI (or any hosted OpenAI-compatible API)

```yaml
llm_backends:
  planner:
    primary:
      base_url: https://api.openai.com/v1
      model: gpt-4o-mini
```

Set an API key via `OpenAICompatibleClient(..., api_key="sk-...")` — the
router doesn't currently read the key from `policy.yaml` for security
reasons (avoid committing secrets to the bridge repo); wire it up via an
environment variable at construction time in your own driver code, or add
`api_key:` to the backend dict in policy.yaml if that machine's copy of
policy.yaml is not itself synced anywhere sensitive.

## What `/v1/chat/completions` must accept

The harness's `OpenAICompatibleClient` sends a standard OpenAI chat
payload with optional `tools`. Minimum required behavior from the backend:

```bash
curl -s http://eran1.local:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-4.6",
    "messages": [
      {"role": "system", "content": "You are the Iddo Harness agent."},
      {"role": "user", "content": "List the files in /tmp"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "list_dir",
          "description": "List the immediate contents of a directory on the harness host.",
          "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"]
          }
        }
      }
    ],
    "temperature": 0.2,
    "max_tokens": 4096,
    "stream": false
  }'
```

Expected response shape (OpenAI schema) — either a direct answer:

```json
{
  "choices": [
    {"message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}
  ]
}
```

...or a tool call:

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_abc123",
            "type": "function",
            "function": {"name": "list_dir", "arguments": "{\"path\": \"/tmp\"}"}
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ]
}
```

The harness's `llm_tools.tool_call_to_task()` also tolerates
`function.arguments` being a pre-parsed dict instead of a JSON string
(some local server builds do this), so either shape works.

For health checks, the backend must respond to `GET /v1/models` with a
2xx and (ideally, though not required) an OpenAI-shaped
`{"data": [{"id": "..."}]}` body — vLLM, Ollama, and OpenAI all do this.

## Streaming

`OpenAICompatibleClient.stream_completion()` yields plain-text content
deltas by parsing the SSE `data: {...}` framing (`stream: true`), ending on
`data: [DONE]` — the same framing vLLM, Ollama, and OpenAI all emit. This
is not currently wired into `LlmDrivenLoop` (which uses the
non-streaming `chat_completion()` for simplicity, since it needs the full
`tool_calls` array before proceeding); it's available for any driver code
that wants to show live token output.

## Retries and timeouts

`OpenAICompatibleClient` retries connection-level failures (refused
connections, connect/read timeouts) up to 3 times with exponential backoff
(0.5s, 1s, 2s by default) before raising `LlmClientError`. HTTP error
responses (4xx/5xx) are **not** retried — they're surfaced immediately,
since retrying a "model not found" or "bad request" rarely helps and can
mask real configuration problems. Default request timeout is 120 seconds,
configurable via the `timeout` constructor argument.

## Files

- `agent/llm_client.py` — `OpenAICompatibleClient`: thin httpx wrapper,
  `chat_completion()`, `stream_completion()`, retries, health checks.
- `agent/llm_router.py` — `LlmRouter`: reads `llm_backends:`, background
  health-check thread, `route(role)` with primary/secondary failover.
- `agent/llm_tools.py` — OpenAI tool schemas for `shell`/`read_file`/
  `write_file`/`list_dir`; `tool_call_to_task()`, `result_to_tool_message()`.
- `agent/llm_loop.py` — `LlmDrivenLoop`: the agent loop itself.
- `patches/cli_llm_commands.py` — `iddo-harness ask` / `iddo-harness models`
  CLI commands, as a patch for `agent/cli.py`.

See also [docs/amp_alignment.md](amp_alignment.md) for how every tool call
in the LLM loop is wrapped in the same AMP envelope shape used by the
GitHub-bridge task path.

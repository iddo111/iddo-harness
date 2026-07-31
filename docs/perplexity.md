# Local Perplexity Harness Worker

The local worker gives a Perplexity API model practical computer capability
without exposing an unaudited remote-control path. The model proposes function
calls; the worker converts each call into an AMP harness task and passes it to
the injected `Executor.run()`. The existing policy, confirmations, sandbox,
vault resolution, and audit wiring therefore remain authoritative.

## Important identity and memory boundary

This worker does **not** log in to the Perplexity website and does not inherit
Perplexity Computer threads, Projects, or Brain. Perplexity does not expose an
API export of that private account state. `PerplexityMemory` is instead an
explicit local JSONL ledger for project instructions, artifact references, and
task/result summaries. Every record requires provenance. Imported records are
merged by immutable ID, and prompt context is bounded and marked as untrusted
reference data rather than authorization. Normal turns persist prompt hashes,
lengths, task IDs, and decisions—not raw prompts, model responses, stdout, or
stderr. Content is retained only when a trusted caller deliberately adds it.

## Credentials

Set `PERPLEXITY_API_KEY` in the service environment (or arrange an existing
vault/bootstrap process to populate that environment). Never put the key in
YAML, command-line arguments, tasks, commits, logs, or memory records. HTTP
errors intentionally omit response bodies because providers can reflect input.

## Embedding

Create `PerplexityApiClient`, `PerplexityMemory`, and `PerplexityWorker` from a
trusted local bootstrap, injecting the same `Executor` used by the Git,
WebSocket, and MCP transports. Call `worker.handshake(force=True)` before work
and `worker.run(prompt)` for an agent turn. No standalone CLI is installed in
this phase so that deployment and service credentials remain an explicit
operator decision.

The handshake reports `agent_id=perplexity`, the selected model, capabilities,
API reachability, and consecutive failures. Active consumers should probe every
60 seconds. Three consecutive failures produce `harness_unreachable`; callers
must back off instead of busy-polling. A successful probe resets the counter.

Every generated task carries:

```json
{
  "agent_id": "perplexity",
  "transport": "local-perplexity-api"
}
```

inside the policy-visible task payload, while the AMP identity is
`brick:perplexity-computer`. `confirm_required` pauses the turn; it is never
auto-approved. The worker has no direct shell, file, process, or network action
implementation of its own.

## Acceptance boundary

Unit tests with an HTTP mock prove request authentication, bounded retries,
tool-to-AMP-to-Executor routing, confirmation pauses, local-memory provenance,
and health recovery. These tests do not prove a live Perplexity subscription,
model tool-call compatibility, or a visible Perplexity GUI. Live acceptance
requires a fresh nonce through the configured API model and a real benign task
whose policy/audit/result chain is inspected end to end.

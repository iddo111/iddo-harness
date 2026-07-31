# MCP adapter

The MCP adapter gives local hosts such as Claude Desktop access to the same
Iddo Harness executor used by the Git and WebSocket bridges. It is a transport
adapter, not an execution shortcut: every call becomes an AMP `harness_task`,
passes through `Executor.run()`, meets the normal policy/approval flow, and is
recorded in the hash-chained audit log with `transport: "mcp"` and `client_id`.

## Claude Desktop (stdio)

Preview the configuration change first:

```powershell
iddo-harness mcp install --host claude-desktop --dry-run
```

Install it:

```powershell
iddo-harness mcp install --host claude-desktop
```

The installer merges an `iddo-harness` entry into
`%APPDATA%\Claude\claude_desktop_config.json` without removing other servers.
Restart Claude Desktop, call `handshake` with a stable `client_id`, then use the
allowed tools. A `confirm_required` result is intentional; approve it with the
normal harness approval flow rather than bypassing policy.

## SSE on localhost

SSE is opt-in. Set `mcp.enabled: true`, `mcp.transport: sse`, and a loopback
`mcp.sse_bind` in `config.yaml`, then put the token in the configured environment
variable (default `HARNESS_MCP_TOKEN`). The token is never read from YAML.
The authenticated policy identity is a static local setting (`mcp.agent_id`,
default `claude`) and can be overridden only by `HARNESS_MCP_AGENT_ID`; a remote
caller's declared `client_id` is audit/correlation metadata and cannot change
its permissions.

```powershell
$env:HARNESS_MCP_TOKEN = '<random-secret>'
python -m agent.mcp_server --transport sse
```

The adapter validates the bind address before startup and independently rejects
non-loopback peers. Both `/sse` and `/messages/` require the exact bearer token.
Do not publish this endpoint through a public reverse proxy.

## Phase 2 tool surface

Only these tools can be allowlisted: `shell.run`, `shell.stream`, `fs.read`,
`fs.write`, `fs.list`, `fs.grep`, `fs.glob`, `process.list`, `process.kill`, and
`handshake`. Agent-native scheduling, workflows, LLM tasks, spawning, and memory
tools are deliberately not exposed in Phase 2.

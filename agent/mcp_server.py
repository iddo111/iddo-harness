"""MCP transport adapter for Iddo Harness.

There is intentionally no execution implementation in this module. Every MCP
tool becomes an AMP ``harness_task``, is parsed by the shared transport parser,
and enters the same :meth:`Executor.run` router used by git and WebSocket.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import secrets
import sys
import uuid
from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

try:
    from amp import build_envelope
    from agent_identity import bind_authenticated_identity
    from approval import ApprovalManager
    from audit import AuditLog
    from config import DEFAULT_MCP_TOOLS, McpConfig, load_config, load_runtime_config
    from executor import Executor
    from policy import PolicyEngine
    from poller import task_from_packet
    from secrets_vault import SecretVault
except ImportError:  # pragma: no cover - installed package imports
    from agent.amp import build_envelope
    from agent.agent_identity import bind_authenticated_identity
    from agent.approval import ApprovalManager
    from agent.audit import AuditLog
    from agent.config import DEFAULT_MCP_TOOLS, McpConfig, load_config, load_runtime_config
    from agent.executor import Executor
    from agent.policy import PolicyEngine
    from agent.poller import task_from_packet
    from agent.secrets_vault import SecretVault


TOOL_TO_KIND = {
    "shell.run": "shell",
    "shell.stream": "shell_stream",
    "fs.read": "read_file",
    "fs.write": "write_file",
    "fs.list": "list_dir",
    "fs.grep": "grep",
    "fs.glob": "glob",
    "process.list": "process_list",
    "process.kill": "process_kill",
    "handshake": "handshake",
}


def is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


class LocalBearerMiddleware:
    """ASGI guard that independently enforces loopback peer and bearer auth."""

    def __init__(self, app: Any, token: str) -> None:
        self.app, self.token = app, token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        peer = (scope.get("client") or ("", 0))[0]
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1").strip()
        scheme, separator, presented = authorization.partition(" ")
        authenticated = (
            separator == " " and scheme.lower() == "bearer"
            and secrets.compare_digest(presented.strip(), self.token)
        )
        if not is_loopback(str(peer)) or not authenticated:
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b"unauthorized\n"})
            return
        await self.app(scope, receive, send)


class McpAdapter:
    def __init__(self, executor: Executor, audit_log: AuditLog, config: McpConfig) -> None:
        self.executor = executor
        self.audit_log = audit_log
        self.config = config
        self._session_clients: dict[int, str] = {}

    def _client_id(self, ctx: Context | None, declared: str | None = None) -> str:
        session_key = id(ctx.session) if ctx is not None else 0
        if declared:
            self._session_clients[session_key] = declared
            return declared
        if session_key in self._session_clients:
            return self._session_clients[session_key]
        if ctx is not None and ctx.client_id:
            return str(ctx.client_id)
        return "unknown-mcp-client"

    def call(self, tool: str, arguments: dict[str, Any], ctx: Context | None = None) -> dict[str, Any]:
        if tool not in self.config.allowed_tools:
            raise PermissionError(f"MCP tool is not allowed: {tool}")
        kind = TOOL_TO_KIND[tool]
        declared = arguments.pop("client_id", None) if tool == "handshake" else None
        client_id = self._client_id(ctx, declared)
        body = {"kind": kind, **arguments}
        envelope = build_envelope(
            direction="inbound", source_brick="mcp", source_instance=client_id,
            channel="harness", identity_canonical=f"brick:{client_id}",
            identity_display_name=client_id, payload_type="harness_task",
            payload_body=body, to_channel="harness", to_address="brick:iddo-harness",
            env_id=str(uuid.uuid4()),
        )
        task = task_from_packet(envelope.to_dict(), None)
        # The MCP-provided client_id is correlation metadata, not authority.
        # Policy sees only this local, operator-configured transport identity.
        bind_authenticated_identity(
            task, self.config.agent_id, transport="mcp", client_id=client_id,
        )
        self.audit_log.record(
            actor=self.config.agent_id, action="mcp_tool_call", resource=tool,
            meta={"transport": "mcp", "client_id": client_id,
                  "agent_id": self.config.agent_id, "task_id": task.id},
        )
        try:
            result = self.executor.run(task)
        except Exception as exc:
            self.audit_log.record(
                actor=self.config.agent_id, action="mcp_tool_result", resource=tool, outcome="error",
                meta={"transport": "mcp", "client_id": client_id,
                      "agent_id": self.config.agent_id, "task_id": task.id, "error": str(exc)},
            )
            raise
        self.audit_log.record(
            actor=self.config.agent_id, action="mcp_tool_result", resource=tool,
            outcome="ok" if result.ok else ("deny" if result.decision in {"block", "denied"} else "error"),
            meta={"transport": "mcp", "client_id": client_id, "task_id": task.id,
                  "agent_id": self.config.agent_id, "decision": result.decision},
        )
        return asdict(result)


def create_server(adapter: McpAdapter) -> FastMCP:
    mcp = FastMCP("Iddo Harness", instructions="All tools are policy-gated and audited.")

    def expose(name: str):
        return name in adapter.config.allowed_tools

    if expose("shell.run"):
        @mcp.tool(name="shell.run")
        def shell_run(command: str, timeout_sec: int = 300, paths: list[str] | None = None,
                      ctx: Context | None = None) -> dict[str, Any]:
            return adapter.call("shell.run", {"command": command, "timeout_sec": timeout_sec,
                                                "paths": paths or []}, ctx)
    if expose("shell.stream"):
        @mcp.tool(name="shell.stream")
        def shell_stream(command: str, timeout_sec: int = 300,
                         ctx: Context | None = None) -> dict[str, Any]:
            return adapter.call("shell.stream", {"command": command, "timeout_sec": timeout_sec}, ctx)
    if expose("fs.read"):
        @mcp.tool(name="fs.read")
        def fs_read(path: str, ctx: Context | None = None) -> dict[str, Any]:
            return adapter.call("fs.read", {"path": path}, ctx)
    if expose("fs.write"):
        @mcp.tool(name="fs.write")
        def fs_write(path: str, content: str, ctx: Context | None = None) -> dict[str, Any]:
            return adapter.call("fs.write", {"path": path, "content": content}, ctx)
    if expose("fs.list"):
        @mcp.tool(name="fs.list")
        def fs_list(path: str, ctx: Context | None = None) -> dict[str, Any]:
            return adapter.call("fs.list", {"path": path}, ctx)
    if expose("fs.grep"):
        @mcp.tool(name="fs.grep")
        def fs_grep(path: str, pattern: str, include: str = "*", max_results: int = 200,
                    ctx: Context | None = None) -> dict[str, Any]:
            return adapter.call("fs.grep", {"path": path, "pattern": pattern,
                                              "include": include, "max_results": max_results}, ctx)
    if expose("fs.glob"):
        @mcp.tool(name="fs.glob")
        def fs_glob(path: str, pattern: str, max_results: int = 200,
                    ctx: Context | None = None) -> dict[str, Any]:
            return adapter.call("fs.glob", {"path": path, "pattern": pattern,
                                              "max_results": max_results}, ctx)
    if expose("process.list"):
        @mcp.tool(name="process.list")
        def process_list(filter: str = "", limit: int = 200,
                         ctx: Context | None = None) -> dict[str, Any]:
            return adapter.call("process.list", {"filter": filter, "limit": limit}, ctx)
    if expose("process.kill"):
        @mcp.tool(name="process.kill")
        def process_kill(pid: int, timeout_sec: int = 5,
                         ctx: Context | None = None) -> dict[str, Any]:
            return adapter.call("process.kill", {"pid": pid, "timeout_sec": timeout_sec}, ctx)
    if expose("handshake"):
        @mcp.tool(name="handshake")
        def handshake(client_id: str, required_kinds: list[str] | None = None,
                      ctx: Context | None = None) -> dict[str, Any]:
            result = adapter.call("handshake", {"client_id": client_id,
                                                  "required_kinds": required_kinds or []}, ctx)
            result.setdefault("metadata", {})["transport"] = "mcp"
            result["metadata"]["client_id"] = client_id
            result["metadata"]["agent_id"] = adapter.config.agent_id
            return result
    return mcp


def build_adapter(policy_path: str | None = None, runtime_path: str | None = None) -> tuple[McpAdapter, McpConfig]:
    policy_cfg = load_config(policy_path)
    runtime = load_runtime_config(runtime_path)
    audit = AuditLog.from_config(policy_cfg)
    vault = SecretVault.from_config(policy_cfg, audit_sink=audit)
    policy = PolicyEngine(policy_cfg, audit_log=audit)
    approvals = ApprovalManager(policy_cfg, audit_log=audit)
    executor = Executor(policy, approvals, vault=vault, audit_log=audit)
    return McpAdapter(executor, audit, runtime.mcp), runtime.mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Iddo Harness MCP adapter")
    parser.add_argument("--config", default=None, help="path to policy.yaml")
    parser.add_argument("--runtime-config", default=None, help="path to config.yaml")
    parser.add_argument("--transport", choices=("stdio", "sse"), default=None)
    args = parser.parse_args(argv)
    adapter, cfg = build_adapter(args.config, args.runtime_config)
    transport = args.transport or cfg.transport
    server = create_server(adapter)
    if transport == "stdio":
        server.run(transport="stdio")
        return
    if not is_loopback(cfg.sse_bind):
        raise SystemExit("MCP SSE bind must be a loopback address")
    token = os.environ.get(cfg.bearer_token_env, "")
    if not token:
        raise SystemExit(f"MCP SSE requires bearer token in {cfg.bearer_token_env}")
    import uvicorn
    app = LocalBearerMiddleware(server.sse_app(), token)
    uvicorn.run(app, host=cfg.sse_bind, port=cfg.sse_port, log_level="info")


if __name__ == "__main__":
    main(sys.argv[1:])

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audit import AuditLog
from config import DEFAULT_MCP_TOOLS, McpConfig
from executor import Result
from mcp_server import LocalBearerMiddleware, McpAdapter, create_server, is_loopback
from agent_identity import authenticated_agent_id


class RecordingExecutor:
    def __init__(self, result: Result | None = None):
        self.tasks = []
        self.result = result

    def run(self, task):
        self.tasks.append(task)
        return self.result or Result(task_id=task.id, ok=True, decision="auto", stdout="ok")


def adapter(tmp_path: Path, *, tools=None, result=None):
    audit = AuditLog(tmp_path / "audit.jsonl", tmp_path / "audit.key", rotate=False)
    executor = RecordingExecutor(result)
    cfg = McpConfig(allowed_tools=list(tools or DEFAULT_MCP_TOOLS))
    return McpAdapter(executor, audit, cfg), executor, audit


@pytest.mark.parametrize(
    ("tool", "arguments", "kind"),
    [
        ("shell.run", {"command": "echo hi"}, "shell"),
        ("shell.stream", {"command": "echo hi"}, "shell_stream"),
        ("fs.read", {"path": "x"}, "read_file"),
        ("fs.write", {"path": "x", "content": "y"}, "write_file"),
        ("fs.list", {"path": "."}, "list_dir"),
        ("fs.grep", {"path": ".", "pattern": "x"}, "grep"),
        ("fs.glob", {"path": ".", "pattern": "*.py"}, "glob"),
        ("process.list", {}, "process_list"),
        ("process.kill", {"pid": 123}, "process_kill"),
    ],
)
def test_every_mcp_tool_becomes_amp_and_enters_executor(tmp_path, tool, arguments, kind):
    subject, executor, _ = adapter(tmp_path)
    result = subject.call(tool, dict(arguments))
    assert result["ok"] is True
    task = executor.tasks[-1]
    assert task.kind == kind
    assert task.envelope.payload.type == "harness_task"
    assert task.envelope.source.brick == "mcp"


def test_allowlist_is_enforced_before_execution(tmp_path):
    subject, executor, _ = adapter(tmp_path, tools=["fs.read"])
    with pytest.raises(PermissionError, match="not allowed"):
        subject.call("shell.run", {"command": "echo nope"})
    assert not executor.tasks


def test_only_phase_two_tools_are_registered(tmp_path):
    subject, _, _ = adapter(tmp_path)
    server = create_server(subject)
    assert set(server._tool_manager._tools) == set(DEFAULT_MCP_TOOLS)
    assert not ({"spawn_task", "workflow", "schedule_task", "llm_task", "memory_get"}
                & set(server._tool_manager._tools))


def test_config_allowlist_controls_advertised_tools(tmp_path):
    subject, _, _ = adapter(tmp_path, tools=["fs.read", "handshake"])
    assert set(create_server(subject)._tool_manager._tools) == {"fs.read", "handshake"}


def test_handshake_records_client_for_later_calls(tmp_path):
    subject, executor, audit = adapter(tmp_path)
    subject.call("handshake", {"client_id": "claude-desktop"})
    subject.call("fs.read", {"path": "x"})
    assert executor.tasks[-1].envelope.source.instance == "claude-desktop"
    assert authenticated_agent_id(executor.tasks[-1]) == "claude"
    rows = audit.tail(10)
    assert rows
    assert all(row["meta"]["transport"] == "mcp" for row in rows)
    assert all(row["meta"]["client_id"] == "claude-desktop" for row in rows)
    assert audit.verify_chain()[0] is True


def test_declared_client_id_cannot_change_authenticated_agent(tmp_path):
    subject, executor, _ = adapter(tmp_path)
    subject.call("handshake", {"client_id": "perplexity"})
    task = executor.tasks[-1]
    assert task.client_id == "perplexity"
    assert authenticated_agent_id(task) == "claude"


def test_operator_can_select_static_authenticated_agent(tmp_path):
    subject, executor, _ = adapter(tmp_path)
    subject.config.agent_id = "perplexity"
    subject.call("handshake", {"client_id": "claude"})
    assert authenticated_agent_id(executor.tasks[-1]) == "perplexity"
    assert executor.tasks[-1].client_id == "claude"


def test_confirm_required_is_returned_unchanged(tmp_path):
    pending = Result(task_id="ignored", ok=False, decision="confirm_required", error="approval needed")
    subject, _, _ = adapter(tmp_path, result=pending)
    result = subject.call("process.kill", {"pid": 123})
    assert result["decision"] == "confirm_required"
    assert result["error"] == "approval needed"


@pytest.mark.parametrize("value", ["127.0.0.1", "::1", "localhost"])
def test_loopback_recognition(value):
    assert is_loopback(value)


@pytest.mark.asyncio
async def test_sse_guard_rejects_remote_peer_before_app():
    called = False
    messages = []

    async def app(scope, receive, send):
        nonlocal called
        called = True

    async def send(message):
        messages.append(message)

    guard = LocalBearerMiddleware(app, "secret")
    await guard(
        {"type": "http", "client": ("192.168.1.20", 5),
         "headers": [(b"authorization", b"Bearer secret")]},
        None, send,
    )
    assert called is False
    assert messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_sse_guard_rejects_wrong_bearer():
    called = False
    messages = []

    async def app(scope, receive, send):
        nonlocal called
        called = True

    async def send(message):
        messages.append(message)

    guard = LocalBearerMiddleware(app, "secret")
    await guard(
        {"type": "http", "client": ("127.0.0.1", 5),
         "headers": [(b"authorization", b"Bearer wrong")]},
        None, send,
    )
    assert called is False
    assert messages[0]["status"] == 401


@pytest.mark.parametrize("authorization", [
    b"", b"Basic secret", b"Bearer", b"Token secret", b"Bearer wrong",
])
@pytest.mark.asyncio
async def test_sse_guard_rejects_malformed_authorization(authorization):
    called = False
    messages = []

    async def app(scope, receive, send):
        nonlocal called
        called = True

    async def send(message):
        messages.append(message)

    guard = LocalBearerMiddleware(app, "secret")
    await guard(
        {"type": "http", "client": ("127.0.0.1", 5),
         "headers": [(b"authorization", authorization)]},
        None, send,
    )
    assert called is False
    assert messages[0]["status"] == 401


@pytest.mark.parametrize("peer", [
    "192.168.1.20", "10.0.0.8", "172.16.1.4", "100.64.0.5", "203.0.113.9",
])
@pytest.mark.asyncio
async def test_sse_guard_rejects_every_non_loopback_peer(peer):
    called = False
    messages = []

    async def app(scope, receive, send):
        nonlocal called
        called = True

    async def send(message):
        messages.append(message)

    await LocalBearerMiddleware(app, "secret")(
        {"type": "http", "client": (peer, 5),
         "headers": [(b"authorization", b"Bearer secret")]},
        None, send,
    )
    assert called is False
    assert messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_sse_guard_allows_local_authenticated_peer():
    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True

    guard = LocalBearerMiddleware(app, "secret")
    await guard(
        {"type": "http", "client": ("127.0.0.1", 5),
         "headers": [(b"authorization", b"Bearer secret")]},
        None, None,
    )
    assert called is True


@pytest.mark.asyncio
async def test_sse_guard_passes_lifespan_to_wrapped_app():
    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True

    await LocalBearerMiddleware(app, "secret")({"type": "lifespan"}, None, None)
    assert called is True

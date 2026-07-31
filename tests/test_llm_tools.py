"""
Tests for agent/llm_tools.py — the tool_call <-> AmpEnvelope <-> Result <->
tool_message round trip.

Flow under test (mirrors what llm_loop.py actually does each iteration):

    OpenAI tool_call
        --tool_call_to_task-->            AmpEnvelope (harness_task)
        --envelope_to_executor_task-->    ExecutorTaskView (id, kind, payload)
        --executor.run(...)-->            Result
        --result_to_tool_message-->       {"role": "tool", ...} message

Also covers the tool schema catalog shape and the harness_result envelope
helper used for per-iteration audit logging.
"""
import json
import os

import pytest

from confirm import ConfirmManager
from executor import Executor, Result
from policy import PolicyEngine
from llm_tools import (
    ALL_TOOLS,
    UnknownToolError,
    envelope_to_executor_task,
    result_to_harness_result_envelope,
    result_to_tool_message,
    tool_call_to_task,
)


# ---------------------------------------------------------------------------
# Tool schema catalog
# ---------------------------------------------------------------------------

def test_all_tools_cover_the_four_primitives():
    names = {t["function"]["name"] for t in ALL_TOOLS}
    assert names == {"shell", "read_file", "write_file", "list_dir"}


def test_each_tool_schema_is_openai_shaped():
    for tool in ALL_TOOLS:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert "name" in fn and "description" in fn and "parameters" in fn
        params = fn["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert "required" in params
        for req in params["required"]:
            assert req in params["properties"]


# ---------------------------------------------------------------------------
# tool_call_to_task
# ---------------------------------------------------------------------------

def _make_tool_call(name: str, arguments: dict, call_id: str = "call_123") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_tool_call_to_task_shell_produces_valid_envelope():
    tc = _make_tool_call("shell", {"command": "ls -la", "paths": ["/tmp"], "timeout_sec": 30})
    env = tool_call_to_task(tc)

    assert env.direction == "inbound"
    assert env.payload.type == "harness_task"
    assert env.body["kind"] == "shell"
    assert env.body["command"] == "ls -la"
    assert env.body["paths"] == ["/tmp"]
    assert env.body["tool_call_id"] == "call_123"
    assert env.body["tool_name"] == "shell"


def test_tool_call_to_task_accepts_dict_arguments_not_just_json_string():
    # Some local vLLM/Ollama builds return already-parsed dict arguments
    # instead of a JSON string; tool_call_to_task should tolerate both.
    tc = {
        "id": "call_456",
        "type": "function",
        "function": {"name": "read_file", "arguments": {"path": "/etc/hostname"}},
    }
    env = tool_call_to_task(tc)
    assert env.body["path"] == "/etc/hostname"


def test_tool_call_to_task_unknown_tool_raises():
    tc = _make_tool_call("delete_everything", {})
    with pytest.raises(UnknownToolError):
        tool_call_to_task(tc)


def test_tool_call_to_task_generates_id_when_missing():
    tc = {
        "type": "function",
        "function": {"name": "list_dir", "arguments": "{\"path\": \"/tmp\"}"},
    }
    env = tool_call_to_task(tc)
    assert env.body["tool_call_id"].startswith("call_")


# ---------------------------------------------------------------------------
# envelope_to_executor_task
# ---------------------------------------------------------------------------

def test_envelope_to_executor_task_strips_bookkeeping_fields():
    tc = _make_tool_call("shell", {"command": "pwd"})
    env = tool_call_to_task(tc)
    task = envelope_to_executor_task(env)

    assert task.id == env.id
    assert task.kind == "shell"
    assert task.payload == {"command": "pwd"}
    assert "tool_call_id" not in task.payload
    assert "tool_name" not in task.payload


# ---------------------------------------------------------------------------
# Full round trip through the real Executor
# ---------------------------------------------------------------------------

class _FakeCfg:
    """Minimal stand-in for agent.config.Config, just enough for PolicyEngine."""
    auto_allow = {"commands": ["ls*", "dir*", "pwd", "cat*"]}
    require_confirm = {"commands": ["pip install*"]}
    block = {"commands": ["rm -rf /"]}


@pytest.fixture
def executor(tmp_path):
    # Use an isolated ConfirmManager (writing under tmp_path, not the real
    # ~/.iddo-harness) so these tests never pollute (or are polluted by)
    # the real confirmation queue that agent/confirm.py otherwise defaults to.
    policy = PolicyEngine(_FakeCfg())
    confirm_manager = ConfirmManager(cfg=None, local_dir=tmp_path / "pending")
    return Executor(policy, confirm_manager=confirm_manager)


def test_round_trip_shell_auto_allowed(executor):
    command = "dir" if os.name == "nt" else "ls -la"
    tc = _make_tool_call("shell", {"command": command}, call_id="call_auto")
    env = tool_call_to_task(tc)
    task = envelope_to_executor_task(env)

    result = executor.run(task)
    assert isinstance(result, Result)
    assert result.decision == "auto"
    assert result.exit_code == 0

    tool_msg = result_to_tool_message(result, tool_call_id=tc["id"])
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_auto"
    content = json.loads(tool_msg["content"])
    assert content["ok"] is True
    assert content["decision"] == "auto"


def test_round_trip_shell_confirm_required(executor):
    tc = _make_tool_call("shell", {"command": "pip install requests"}, call_id="call_confirm")
    env = tool_call_to_task(tc)
    task = envelope_to_executor_task(env)

    result = executor.run(task)
    assert result.ok is False
    assert result.decision == "confirm_required"

    tool_msg = result_to_tool_message(result, tool_call_id=tc["id"])
    content = json.loads(tool_msg["content"])
    assert content["ok"] is False
    assert content["decision"] == "confirm_required"
    assert "confirmation" in content["error"]


def test_round_trip_shell_blocked(executor):
    tc = _make_tool_call("shell", {"command": "rm -rf /"}, call_id="call_block")
    env = tool_call_to_task(tc)
    task = envelope_to_executor_task(env)

    result = executor.run(task)
    assert result.ok is False
    assert result.decision == "block"

    tool_msg = result_to_tool_message(result, tool_call_id=tc["id"])
    content = json.loads(tool_msg["content"])
    assert content["decision"] == "block"


def test_round_trip_write_and_read_file(executor, tmp_path):
    target = tmp_path / "hello.txt"

    write_tc = _make_tool_call(
        "write_file", {"path": str(target), "content": "hello world"}, call_id="call_write"
    )
    write_env = tool_call_to_task(write_tc)
    write_task = envelope_to_executor_task(write_env)
    write_result = executor.run(write_task)

    # write_file falls under require_confirm by default (no matching
    # auto_allow pattern for "write <path>") — _FakeCfg has no path rules,
    # so PolicyEngine's default-deny-to-confirm kicks in.
    assert write_result.decision in ("confirm_required", "auto")

    if write_result.decision == "confirm_required":
        write_msg = result_to_tool_message(write_result, tool_call_id=write_tc["id"])
        content = json.loads(write_msg["content"])
        assert content["ok"] is False
        return  # nothing was actually written; read-back would fail, which is correct.

    read_tc = _make_tool_call("read_file", {"path": str(target)}, call_id="call_read")
    read_env = tool_call_to_task(read_tc)
    read_task = envelope_to_executor_task(read_env)
    read_result = executor.run(read_task)

    read_msg = result_to_tool_message(read_result, tool_call_id=read_tc["id"])
    content = json.loads(read_msg["content"])
    assert content["ok"] is True
    assert content["stdout"] == "hello world"


# ---------------------------------------------------------------------------
# result_to_tool_message — dict input (not just Result dataclass)
# ---------------------------------------------------------------------------

def test_result_to_tool_message_accepts_plain_dict():
    result_dict = {"ok": False, "decision": "error", "error": "boom", "task_id": "t1"}
    msg = result_to_tool_message(result_dict, tool_call_id="call_x")
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "call_x"
    content = json.loads(msg["content"])
    assert content["error"] == "boom"


def test_result_to_tool_message_falls_back_to_task_id_when_no_call_id_given():
    result_dict = {"ok": True, "decision": "auto", "task_id": "task-999"}
    msg = result_to_tool_message(result_dict)
    assert msg["tool_call_id"] == "task-999"


# ---------------------------------------------------------------------------
# result_to_harness_result_envelope — used for audit logging in llm_loop.py
# ---------------------------------------------------------------------------

def test_result_to_harness_result_envelope_round_trips_ok_and_task_id():
    result = Result(task_id="abc-123", ok=True, decision="auto", stdout="hi", exit_code=0)
    env = result_to_harness_result_envelope(result)

    assert env.direction == "outbound"
    assert env.payload.type == "harness_result"
    assert env.body["task_id"] == "abc-123"
    assert env.body["ok"] is True
    assert env.body["stdout"] == "hi"

    # Must be JSON-serializable (it's an AMP envelope headed for the audit log).
    json.dumps(env.to_dict())

"""
llm_tools.py — Bridges OpenAI-style `tool_calls` to the harness's own task
primitives (shell / read_file / write_file / list_dir), and executor
Results back into OpenAI-style `role: "tool"` messages.

This is the glue between llm_loop.py (which talks to the LLM) and
executor.py (which actually does the work), expressed entirely in terms of
AMP envelopes so every task — whether it originated from the GitHub bridge
(poller.py) or from a local LLM tool-call (llm_loop.py) — flows through the
same AmpEnvelope shape end-to-end.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

try:
    from amp import (
        AmpEnvelope,
        build_envelope,
    )
except ImportError:  # pragma: no cover
    from agent.amp import (
        AmpEnvelope,
        build_envelope,
    )

# ---------------------------------------------------------------------------
# Tool schemas (OpenAI `tools=[...]` format)
# ---------------------------------------------------------------------------
# These mirror the four task `kind`s the executor understands
# (docs/task_packet_spec.md). Keep the parameter names identical to the
# executor's `task.payload` keys so tool_call_to_task() below is a
# near-direct passthrough.

SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": (
            "Run a shell command on the harness host. Subject to the harness "
            "policy engine (policy.yaml) — some commands auto-run, some "
            "require user confirmation, and some are always blocked."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filesystem paths this command touches, for policy path-matching.",
                },
                "cwd": {"type": "string", "description": "Working directory to run the command in."},
                "timeout_sec": {"type": "integer", "description": "Timeout in seconds.", "default": 300},
            },
            "required": ["command"],
        },
    },
}

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a text file from the harness host filesystem.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or ~-relative path to read."},
            },
            "required": ["path"],
        },
    },
}

WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write (create or overwrite) a text file on the harness host. "
            "Usually requires user confirmation per policy.yaml."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or ~-relative path to write."},
                "content": {"type": "string", "description": "Full file contents to write."},
            },
            "required": ["path", "content"],
        },
    },
}

LIST_DIR_TOOL = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List the immediate contents of a directory on the harness host.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to list."},
            },
            "required": ["path"],
        },
    },
}

ALL_TOOLS: list[dict] = [SHELL_TOOL, READ_FILE_TOOL, WRITE_FILE_TOOL, LIST_DIR_TOOL]

# tool name -> harness task "kind"
_TOOL_NAME_TO_KIND = {
    "shell": "shell",
    "read_file": "read_file",
    "write_file": "write_file",
    "list_dir": "list_dir",
}


class UnknownToolError(ValueError):
    pass


# ---------------------------------------------------------------------------
# LLM tool_call -> AmpEnvelope (harness_task)
# ---------------------------------------------------------------------------

def _parse_tool_call_args(tool_call: dict) -> dict:
    """OpenAI puts function args as a JSON *string*; be lenient and accept
    an already-parsed dict too (some local vLLM/Ollama builds do this)."""
    fn = tool_call.get("function", {})
    args = fn.get("arguments", {})
    if isinstance(args, str):
        if not args.strip():
            return {}
        return json.loads(args)
    if isinstance(args, dict):
        return args
    raise ValueError(f"Unsupported tool_call.function.arguments type: {type(args)}")


def tool_call_to_task(
    tool_call: dict,
    *,
    source_instance: str = "llm-loop",
    identity_canonical: str = "session:llm-loop",
    reply_to_address: str = "session:llm-loop",
    reply_to_channel: str = "harness",
) -> AmpEnvelope:
    """
    Converts a single OpenAI-style tool_call dict, e.g.:

        {
          "id": "call_abc123",
          "type": "function",
          "function": {"name": "shell", "arguments": "{\\"command\\": \\"ls\\"}"}
        }

    into an inbound AMP `harness_task` envelope, ready to be handed to
    executor.py (via poller.Task — see llm_loop.py for that adapter).

    The tool_call's `id` is preserved in the envelope body as
    `tool_call_id` so result_to_tool_message() can round-trip it back into
    the `tool_call_id` field OpenAI's message format requires.
    """
    fn = tool_call.get("function", {})
    name = fn.get("name")
    if name not in _TOOL_NAME_TO_KIND:
        raise UnknownToolError(f"Unknown tool name: {name!r}; known tools: {sorted(_TOOL_NAME_TO_KIND)}")

    args = _parse_tool_call_args(tool_call)
    kind = _TOOL_NAME_TO_KIND[name]

    # Normalize args into task.payload shape expected by executor.py
    payload_body = {
        "kind": kind,
        "tool_call_id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
        "tool_name": name,
        **args,
    }

    return build_envelope(
        direction="inbound",
        source_brick="iddo-harness-llm-loop",
        source_instance=source_instance,
        channel="harness",
        identity_canonical=identity_canonical,
        payload_type="harness_task",
        payload_body=payload_body,
        to_channel=reply_to_channel,
        to_address=reply_to_address,
    )


# ---------------------------------------------------------------------------
# Envelope -> poller.Task-like payload (small helper for llm_loop.py)
# ---------------------------------------------------------------------------

@dataclass
class ExecutorTaskView:
    """
    Minimal duck-typed stand-in for poller.Task, exposing exactly the
    attributes executor.py's Executor.run() reads (`id`, `kind`, `payload`).
    Kept here (rather than importing poller.Task) so llm_tools.py has no
    dependency on poller.py, per the task's "don't touch other agents'
    files" boundary — this is intentionally a parallel, compatible shape.
    """
    id: str
    kind: str
    payload: dict


def envelope_to_executor_task(env: AmpEnvelope) -> ExecutorTaskView:
    """Adapts a harness_task AmpEnvelope into something Executor.run() accepts."""
    body = dict(env.body)
    kind = body.pop("kind")
    # tool_call_id/tool_name are bookkeeping, not part of the executor payload
    body.pop("tool_call_id", None)
    body.pop("tool_name", None)
    return ExecutorTaskView(id=env.id, kind=kind, payload=body)


# ---------------------------------------------------------------------------
# executor.Result -> role=tool message
# ---------------------------------------------------------------------------

def result_to_tool_message(result: Any, tool_call_id: str | None = None) -> dict:
    """
    Converts an executor.Result (or any object/dict with the same shape:
    ok, decision, stdout, stderr, exit_code, error, metadata) into an
    OpenAI-style `{"role": "tool", ...}` message suitable for appending to
    the running `messages` list before the next chat_completion() call.

    `tool_call_id` should be the id of the tool_call this result answers;
    if not supplied explicitly, this function tries `result.metadata['tool_call_id']`
    then falls back to `result.task_id`.
    """
    if isinstance(result, dict):
        ok = result.get("ok")
        decision = result.get("decision")
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = result.get("exit_code")
        error = result.get("error", "")
        metadata = result.get("metadata", {}) or {}
        task_id = result.get("task_id")
    else:
        ok = result.ok
        decision = result.decision
        stdout = getattr(result, "stdout", "")
        stderr = getattr(result, "stderr", "")
        exit_code = getattr(result, "exit_code", None)
        error = getattr(result, "error", "")
        metadata = getattr(result, "metadata", {}) or {}
        task_id = getattr(result, "task_id", None)

    resolved_tool_call_id = tool_call_id or metadata.get("tool_call_id") or task_id or "unknown_call"

    content_obj = {
        "ok": ok,
        "decision": decision,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "error": error,
        "metadata": metadata,
    }

    return {
        "role": "tool",
        "tool_call_id": resolved_tool_call_id,
        "content": json.dumps(content_obj, ensure_ascii=False, default=str),
    }


def result_to_harness_result_envelope(
    result: Any,
    *,
    source_instance: str = "llm-loop",
    identity_canonical: str = "session:llm-loop",
    reply_to_address: str = "session:llm-loop",
    reply_to_channel: str = "harness",
) -> AmpEnvelope:
    """
    Wraps an executor.Result into an outbound `harness_result` AMP envelope
    — used by llm_loop.py's per-iteration audit log so every tool
    invocation, whether it came from the GitHub bridge or the LLM loop, is
    recorded in the same envelope shape.
    """
    if isinstance(result, dict):
        ok = bool(result.get("ok"))
        task_id = result.get("task_id", str(uuid.uuid4()))
        body = dict(result)
    else:
        ok = bool(result.ok)
        task_id = result.task_id
        body = {
            "task_id": result.task_id,
            "ok": result.ok,
            "decision": result.decision,
            "stdout": getattr(result, "stdout", ""),
            "stderr": getattr(result, "stderr", ""),
            "exit_code": getattr(result, "exit_code", None),
            "error": getattr(result, "error", ""),
            "metadata": getattr(result, "metadata", {}) or {},
        }
    body["task_id"] = task_id
    body["ok"] = ok

    return build_envelope(
        direction="outbound",
        source_brick="iddo-harness-llm-loop",
        source_instance=source_instance,
        channel="harness",
        identity_canonical=identity_canonical,
        payload_type="harness_result",
        payload_body=body,
        to_channel=reply_to_channel,
        to_address=reply_to_address,
    )

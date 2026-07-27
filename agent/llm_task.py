"""
LLM tool loop — ``kind: llm_task``.

``agent/llm_loop.py`` already runs a model against the executor, but it is an
*alternative entry point*: you reach it from the CLI, it talks to a configured
HTTP backend, and it is not something a producer can ask for in a task packet.
This module makes the loop a first-class kind, so "figure this out on the box"
arrives over the same bridge, through the same policy engine, with the same
audit trail as ``shell``.

The providers here are **mocks by default**, and that is deliberate rather than
a placeholder: a task kind whose tests need a running inference server is a
task kind that is not tested. A scripted provider makes the *loop* — iteration
limits, tool dispatch, policy interruptions, transcript shape — testable
without a GPU, and :class:`HttpProvider` plugs the real thing in when a
backend exists.

The loop never auto-approves. A tool call that policy answers with
``confirm_required`` stops the run and reports what is waiting, because a model
deciding to proceed through a confirmation gate would defeat the gate.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

try:
    from subtasks import build_subtask
except ImportError:  # pragma: no cover - packaged import
    from agent.subtasks import build_subtask  # type: ignore[no-redef]

log = logging.getLogger("harness.llm_task")

DEFAULT_MAX_ITERATIONS = 10
MAX_ALLOWED_ITERATIONS = 50
DEFAULT_TOOL_KINDS = ("shell", "read_file", "write_file", "list_dir", "grep", "glob")

DEFAULT_SYSTEM_PROMPT = (
    "You are the Iddo Harness agent running on the user's own machine. Use the "
    "available tools to accomplish the request. Tool results may come back with "
    "decision=confirm_required, which means the policy engine paused the action "
    "for the owner to approve — do not assume it ran. When finished, reply with "
    "a final plain-text message and no further tool calls."
)

#: Why the loop stopped. ``final`` is the only one that means "the model is done".
STOP_REASONS = frozenset(
    {"final", "max_iterations", "confirm_required", "blocked", "provider_error", "no_tools"}
)


class LlmTaskError(ValueError):
    """Raised for an unusable ``llm_task`` payload."""


@dataclass
class ToolCall:
    """One tool invocation requested by a model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, index: int = 0) -> "ToolCall":
        """Accept both the OpenAI nested shape and a flat ``{name, arguments}``."""
        function = data.get("function") if isinstance(data.get("function"), dict) else {}
        name = str(function.get("name") or data.get("name") or "").strip()
        if not name:
            raise LlmTaskError("tool call has no name")
        raw_args = function.get("arguments", data.get("arguments", {}))
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args or "{}")
            except json.JSONDecodeError as exc:
                raise LlmTaskError(f"tool call {name} has unparseable arguments: {exc}") from exc
        if not isinstance(raw_args, dict):
            raise LlmTaskError(f"tool call {name} arguments must be an object")
        return cls(id=str(data.get("id") or f"call-{index}"), name=name, arguments=raw_args)


@dataclass
class ProviderResponse:
    """One model turn: either a final message, or tool calls, or both."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Provider:
    """The contract a model backend must satisfy for the loop."""

    name = "provider"

    def complete(self, messages: Sequence[dict[str, Any]], tools: Sequence[str]) -> ProviderResponse:
        """Produce the next turn given the transcript so far."""
        raise NotImplementedError


class ScriptedProvider(Provider):
    """Replays a fixed list of turns — the workhorse for testing the loop.

    Running past the end of the script returns a final message rather than
    raising: a loop that is *supposed* to stop should stop, and a script that
    ran out is the clearest possible way to express that.
    """

    name = "scripted"

    def __init__(self, turns: Iterable[Any], *, exhausted_message: str = "done") -> None:
        self.turns = [self._coerce(t) for t in turns]
        self.exhausted_message = exhausted_message
        self.calls: list[list[dict[str, Any]]] = []

    @staticmethod
    def _coerce(turn: Any) -> ProviderResponse:
        if isinstance(turn, ProviderResponse):
            return turn
        if isinstance(turn, str):
            return ProviderResponse(content=turn)
        if isinstance(turn, dict):
            raw_calls = turn.get("tool_calls") or []
            return ProviderResponse(
                content=str(turn.get("content", "")),
                tool_calls=[ToolCall.from_dict(c, index=i) for i, c in enumerate(raw_calls)],
            )
        raise LlmTaskError(f"cannot interpret scripted turn: {turn!r}")

    def complete(self, messages: Sequence[dict[str, Any]], tools: Sequence[str]) -> ProviderResponse:
        self.calls.append([dict(m) for m in messages])
        index = len(self.calls) - 1
        if index < len(self.turns):
            return self.turns[index]
        return ProviderResponse(content=self.exhausted_message)


class EchoProvider(Provider):
    """Answers immediately with the prompt it was given, calling no tools.

    The trivial case worth having a name for: it proves a caller wired the
    kind up correctly without proving anything about a model.
    """

    name = "echo"

    def __init__(self, prefix: str = "echo: ") -> None:
        self.prefix = prefix

    def complete(self, messages: Sequence[dict[str, Any]], tools: Sequence[str]) -> ProviderResponse:
        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
        )
        return ProviderResponse(content=f"{self.prefix}{last_user}")


class KeywordToolProvider(Provider):
    """A deterministic stand-in that calls a tool when the prompt suggests one.

    Not an attempt at intelligence — it is a fixture with a decision in it, so
    a test can exercise "model asks for a shell command, sees the output, then
    summarises" end to end without a model.
    """

    name = "keyword"

    #: Ordered because the first match wins; ``read`` before ``list`` so
    #: "read the listing" reads rather than lists.
    RULES: tuple[tuple[str, str, str], ...] = (
        (r"\bread\b|\bcat\b", "read_file", "path"),
        (r"\blist\b|\bls\b|\bdir\b", "list_dir", "path"),
        (r"\brun\b|\bshell\b|\bexecute\b", "shell", "command"),
    )

    def __init__(self, *, target: str = ".") -> None:
        self.target = target
        self.turn = 0

    def complete(self, messages: Sequence[dict[str, Any]], tools: Sequence[str]) -> ProviderResponse:
        self.turn += 1
        if any(m.get("role") == "tool" for m in messages):
            observed = [m.get("content", "") for m in messages if m.get("role") == "tool"]
            return ProviderResponse(content=f"observed {len(observed)} tool result(s)")
        prompt = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "user").lower()
        for pattern, tool_name, argument in self.RULES:
            if re.search(pattern, prompt) and tool_name in tools:
                return ProviderResponse(
                    tool_calls=[ToolCall(id=f"call-{self.turn}", name=tool_name, arguments={argument: self.target})]
                )
        return ProviderResponse(content="nothing to do")


class HttpProvider(Provider):
    """Adapts the existing OpenAI-compatible client to the provider contract.

    Kept thin on purpose: :mod:`agent.llm_client` already owns retries,
    timeouts and the wire dialect, and duplicating any of that here would give
    the two entry points two different failure modes.
    """

    name = "http"

    def __init__(self, client: Any, *, tool_schemas: Sequence[dict[str, Any]] | None = None) -> None:
        self.client = client
        self.tool_schemas = list(tool_schemas or [])

    def complete(self, messages: Sequence[dict[str, Any]], tools: Sequence[str]) -> ProviderResponse:
        schemas = [
            s for s in self.tool_schemas if s.get("function", {}).get("name") in set(tools)
        ] or None
        raw = self.client.chat(list(messages), tools=schemas)
        message = (raw.get("choices") or [{}])[0].get("message", {}) if isinstance(raw, dict) else {}
        calls = [
            ToolCall.from_dict(c, index=i) for i, c in enumerate(message.get("tool_calls") or [])
        ]
        return ProviderResponse(content=str(message.get("content") or ""), tool_calls=calls)


#: Providers addressable by name from a task payload. ``http`` is absent by
#: design — it needs a live client, which a packet cannot supply.
MOCK_PROVIDERS: dict[str, Callable[..., Provider]] = {
    "scripted": ScriptedProvider,
    "echo": EchoProvider,
    "keyword": KeywordToolProvider,
}


def build_provider(payload: dict[str, Any]) -> Provider:
    """Construct the provider a payload asks for."""
    name = str(payload.get("provider") or "echo").strip().lower()
    if name not in MOCK_PROVIDERS:
        raise LlmTaskError(
            f"unknown provider: {name!r}; available: {sorted(MOCK_PROVIDERS)}"
        )
    if name == "scripted":
        turns = payload.get("script") or payload.get("turns") or []
        if not isinstance(turns, list):
            raise LlmTaskError("scripted provider needs a 'script' list")
        return ScriptedProvider(turns)
    if name == "keyword":
        return KeywordToolProvider(target=str(payload.get("target", ".")))
    return EchoProvider(prefix=str(payload.get("prefix", "echo: ")))


@dataclass
class LlmTaskResult:
    """The outcome of one ``llm_task`` run."""

    ok: bool
    stop_reason: str
    iterations: int
    final_message: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    duration_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Render for a task result body."""
        body: dict[str, Any] = {
            "ok": self.ok,
            "stop_reason": self.stop_reason,
            "iterations": self.iterations,
            "final_message": self.final_message,
            "messages": self.messages,
            "tool_results": self.tool_results,
            "tool_calls": len(self.tool_results),
            "duration_sec": self.duration_sec,
        }
        if self.pending_confirmation is not None:
            body["pending_confirmation"] = self.pending_confirmation
        return body


class LlmTaskRunner:
    """Drives provider ↔ executor turns until the model stops asking for tools.

    ``execute`` is injected for the third time in this track, and for the same
    reason: the loop decides what to run, the executor decides whether it may.
    """

    def __init__(
        self,
        execute: Callable[[Any], Any],
        *,
        parent: Any = None,
        allowed_kinds: Sequence[str] = DEFAULT_TOOL_KINDS,
    ) -> None:
        self.execute = execute
        self.parent = parent
        self.allowed_kinds = list(allowed_kinds)

    def run(
        self,
        prompt: str,
        provider: Provider,
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        tools: Sequence[str] | None = None,
    ) -> LlmTaskResult:
        """Run the loop and return its transcript and stopping condition."""
        if not str(prompt or "").strip():
            raise LlmTaskError("llm_task needs a non-empty 'prompt'")
        limit = max(1, min(int(max_iterations), MAX_ALLOWED_ITERATIONS))
        available = [t for t in (tools or self.allowed_kinds) if t in self.allowed_kinds]

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        tool_results: list[dict[str, Any]] = []
        started = time.time()
        iterations = 0
        stop_reason = "max_iterations"
        final_message = ""
        pending: dict[str, Any] | None = None

        while iterations < limit:
            iterations += 1
            try:
                response = provider.complete(messages, available)
            except Exception as exc:
                log.exception("provider %s failed", getattr(provider, "name", "?"))
                return LlmTaskResult(
                    ok=False,
                    stop_reason="provider_error",
                    iterations=iterations,
                    final_message=f"{type(exc).__name__}: {exc}",
                    messages=messages,
                    tool_results=tool_results,
                    duration_sec=time.time() - started,
                )

            if not response.wants_tools:
                messages.append({"role": "assistant", "content": response.content})
                final_message = response.content
                stop_reason = "final"
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                        for c in response.tool_calls
                    ],
                }
            )

            halt = ""
            for call in response.tool_calls:
                record = self._run_tool(call, iterations, available)
                tool_results.append(record)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "name": call.name,
                     "content": record["content"]}
                )
                if record["decision"] == "confirm_required" and not halt:
                    halt = "confirm_required"
                    pending = {"tool_call_id": call.id, "tool": call.name, "reason": record["error"]}
                elif record["decision"] == "block" and not halt:
                    halt = "blocked"
                    pending = {"tool_call_id": call.id, "tool": call.name, "reason": record["error"]}

            if halt:
                stop_reason = halt
                final_message = f"halted: {halt}"
                break

        return LlmTaskResult(
            ok=stop_reason == "final",
            stop_reason=stop_reason,
            iterations=iterations,
            final_message=final_message,
            messages=messages,
            tool_results=tool_results,
            pending_confirmation=pending,
            duration_sec=time.time() - started,
        )

    def _run_tool(self, call: ToolCall, iteration: int, available: Sequence[str]) -> dict[str, Any]:
        """Dispatch one tool call through the executor, never raising.

        Enforced against ``available`` — the per-run list — not against
        ``allowed_kinds``. A model that calls a tool it was not offered must be
        refused, or narrowing the tool list would be advice rather than a limit.
        """
        if call.name not in available:
            return {
                "tool_call_id": call.id, "tool": call.name, "ok": False,
                "decision": "unavailable_tool", "error": f"tool not available: {call.name}",
                "content": f"error: tool not available: {call.name}",
            }
        try:
            task = build_subtask(
                {
                    "id": f"{getattr(self.parent, 'id', 'llm')}-tool-{iteration}-{call.id}",
                    "kind": call.name,
                    "payload": call.arguments,
                },
                parent=self.parent,
            )
            result = self.execute(task)
        except Exception as exc:
            log.exception("tool %s raised", call.name)
            return {
                "tool_call_id": call.id, "tool": call.name, "ok": False,
                "decision": "error", "error": f"{type(exc).__name__}: {exc}",
                "content": f"error: {exc}",
            }

        stdout = (getattr(result, "stdout", "") or "")[-4000:]
        stderr = (getattr(result, "stderr", "") or "")[-1000:]
        error = getattr(result, "error", "") or ""
        return {
            "tool_call_id": call.id,
            "tool": call.name,
            "ok": bool(getattr(result, "ok", False)),
            "decision": getattr(result, "decision", ""),
            "exit_code": getattr(result, "exit_code", None),
            "error": error,
            "content": stdout or stderr or error or "(no output)",
        }

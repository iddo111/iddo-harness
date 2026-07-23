"""
llm_loop.py — LlmDrivenLoop: an alternative entry point to main.py.

Where main.py drives the harness from the GitHub bridge (poller → executor
→ reporter), LlmDrivenLoop drives it directly from a local LLM: the model
proposes tool_calls, the executor runs them (subject to the same policy
engine as everything else), and results are fed back to the model until it
stops calling tools (or we hit max_iterations).

This module does NOT replace or modify main.py — it is invoked separately,
e.g. via `iddo-harness ask <role> "<prompt>"` (see patches/cli_llm_commands.py).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable

from amp import AmpEnvelope, build_envelope, to_json

from executor import Executor, Result
from policy import Decision, PolicyEngine
from llm_router import LlmRouter
from llm_tools import (
    ALL_TOOLS,
    envelope_to_executor_task,
    result_to_harness_result_envelope,
    result_to_tool_message,
    tool_call_to_task,
)

log = logging.getLogger("harness.llm_loop")

DEFAULT_MAX_ITERATIONS = 25

DEFAULT_SYSTEM_PROMPT = (
    "You are the Iddo Harness agent, running on the user's own machine with "
    "real shell/file access via tools. Use the `shell`, `read_file`, "
    "`write_file`, and `list_dir` tools to accomplish the user's request. "
    "Some tool calls may come back with decision=confirm_required — this "
    "means the harness policy engine paused the action for the human owner "
    "to approve; do not assume it ran. When you are done, reply with a "
    "final plain-text message and no further tool_calls."
)


class PendingConfirmation(Exception):
    """
    Raised (and caught internally, surfaced via LoopResult.paused=True)
    when the policy engine returns Decision.CONFIRM for a tool call the LLM
    requested. The loop halts — it does NOT auto-approve — and the caller
    (CLI / another driver) is responsible for resuming once a human
    approves out-of-band (e.g. via the existing push/SMS confirm flow
    described in executor.py's NOTE).
    """
    def __init__(self, tool_call_id: str, reason: str):
        self.tool_call_id = tool_call_id
        self.reason = reason
        super().__init__(f"Confirmation required for {tool_call_id}: {reason}")


@dataclass
class LoopResult:
    done: bool
    paused: bool
    iterations: int
    final_message: str | None
    messages: list[dict]
    audit_log: list[dict] = field(default_factory=list)  # list of harness_result envelope dicts
    pending_confirmation: dict | None = None


class LlmDrivenLoop:
    """
    Runs an LLM-driven agent loop against the harness's own executor.

    Parameters
    ----------
    router:
        an LlmRouter instance (already constructed from policy.yaml's
        llm_backends section).
    policy:
        a PolicyEngine instance — reused as-is; LlmDrivenLoop never
        bypasses it.
    executor:
        an Executor instance bound to the same policy engine.
    role:
        which llm_backends role to route to (default "coder").
    max_iterations:
        safety cap on tool-call round-trips (default 25).
    on_confirm_required:
        optional callback invoked with (tool_call, reason) whenever the
        policy engine pauses the loop for human approval. Defaults to a
        no-op; the CLI layer can wire this to an actual notification.
    """

    def __init__(
        self,
        router: LlmRouter,
        policy: PolicyEngine,
        executor: Executor,
        role: str = "coder",
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        on_confirm_required: Callable[[dict, str], None] | None = None,
        source_instance: str | None = None,
    ):
        self.router = router
        self.policy = policy
        self.executor = executor
        self.role = role
        self.max_iterations = max_iterations
        self.system_prompt = system_prompt
        self.on_confirm_required = on_confirm_required or (lambda tool_call, reason: None)
        self.source_instance = source_instance or f"llm-loop-{uuid.uuid4().hex[:8]}"

    # -----------------------------------------------------------------------
    def run(self, prompt: str, extra_messages: list[dict] | None = None) -> LoopResult:
        """
        Runs the loop to completion (LLM stops requesting tools, a CONFIRM
        is hit, or max_iterations is reached) and returns a LoopResult.
        """
        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]
        if extra_messages:
            messages.extend(extra_messages)
        messages.append({"role": "user", "content": prompt})

        audit_log: list[dict] = []
        client = self.router.route(self.role)

        for iteration in range(1, self.max_iterations + 1):
            log.info(f"[{self.role}] iteration {iteration}/{self.max_iterations}")

            response = client.chat_completion(messages=messages, tools=ALL_TOOLS)
            choice = response["choices"][0]
            message = choice["message"]
            messages.append(message)

            tool_calls = message.get("tool_calls") or []
            audit_log.append(self._audit_iteration(iteration, "llm_response", {
                "content": message.get("content"),
                "tool_call_count": len(tool_calls),
                "finish_reason": choice.get("finish_reason"),
            }))

            if not tool_calls:
                # Model is done — no more tool calls requested.
                return LoopResult(
                    done=True,
                    paused=False,
                    iterations=iteration,
                    final_message=message.get("content"),
                    messages=messages,
                    audit_log=audit_log,
                )

            for tool_call in tool_calls:
                try:
                    envelope = tool_call_to_task(
                        tool_call,
                        source_instance=self.source_instance,
                    )
                except Exception as e:
                    log.exception("Failed to convert tool_call to task")
                    tool_msg = result_to_tool_message(
                        {"ok": False, "decision": "error", "error": str(e)},
                        tool_call_id=tool_call.get("id"),
                    )
                    messages.append(tool_msg)
                    continue

                task_view = envelope_to_executor_task(envelope)
                result = self.executor.run(task_view)

                result_envelope = result_to_harness_result_envelope(
                    result, source_instance=self.source_instance,
                )
                audit_log.append(self._audit_iteration(iteration, "tool_result", result_envelope.to_dict()))

                if result.decision == "confirm_required":
                    reason = result.error or "policy requires human confirmation"
                    self.on_confirm_required(tool_call, reason)
                    log.warning(f"Pausing loop: {reason}")
                    return LoopResult(
                        done=False,
                        paused=True,
                        iterations=iteration,
                        final_message=None,
                        messages=messages,
                        audit_log=audit_log,
                        pending_confirmation={
                            "tool_call": tool_call,
                            "reason": reason,
                            "envelope": envelope.to_dict(),
                        },
                    )

                tool_msg = result_to_tool_message(result, tool_call_id=tool_call.get("id"))
                messages.append(tool_msg)

        log.warning(f"[{self.role}] max_iterations ({self.max_iterations}) reached without completion")
        return LoopResult(
            done=False,
            paused=False,
            iterations=self.max_iterations,
            final_message=None,
            messages=messages,
            audit_log=audit_log,
        )

    # -----------------------------------------------------------------------
    def _audit_iteration(self, iteration: int, phase: str, data: dict) -> dict:
        """
        Every iteration is wrapped in an AMP-shaped audit record (per the
        task's requirement: "Every iteration wrapped in AMP audit log").
        Logged at INFO and returned so callers can persist/inspect it.
        """
        env = build_envelope(
            direction="outbound",
            source_brick="iddo-harness-llm-loop",
            source_instance=self.source_instance,
            channel="harness",
            identity_canonical=f"session:{self.source_instance}",
            payload_type="harness_result",
            payload_body={
                "task_id": f"llm-loop-iter-{iteration}-{phase}",
                "ok": True,
                "decision": "audit",
                "metadata": {"iteration": iteration, "phase": phase, "role": self.role, **data},
            },
            to_channel="harness",
            to_address=f"session:{self.source_instance}",
        )
        record = env.to_dict()
        log.info(f"AMP audit: {to_json(env)}")
        return record

"""
Executor v3 — the agent-native kinds.

v1 gave the harness hands. v2 gave it better hands: streaming, sessions,
surgical edits. Both share an assumption — one packet, one action, no memory of
the last one — that stops being true the moment the thing on the other end of
the bridge is an agent rather than a person clicking buttons.

Track C is what an agent needs that a remote control does not:

===================  ======================================================
kind                 what it does
===================  ======================================================
``spawn_task``       start child tasks and return their ids immediately
``await_tasks``      block until named children finish
``workflow``         run a whole dependency graph from one packet
``memory_set``       remember a fact, optionally with a TTL
``memory_get``       recall one
``memory_list``      browse what is remembered
``memory_delete``    forget one
``schedule_task``    run a task on a cron/interval/one-shot trigger
``schedule_list``    show registered schedules
``schedule_cancel``  drop one
``llm_task``         let a model drive tools through the policy engine
``handshake``        report capabilities and negotiate compatibility
``run_template``     run a named, parameterised plan
``template_list``    show the available templates
===================  ======================================================

Two design rules hold throughout. **Nothing here is a second execution path** —
every child, node and tool call goes back through the same
:class:`agent.executor.Executor`, so it meets the same policy engine and
produces the same ``Result``. And **nothing here touches v1 or v2**:
``Executor.run()`` gains one router hook, exactly as it already has for v2.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

try:
    from executor import Result
except ImportError:  # pragma: no cover - packaged import
    from agent.executor import Result  # type: ignore[no-redef]

try:
    from handshake import build_report, negotiate
    from llm_task import LlmTaskError, LlmTaskRunner, build_provider
    from memory import MemoryError as MemoryStoreError, MemoryStore
    from scheduler import Scheduler, ScheduleError, parse_schedule
    from subtasks import SubtaskError, SubtaskManager, build_subtask
    from templates import TemplateError, TemplateRegistry
    from workflow import WorkflowError, WorkflowRunner, parse_workflow
except ImportError:  # pragma: no cover - packaged import
    from agent.handshake import build_report, negotiate  # type: ignore[no-redef]
    from agent.llm_task import LlmTaskError, LlmTaskRunner, build_provider  # type: ignore[no-redef]
    from agent.memory import MemoryError as MemoryStoreError, MemoryStore  # type: ignore[no-redef]
    from agent.scheduler import Scheduler, ScheduleError, parse_schedule  # type: ignore[no-redef]
    from agent.subtasks import SubtaskError, SubtaskManager, build_subtask  # type: ignore[no-redef]
    from agent.templates import TemplateError, TemplateRegistry  # type: ignore[no-redef]
    from agent.workflow import WorkflowError, WorkflowRunner, parse_workflow  # type: ignore[no-redef]

log = logging.getLogger("harness.executor_v3")

#: Every kind handled here. ``Executor.run()`` consults this set, so adding a
#: kind to it plus a handler is all the wiring a new verb needs.
V3_KINDS: frozenset[str] = frozenset(
    {
        "spawn_task",
        "await_tasks",
        "workflow",
        "memory_set",
        "memory_get",
        "memory_list",
        "memory_delete",
        "schedule_task",
        "schedule_list",
        "schedule_cancel",
        "llm_task",
        "handshake",
        "run_template",
        "template_list",
    }
)

#: Kinds that only read harness-local bookkeeping — no shell, no filesystem
#: outside the harness's own state directory, nothing a policy rule is written
#: to catch. Gating these behind the default-confirm rule would mean a human
#: taps approve to let an agent read its own notes.
_UNGATED_KINDS: frozenset[str] = frozenset(
    {"memory_get", "memory_list", "handshake", "template_list", "schedule_list", "await_tasks"}
)

DEFAULT_AWAIT_TIMEOUT = 300.0


def default_schedule_path() -> Path:
    """Where schedules persist, resolved against the current home directory."""
    return Path.home() / ".iddo-harness" / "schedules.json"


class ExecutorV3:
    """Runs the v3 agent-native kinds against the same policy engine as v1/v2.

    ``parent_executor`` is the full :class:`agent.executor.Executor`, not this
    class: a child task can be *any* kind, so recursion has to re-enter at the
    router rather than here. It is injected rather than constructed so there is
    exactly one executor — and therefore one set of live sessions, watches and
    policy decisions — per process.
    """

    def __init__(
        self,
        policy: Any,
        confirm_manager: Any = None,
        chunk_sink: Callable[[Any, dict[str, Any], int, bool], None] | None = None,
        *,
        parent_executor: Any = None,
        memory: MemoryStore | None = None,
        memory_path: str | Path | None = None,
        scheduler: Scheduler | None = None,
        schedule_path: str | Path | None = None,
        templates: TemplateRegistry | None = None,
        subtasks: SubtaskManager | None = None,
    ) -> None:
        self.policy = policy
        self.confirm_manager = confirm_manager
        self.chunk_sink = chunk_sink
        self.parent_executor = parent_executor
        self.templates = templates or TemplateRegistry()
        self._memory = memory
        self._memory_path = memory_path
        self._scheduler = scheduler
        self._schedule_path = schedule_path
        self._subtasks = subtasks
        self._seq: dict[str, int] = {}

    # -- lazily-built collaborators ------------------------------------------
    # Built on first use so a harness that never sends a memory_* packet never
    # creates a database, and a test that only exercises workflows never starts
    # a thread pool.

    @property
    def memory(self) -> MemoryStore:
        """The SQLite memory store, opened on first use."""
        if self._memory is None:
            self._memory = MemoryStore(self._memory_path)
        return self._memory

    @property
    def scheduler(self) -> Scheduler:
        """The schedule registry, loaded from disk on first use."""
        if self._scheduler is None:
            path = self._schedule_path if self._schedule_path is not None else default_schedule_path()
            self._scheduler = Scheduler(dispatch=self._dispatch_schedule, path=path)
        return self._scheduler

    @property
    def subtasks(self) -> SubtaskManager:
        """The child-task manager, whose pool starts on first use."""
        if self._subtasks is None:
            self._subtasks = SubtaskManager(self._execute_child)
        return self._subtasks

    # -- dispatch -----------------------------------------------------------

    def handles(self, kind: str) -> bool:
        """True when ``kind`` is one of the v3 agent-native kinds."""
        return kind in V3_KINDS

    def run(self, task: Any) -> Result:
        """Policy-gate and execute ``task``, returning a v1-shaped Result."""
        handler = self._handlers().get(task.kind)
        if handler is None:
            return Result(
                task_id=task.id, ok=False, decision="unknown_kind", error=f"unknown kind: {task.kind}"
            )
        early = self._gate(task)
        if early is not None:
            return early
        return self._invoke(task, handler)

    def resume_after_confirm(self, task: Any, approved: bool) -> Result:
        """Execute a previously parked task once a human has answered."""
        if not approved:
            return Result(
                task_id=task.id, ok=False, decision="denied", error="confirmation denied by user"
            )
        handler = self._handlers().get(task.kind)
        if handler is None:
            return Result(
                task_id=task.id, ok=False, decision="unknown_kind", error=f"unknown kind: {task.kind}"
            )
        return self._invoke(task, handler, decision="approved")

    def _invoke(self, task: Any, handler: Callable[..., Result], decision: str = "auto") -> Result:
        """Run a handler, converting the expected failures into Results.

        The narrow excepts come first so a producer's mistake reads as
        ``bad_request`` with the reason, and only a genuine surprise falls
        through to the catch-all.
        """
        try:
            return handler(task, decision)
        except (
            WorkflowError,
            TemplateError,
            ScheduleError,
            SubtaskError,
            LlmTaskError,
            MemoryStoreError,
        ) as exc:
            log.info("task=%s kind=%s rejected: %s", task.id, task.kind, exc)
            return Result(task_id=task.id, ok=False, decision="bad_request", error=str(exc))
        except Exception as exc:  # never let a v3 kind take the poller down
            log.exception("task=%s kind=%s raised", task.id, task.kind)
            return Result(
                task_id=task.id, ok=False, decision="error", error=f"{type(exc).__name__}: {exc}"
            )

    def _handlers(self) -> dict[str, Callable[[Any, str], Result]]:
        return {
            "spawn_task": self._spawn_task,
            "await_tasks": self._await_tasks,
            "workflow": self._workflow,
            "memory_set": self._memory_set,
            "memory_get": self._memory_get,
            "memory_list": self._memory_list,
            "memory_delete": self._memory_delete,
            "schedule_task": self._schedule_task,
            "schedule_list": self._schedule_list,
            "schedule_cancel": self._schedule_cancel,
            "llm_task": self._llm_task,
            "handshake": self._handshake,
            "run_template": self._run_template,
            "template_list": self._template_list,
        }

    # -- policy -------------------------------------------------------------

    def _gate(self, task: Any) -> Result | None:
        """Apply the policy engine to a v3 kind, or skip it for read-only ones.

        Composite kinds (``workflow``, ``spawn_task``, ``run_template``,
        ``llm_task``) are *not* gated here on purpose: gating the container
        would ask the owner to approve an envelope whose contents they cannot
        see. Every child instead meets the policy engine on its own terms when
        it reaches the executor, which is the decision the owner can actually
        make.
        """
        if task.kind in _UNGATED_KINDS or self.policy is None:
            return None
        if task.kind in {"workflow", "spawn_task", "run_template", "llm_task"}:
            return None

        command, paths = self._policy_probe(task)
        decision, reason = self.policy.decide(command, paths)
        self.policy.audit(task.id, command, decision, reason)
        value = getattr(decision, "value", str(decision))
        if value == "block":
            return Result(task_id=task.id, ok=False, decision="block", error=reason)
        if value == "confirm" and self.confirm_manager is not None:
            pending = self.confirm_manager.create(task, reason)
            return Result(
                task_id=task.id,
                ok=False,
                decision="confirm_required",
                error=reason,
                metadata={"message": getattr(pending, "message", reason)},
            )
        return None

    @staticmethod
    def _policy_probe(task: Any) -> tuple[str, list[str]]:
        """Synthesise the command line a v3 kind is equivalent to.

        The same trick v2 uses for ``grep``/``patch``: these kinds have no real
        command line, so they present one the rules in ``policy.yaml`` can
        actually match.
        """
        payload = getattr(task, "payload", {}) or {}
        if task.kind.startswith("memory_"):
            return f"{task.kind} {payload.get('key', '')}".strip(), []
        if task.kind == "schedule_task":
            inner = payload.get("task") or {}
            return f"schedule {inner.get('kind', '')}".strip(), []
        if task.kind == "schedule_cancel":
            return f"schedule_cancel {payload.get('schedule_id', '')}".strip(), []
        return task.kind, []

    # -- chunk streaming ----------------------------------------------------

    def _next_seq(self, task_id: str) -> int:
        seq = self._seq.get(task_id, 0)
        self._seq[task_id] = seq + 1
        return seq

    def _emit(self, task: Any, body: dict[str, Any], is_final: bool = False) -> dict[str, Any]:
        """Send one chunk to the sink, mirroring ExecutorV2's contract."""
        seq = self._next_seq(task.id)
        body = {"task_id": task.id, "seq": seq, "is_final": is_final, **body}
        if self.chunk_sink is not None:
            try:
                self.chunk_sink(task, body, seq, is_final)
            except Exception:  # a failing transport must not abort the work
                log.exception("task=%s chunk_sink failed at seq=%d", task.id, seq)
        return body

    # -- child execution ----------------------------------------------------

    def _execute_child(self, task: Any) -> Result:
        """Run one child through the full router, so any kind is reachable."""
        if self.parent_executor is None:
            raise SubtaskError("no parent executor is wired up; child tasks cannot run")
        return self.parent_executor.run(task)

    def _dispatch_schedule(self, schedule: Any) -> Result:
        """Fire a scheduled task through the executor."""
        task = build_subtask(
            {**schedule.task, "id": f"{schedule.id}-run-{schedule.run_count + 1}"},
        )
        return self._execute_child(task)

    # -- 1/2. sub-tasks -----------------------------------------------------

    def _spawn_task(self, task: Any, decision: str = "auto") -> Result:
        """Start one or more children and return their ids without waiting."""
        payload = task.payload or {}
        specs = payload.get("tasks")
        if specs is None:
            single = payload.get("task")
            specs = [single] if single is not None else []
        if not isinstance(specs, list) or not specs:
            raise SubtaskError("spawn_task needs a 'tasks' list (or a single 'task')")

        depth = int(getattr(task, "depth", 0)) + 1
        children = [
            build_subtask(spec, parent=task, depth=depth, index=i) for i, spec in enumerate(specs)
        ]
        records = self.subtasks.spawn_many(children)
        spawned = [r.to_dict(include_result=False) for r in records]

        wait_for = payload.get("await") or payload.get("wait")
        if wait_for:
            awaited = self.subtasks.await_tasks(
                [r.id for r in records], timeout=float(payload.get("timeout_sec", DEFAULT_AWAIT_TIMEOUT))
            )
            return self._finish(
                task, decision, awaited["ok"],
                {"spawned": spawned, "awaited": awaited, "task_ids": [r.id for r in records]},
            )

        return self._finish(
            task, decision, True, {"spawned": spawned, "task_ids": [r.id for r in records]}
        )

    def _await_tasks(self, task: Any, decision: str = "auto") -> Result:
        """Block until the named children finish, or the timeout expires."""
        payload = task.payload or {}
        ids = payload.get("task_ids") or payload.get("ids") or []
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list):
            raise SubtaskError("await_tasks needs a 'task_ids' list")

        outcome = self.subtasks.await_tasks(
            ids,
            timeout=float(payload.get("timeout_sec", DEFAULT_AWAIT_TIMEOUT)),
            require_all=bool(payload.get("require_all", True)),
        )
        return self._finish(task, decision, outcome["ok"], outcome)

    # -- 3. workflow --------------------------------------------------------

    def _workflow(self, task: Any, decision: str = "auto") -> Result:
        """Validate and run a dependency graph, streaming node completions."""
        spec = parse_workflow(task.payload or {})
        runner = WorkflowRunner(
            self._execute_child,
            parent=task,
            on_node_done=lambda outcome: self._emit(task, {"node": outcome.to_dict()}),
        )
        summary = runner.run(spec)
        return self._finish(task, decision, summary["ok"], summary, streamed=True)

    # -- 4. memory ----------------------------------------------------------

    def _memory_set(self, task: Any, decision: str = "auto") -> Result:
        payload = task.payload or {}
        if "value" not in payload:
            raise MemoryStoreError("memory_set needs a 'value'")
        record = self.memory.set(
            payload.get("key", ""),
            payload["value"],
            namespace=payload.get("namespace", "default"),
            ttl_seconds=payload.get("ttl_seconds"),
            tags=payload.get("tags"),
        )
        return self._finish(task, decision, True, {"record": record.to_dict()})

    def _memory_get(self, task: Any, decision: str = "auto") -> Result:
        payload = task.payload or {}
        key = payload.get("key", "")
        record = self.memory.get_record(key, namespace=payload.get("namespace", "default"))
        if record is None:
            # A miss is a real answer, not a failure — "I do not remember" is
            # what the caller asked about, and ok=false would make every
            # first-run lookup look broken.
            return self._finish(
                task, decision, True,
                {"found": False, "key": key, "namespace": payload.get("namespace", "default"), "value": None},
            )
        return self._finish(task, decision, True, {"found": True, **record.to_dict()})

    def _memory_list(self, task: Any, decision: str = "auto") -> Result:
        payload = task.payload or {}
        namespace = payload.get("namespace", "default")
        if namespace in ("*", "all"):
            namespace = None
        records = self.memory.list(
            namespace=namespace,
            prefix=payload.get("prefix"),
            tag=payload.get("tag"),
            limit=payload.get("limit"),
        )
        include_values = bool(payload.get("include_values", True))
        return self._finish(
            task, decision, True,
            {
                "count": len(records),
                "namespaces": self.memory.namespaces(),
                "records": [r.to_dict(include_value=include_values) for r in records],
            },
        )

    def _memory_delete(self, task: Any, decision: str = "auto") -> Result:
        payload = task.payload or {}
        namespace = payload.get("namespace", "default")
        if payload.get("clear_namespace"):
            removed = self.memory.clear(namespace=namespace)
            return self._finish(task, decision, True, {"cleared": removed, "namespace": namespace})
        key = payload.get("key", "")
        if not key:
            raise MemoryStoreError("memory_delete needs a 'key' (or clear_namespace: true)")
        deleted = self.memory.delete(key, namespace=namespace)
        return self._finish(
            task, decision, True, {"deleted": deleted, "key": key, "namespace": namespace}
        )

    # -- 5. scheduling ------------------------------------------------------

    def _schedule_task(self, task: Any, decision: str = "auto") -> Result:
        schedule = parse_schedule(task.payload or {})
        self.scheduler.add(schedule)
        return self._finish(task, decision, True, {"schedule": schedule.to_dict()})

    def _schedule_list(self, task: Any, decision: str = "auto") -> Result:
        payload = task.payload or {}
        schedules = self.scheduler.list(
            include_exhausted=bool(payload.get("include_exhausted", True))
        )
        return self._finish(
            task, decision, True,
            {"count": len(schedules), "schedules": [s.to_dict() for s in schedules]},
        )

    def _schedule_cancel(self, task: Any, decision: str = "auto") -> Result:
        payload = task.payload or {}
        schedule_id = payload.get("schedule_id") or payload.get("id") or ""
        if not schedule_id:
            raise ScheduleError("schedule_cancel needs a 'schedule_id'")
        removed = self.scheduler.remove(str(schedule_id))
        return self._finish(
            task, decision, removed,
            {"removed": removed, "schedule_id": schedule_id},
            error="" if removed else f"no such schedule: {schedule_id}",
        )

    # -- 6. LLM loop --------------------------------------------------------

    def _llm_task(self, task: Any, decision: str = "auto") -> Result:
        payload = task.payload or {}
        provider = build_provider(payload)
        runner = LlmTaskRunner(self._execute_child, parent=task)
        outcome = runner.run(
            str(payload.get("prompt", "")),
            provider,
            system_prompt=str(payload.get("system_prompt") or _default_system(payload)),
            max_iterations=int(payload.get("max_iterations", 10)),
            tools=payload.get("tools"),
        )
        body = outcome.to_dict()
        body["provider"] = getattr(provider, "name", "?")
        return self._finish(task, decision, outcome.ok, body, error=_llm_error(outcome))

    # -- 7. handshake -------------------------------------------------------

    def _handshake(self, task: Any, decision: str = "auto") -> Result:
        report = build_report(policy=self.policy, v3_kinds=V3_KINDS, limits=self._limits())
        body = negotiate(report, task.payload or {})
        return self._finish(task, decision, body["negotiation"]["compatible"], body)

    def _limits(self) -> dict[str, Any]:
        """Runtime ceilings a producer should plan against."""
        try:
            from memory import MAX_VALUE_BYTES
            from workflow import MAX_NODES
        except ImportError:  # pragma: no cover - packaged import
            from agent.memory import MAX_VALUE_BYTES  # type: ignore[no-redef]
            from agent.workflow import MAX_NODES  # type: ignore[no-redef]
        return {
            "max_workflow_nodes": MAX_NODES,
            "max_memory_value_bytes": MAX_VALUE_BYTES,
            "max_subtask_depth": self.subtasks.max_depth,
            "max_active_subtasks": self.subtasks.max_active,
            "templates": self.templates.names(),
        }

    # -- 8. templates -------------------------------------------------------

    def _run_template(self, task: Any, decision: str = "auto") -> Result:
        payload = task.payload or {}
        name = payload.get("template") or payload.get("name") or ""
        rendered = self.templates.render(str(name), payload.get("params"))
        if payload.get("dry_run"):
            # Rendering without running is how a producer checks a template's
            # parameters against a real registry before committing to the run.
            return self._finish(task, decision, True, {"dry_run": True, "workflow": rendered})

        spec = parse_workflow(rendered)
        runner = WorkflowRunner(
            self._execute_child,
            parent=task,
            on_node_done=lambda outcome: self._emit(task, {"node": outcome.to_dict()}),
        )
        summary = runner.run(spec)
        summary["template"] = rendered["template"]
        summary["params"] = rendered["params"]
        return self._finish(task, decision, summary["ok"], summary, streamed=True)

    def _template_list(self, task: Any, decision: str = "auto") -> Result:
        payload = task.payload or {}
        listing = self.templates.list(include_nodes=bool(payload.get("include_nodes", False)))
        return self._finish(task, decision, True, {"count": len(listing), "templates": listing})

    # -- results ------------------------------------------------------------

    def _finish(
        self,
        task: Any,
        decision: str,
        ok: bool,
        body: dict[str, Any],
        *,
        error: str = "",
        streamed: bool = False,
    ) -> Result:
        """Build the Result, emitting a closing chunk for streaming kinds.

        Only kinds that emitted progress chunks get a final chunk: a
        ``memory_get`` that never streamed anything must not start a chunk
        stream just to close it, or a consumer following the chunk protocol
        sees a stream it never subscribed to.
        """
        metadata = dict(body)
        if streamed:
            final = self._emit(task, {"ok": ok, "decision": decision, **body}, is_final=True)
            metadata["final_chunk"] = final
        return Result(
            task_id=task.id,
            ok=ok,
            decision=decision,
            error=error or ("" if ok else str(body.get("error", ""))),
            metadata=metadata,
        )

    # -- lifecycle ----------------------------------------------------------

    def shutdown(self) -> None:
        """Release the collaborators this executor started. Safe to call twice."""
        if self._subtasks is not None:
            self._subtasks.shutdown()
        if self._scheduler is not None:
            self._scheduler.stop()
        if self._memory is not None:
            self._memory.close()


def _default_system(payload: dict[str, Any]) -> str:
    try:
        from llm_task import DEFAULT_SYSTEM_PROMPT
    except ImportError:  # pragma: no cover - packaged import
        from agent.llm_task import DEFAULT_SYSTEM_PROMPT  # type: ignore[no-redef]
    return str(payload.get("system_prompt") or DEFAULT_SYSTEM_PROMPT)


def _llm_error(outcome: Any) -> str:
    """Surface a non-``final`` stop reason as the Result's error."""
    if outcome.stop_reason == "final":
        return ""
    return f"llm loop stopped: {outcome.stop_reason}"

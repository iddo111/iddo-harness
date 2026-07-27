"""
Sub-tasks — a task that starts other tasks and waits for them.

v1 and v2 both assume one packet, one action. Anything shaped like "build all
four services, then report" has to be decomposed by the *producer*, round-trip
by round-trip, at one poll interval each. That is the wrong place for the
decomposition: the harness already knows how to run every kind, and it is the
side of the wire without the latency.

So: ``spawn_task`` starts a child on a worker thread and returns its id
immediately, and ``await_tasks`` blocks until the ids you name have finished
(or a timeout expires). The child is an ordinary task packet — same kinds,
same policy engine, same Result — which is what keeps this from becoming a
second execution model.

Two guards make recursion survivable: ``max_depth`` stops a template that
spawns itself, and ``max_active`` bounds how much work one harness will have
in flight regardless of how enthusiastically a producer asks.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger("harness.subtasks")

DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_ACTIVE = 8
DEFAULT_AWAIT_TIMEOUT = 300.0

#: Terminal states. Anything else means the child is still going.
FINISHED_STATES = frozenset({"succeeded", "failed", "cancelled"})


class SubtaskError(RuntimeError):
    """Raised when a spawn is refused: bad spec, too deep, or too many."""


@dataclass
class SubTask:
    """A task packet built in-process rather than read off the bridge.

    Deliberately duck-type-compatible with :class:`agent.poller.Task` — the
    executor only ever touches ``id``/``kind``/``payload``/``envelope``, and a
    child inherits its parent's envelope so a result still knows its way home.
    """

    id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    priority: str = "normal"
    source_path: Path | None = None
    envelope: Any = None
    parent_id: str | None = None
    depth: int = 0


@dataclass
class SubtaskRecord:
    """Bookkeeping for one spawned child."""

    id: str
    kind: str
    parent_id: str | None
    depth: int
    state: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: str = ""

    @property
    def finished(self) -> bool:
        return self.state in FINISHED_STATES

    @property
    def duration(self) -> float | None:
        """Wall time spent executing, or ``None`` if it has not run yet."""
        if self.started_at is None:
            return None
        return (self.finished_at or time.time()) - self.started_at

    def to_dict(self, *, include_result: bool = True) -> dict[str, Any]:
        """Render for a task result body, flattening the child's Result."""
        body: dict[str, Any] = {
            "task_id": self.id,
            "kind": self.kind,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_sec": self.duration,
        }
        if self.error:
            body["error"] = self.error
        if include_result and self.result is not None:
            body["result"] = _result_summary(self.result)
        return body


def _result_summary(result: Any) -> dict[str, Any]:
    """Compact a child's Result down to what a parent actually reads back.

    The full stdout of a child is already in its own chunk stream; repeating
    all of it inside the parent's result would double every byte the bridge
    carries for a fan-out of ten.
    """
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return {
        "ok": bool(getattr(result, "ok", False)),
        "decision": getattr(result, "decision", ""),
        "exit_code": getattr(result, "exit_code", None),
        "error": getattr(result, "error", "") or "",
        "stdout": stdout[-4000:],
        "stderr": stderr[-2000:],
        "stdout_truncated": len(stdout) > 4000,
    }


def build_subtask(
    spec: dict[str, Any],
    *,
    parent: Any = None,
    depth: int = 0,
    index: int = 0,
) -> SubTask:
    """Turn a ``{kind, payload}`` mapping into a runnable :class:`SubTask`.

    The id is derived from the parent's when the spec does not name one, so a
    fan-out is legible in the results directory (``build-42-sub-0``) instead of
    being a row of unrelated UUIDs.
    """
    if not isinstance(spec, dict):
        raise SubtaskError(f"task spec must be an object, got {type(spec).__name__}")
    kind = str(spec.get("kind") or "").strip()
    if not kind:
        raise SubtaskError("task spec needs a 'kind'")

    given_id = str(spec.get("id") or "").strip()
    if given_id:
        task_id = given_id
    elif parent is not None:
        task_id = f"{getattr(parent, 'id', 'task')}-sub-{index}"
    else:
        task_id = f"sub-{uuid.uuid4().hex[:12]}"

    payload = spec.get("payload")
    if payload is None:
        # Allow the flattened form (`{"kind": "shell", "command": "ls"}`),
        # which is what a producer writes by hand more often than not.
        payload = {k: v for k, v in spec.items() if k not in {"id", "kind", "payload", "depends_on"}}
    if not isinstance(payload, dict):
        raise SubtaskError("task payload must be an object")

    return SubTask(
        id=task_id,
        kind=kind,
        payload=dict(payload),
        priority=str(spec.get("priority", "normal")),
        envelope=getattr(parent, "envelope", None),
        parent_id=getattr(parent, "id", None),
        depth=depth,
    )


class SubtaskManager:
    """Runs child tasks on a bounded thread pool and tracks their outcomes.

    ``execute`` is injected — in production a closure over the shared
    :class:`agent.executor.Executor`, in tests a stub — so this module holds
    scheduling logic and no execution logic.
    """

    def __init__(
        self,
        execute: Callable[[Any], Any],
        *,
        max_active: int = DEFAULT_MAX_ACTIVE,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        self.execute = execute
        self.max_active = max(1, int(max_active))
        self.max_depth = max(1, int(max_depth))
        self._pool = ThreadPoolExecutor(max_workers=self.max_active, thread_name_prefix="subtask")
        self._lock = threading.RLock()
        self._records: dict[str, SubtaskRecord] = {}
        self._events: dict[str, threading.Event] = {}
        self._cancelled: set[str] = set()

    # -- spawning -----------------------------------------------------------

    def spawn(self, task: SubTask) -> SubtaskRecord:
        """Start ``task`` on a worker thread and return its record at once."""
        if task.depth > self.max_depth:
            raise SubtaskError(
                f"sub-task depth {task.depth} exceeds max_depth={self.max_depth}"
            )
        with self._lock:
            if task.id in self._records and not self._records[task.id].finished:
                raise SubtaskError(f"sub-task id already in flight: {task.id}")
            if self.active_count() >= self.max_active:
                raise SubtaskError(
                    f"too many sub-tasks in flight (max_active={self.max_active})"
                )
            record = SubtaskRecord(
                id=task.id, kind=task.kind, parent_id=task.parent_id, depth=task.depth
            )
            self._records[task.id] = record
            self._events[task.id] = threading.Event()

        self._pool.submit(self._run, task, record)
        log.info("spawned sub-task %s (kind=%s depth=%d)", task.id, task.kind, task.depth)
        return record

    def spawn_many(self, tasks: Iterable[SubTask]) -> list[SubtaskRecord]:
        """Spawn a batch, stopping at the first refusal.

        Partial success is reported by the records that came back, so a caller
        that hit ``max_active`` halfway can still await what did start.
        """
        started: list[SubtaskRecord] = []
        for task in tasks:
            started.append(self.spawn(task))
        return started

    def _run(self, task: SubTask, record: SubtaskRecord) -> None:
        with self._lock:
            if task.id in self._cancelled:
                record.state = "cancelled"
                record.finished_at = time.time()
                self._events[task.id].set()
                return
            record.state = "running"
            record.started_at = time.time()
        try:
            result = self.execute(task)
            with self._lock:
                record.result = result
                record.state = "succeeded" if getattr(result, "ok", False) else "failed"
        except Exception as exc:  # a child blowing up must not kill the parent
            log.exception("sub-task %s raised", task.id)
            with self._lock:
                record.state = "failed"
                record.error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                record.finished_at = time.time()
                self._events[task.id].set()

    # -- awaiting -----------------------------------------------------------

    def await_tasks(
        self,
        task_ids: Iterable[str],
        *,
        timeout: float = DEFAULT_AWAIT_TIMEOUT,
        require_all: bool = True,
    ) -> dict[str, Any]:
        """Block until the named children finish, or ``timeout`` elapses.

        ``require_all=False`` returns as soon as *one* finishes — the shape you
        want for "whichever mirror answers first". Unknown ids are reported
        rather than waited on, because waiting forever for a task that was
        never spawned is the worst available behaviour.
        """
        ids = [str(t) for t in task_ids]
        if not ids:
            return {"ok": True, "completed": [], "pending": [], "unknown": [], "timed_out": False}

        with self._lock:
            unknown = [t for t in ids if t not in self._records]
            known = [t for t in ids if t in self._records]
            events = {t: self._events[t] for t in known}

        deadline = time.time() + max(0.0, float(timeout))
        timed_out = False
        for task_id, event in events.items():
            remaining = deadline - time.time()
            if not require_all and self._any_finished(known):
                break
            if remaining <= 0 or not event.wait(remaining):
                timed_out = True
                if require_all:
                    break

        with self._lock:
            records = [self._records[t] for t in known]
        completed = [r for r in records if r.finished]
        pending = [r for r in records if not r.finished]

        return {
            "ok": not unknown and not pending and all(r.state == "succeeded" for r in completed),
            "completed": [r.to_dict() for r in completed],
            "pending": [r.to_dict(include_result=False) for r in pending],
            "unknown": unknown,
            "timed_out": timed_out,
        }

    def _any_finished(self, ids: Iterable[str]) -> bool:
        with self._lock:
            return any(self._records[t].finished for t in ids if t in self._records)

    # -- introspection ------------------------------------------------------

    def status(self, task_id: str) -> SubtaskRecord | None:
        """Return one child's record, or ``None`` when the id is unknown."""
        with self._lock:
            return self._records.get(task_id)

    def active_count(self) -> int:
        """How many children are queued or running right now."""
        with self._lock:
            return sum(1 for r in self._records.values() if not r.finished)

    def all_records(self) -> list[SubtaskRecord]:
        """Every record this manager has seen, oldest first."""
        with self._lock:
            return sorted(self._records.values(), key=lambda r: r.created_at)

    def cancel(self, task_id: str) -> bool:
        """Mark a child cancelled. Effective only before it starts running.

        A child that is already inside the executor is stopped through the v2
        cancellation path, not from here — this manager owns scheduling, and
        killing a live process is the executor's job.
        """
        with self._lock:
            self._cancelled.add(task_id)
            record = self._records.get(task_id)
            if record is None:
                return False
            if record.state == "queued":
                record.state = "cancelled"
                record.finished_at = time.time()
                self._events[task_id].set()
                return True
            return False

    def forget_finished(self) -> int:
        """Drop finished records so a long-lived harness does not grow forever."""
        with self._lock:
            done = [t for t, r in self._records.items() if r.finished]
            for task_id in done:
                self._records.pop(task_id, None)
                self._events.pop(task_id, None)
                self._cancelled.discard(task_id)
            return len(done)

    def shutdown(self, *, wait: bool = False) -> None:
        """Stop accepting work and tear the pool down."""
        self._pool.shutdown(wait=wait, cancel_futures=not wait)

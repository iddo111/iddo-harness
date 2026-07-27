"""
Concurrent task runner: retries, cancellation, and a bounded worker pool.

v1/v2 ran tasks one at a time inside the polling loop, so a ten-minute build
blocked every ``read_file`` behind it. The runner replaces that inner loop:

* Tasks go through :class:`agent.queue.TaskQueue`, so priority and the
  ``not_before`` / ``deadline`` / ``depends_on`` constraints are honoured.
* Up to ``max_concurrent_tasks`` run at once on worker threads.
* A failing task is retried per its own ``retry`` policy (or the config
  default), each attempt recorded as ``results/<id>-attempt-<n>.json``.
* The ``cancel`` kind reaches into the queue or into
  :meth:`agent.executor_v2.ExecutorV2.stop` to take a task down.

The executor itself is unchanged apart from the cancellation hooks: everything
here is scheduling, so v1 and v2 task semantics are identical whether a task
arrives through this runner or through the old serial loop.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

try:  # Flat script-style imports, matching the rest of agent/.
    from executor import Result
    from taskqueue import TaskQueue, parse_schedule
except ImportError:  # pragma: no cover - package-style import
    from agent.executor import Result  # type: ignore[no-redef]
    from agent.taskqueue import TaskQueue, parse_schedule  # type: ignore[no-redef]

log = logging.getLogger("iddo-harness.runner")

CANCEL_KIND = "cancel"

#: A task that ended in one of these decisions is not worth retrying — the
#: outcome is a policy verdict or an operator action, not a flaky failure.
NON_RETRYABLE_DECISIONS = frozenset({"block", "blocked", "confirm_required", "cancelled"})


@dataclass
class Outcome:
    """What the runner did with one task."""

    task: Any
    result: Any
    attempts: int
    cancelled: bool = False

    @property
    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        decision = str(getattr(self.result, "decision", "") or "")
        if decision in {"block", "blocked"}:
            return "blocked"
        if decision == "confirm_required":
            return "confirm_required"
        return "ok" if getattr(self.result, "ok", False) else "error"


class TaskRunner:
    """Drives a :class:`TaskQueue` through an executor on a worker pool.

    ``sleep`` is injectable so retry backoff can be asserted in tests without
    actually waiting; production passes :func:`time.sleep`.
    """

    def __init__(
        self,
        *,
        executor: Any,
        reporter: Any,
        task_queue: TaskQueue | None = None,
        metrics: Any = None,
        max_concurrent_tasks: int = 3,
        default_max_attempts: int = 1,
        default_backoff_seconds: list[float] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_finished: Callable[[Outcome], None] | None = None,
    ) -> None:
        self.executor = executor
        self.reporter = reporter
        self.metrics = metrics
        self.max_concurrent_tasks = max(1, int(max_concurrent_tasks))
        self.default_max_attempts = max(1, int(default_max_attempts))
        self.default_backoff_seconds = list(default_backoff_seconds or [])
        self.sleep = sleep
        self.on_finished = on_finished
        self.queue = task_queue or TaskQueue(
            default_max_attempts=self.default_max_attempts,
            default_backoff_seconds=self.default_backoff_seconds,
        )

        self._slots = threading.Semaphore(self.max_concurrent_tasks)
        self._lock = threading.RLock()
        self._workers: dict[str, threading.Thread] = {}
        self._cancelled: set[str] = set()
        self._dequeued: dict[str, Any] = {}
        self._peak_concurrency = 0
        self._in_flight = 0

    # -- introspection ------------------------------------------------------

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def peak_concurrency(self) -> int:
        """High-water mark of simultaneous workers — asserted by the tests."""
        with self._lock:
            return self._peak_concurrency

    def running_task_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._workers)

    # -- submission ---------------------------------------------------------

    def submit(self, task: Any) -> bool:
        """Queue a task (or execute a ``cancel`` immediately).

        Cancellation jumps the queue on purpose: making it wait behind the work
        it is trying to stop would defeat the point.
        """
        if getattr(task, "kind", "") == CANCEL_KIND:
            self.handle_cancel(task)
            return True
        if self.metrics is not None:
            self.metrics.task_observed(str(task.id))
        return self.queue.add(task) is not None

    def submit_all(self, tasks: Any) -> int:
        return sum(1 for task in tasks if self.submit(task))

    # -- cancellation -------------------------------------------------------

    def cancel(self, target_id: str) -> str:
        """Cancel ``target_id``. Returns ``queued``, ``running`` or ``unknown``.

        ``queued`` means it was pulled out before starting, so the runner owes
        the producer a result file; ``running`` means the executor was
        signalled and will emit the final chunk itself.
        """
        with self._lock:
            self._cancelled.add(target_id)
            was_running = target_id in self._workers

        removed = self.queue.remove(target_id)
        if removed is not None and not was_running:
            # Keep the real task: its AMP envelope is what routes the synthetic
            # cancelled result back to whoever queued it.
            with self._lock:
                self._dequeued[target_id] = removed
            return "queued"

        stop = getattr(self.executor, "stop", None)
        if stop is None:
            v2 = getattr(self.executor, "v2", None)
            stop = getattr(v2, "stop", None) if v2 is not None else None
        signalled = bool(stop and stop(target_id))
        if was_running or signalled:
            return "running"
        return "unknown"

    def is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._cancelled

    def handle_cancel(self, task: Any) -> Any:
        """Execute a ``cancel`` task packet and report both results.

        The ``cancel`` task gets its own result (did the cancellation land?),
        and a target that never started gets a synthetic ``cancelled`` result
        so the producer is not left waiting for a chunk that will never come.
        """
        target = str((getattr(task, "payload", None) or {}).get("task_id") or "").strip()
        if not target:
            result = Result(
                task_id=task.id, ok=False, decision="error",
                error="cancel requires payload.task_id",
            )
            self._report(task, result)
            return result

        state = self.cancel(target)
        if state == "queued":
            self._report_cancelled_target(task, target)

        ok = state != "unknown"
        result = Result(
            task_id=task.id,
            ok=ok,
            decision="auto",
            stdout=f"{target}: {state}",
            error="" if ok else f"no such task: {target}",
            metadata={"target_task_id": target, "target_state": state},
        )
        self._report(task, result)
        if self.metrics is not None:
            self.metrics.task_completed(str(task.id), "ok" if ok else "error", CANCEL_KIND)
        return result

    def _report_cancelled_target(self, cancel_task: Any, target_id: str) -> None:
        """Write the final chunk for a target that was killed before starting."""
        body = {
            "ok": False,
            "decision": "cancelled",
            "status": "cancelled",
            "cancelled": True,
            "error": "cancelled",
            "cancelled_by": str(getattr(cancel_task, "id", "")),
        }
        with self._lock:
            target = self._dequeued.pop(target_id, None)
        if target is None:
            target = _TargetStub(target_id)
        send_chunk = getattr(self.reporter, "send_chunk", None)
        if send_chunk is not None:
            send_chunk(target, body, 0, True)
        else:  # pragma: no cover - v1 reporter fallback
            self.reporter.send_error(target, "cancelled")
        if self.metrics is not None:
            self.metrics.task_completed(target_id, "cancelled")

    # -- execution ----------------------------------------------------------

    def run_task(self, task: Any) -> Outcome:
        """Run one task to completion, retrying per its policy. Blocking."""
        schedule = parse_schedule(
            task,
            default_max_attempts=self.default_max_attempts,
            default_backoff_seconds=self.default_backoff_seconds,
        )
        retry = schedule.retry
        task_id = str(task.id)
        result: Any = None
        attempt = 0

        while attempt < retry.max_attempts:
            if attempt:
                # Backoff happens before the counter moves, so a cancellation
                # landing mid-sleep leaves `attempts` at the number of attempts
                # that actually reached the executor.
                delay = retry.delay_before(attempt + 1)
                if delay:
                    self.sleep(delay)
                if self.is_cancelled(task_id):
                    break
                if self.metrics is not None:
                    self.metrics.retry_attempted(task_id)
                log.info(f"task {task_id}: retry {attempt + 1}/{retry.max_attempts}")
            attempt += 1

            if self.metrics is not None:
                self.metrics.task_started(task_id, getattr(task, "kind", "unknown"))

            try:
                result = self.executor.run(task)
            except Exception as exc:  # an executor crash is a retryable failure
                log.exception(f"task {task_id}: executor raised")
                result = _error_result(task_id, str(exc))

            if not self._should_retry(result, task_id) or attempt >= retry.max_attempts:
                break
            # Only losing attempts get an attempt file; the settling attempt is
            # reported through the normal result path below.
            self.reporter.send_attempt(task, result, attempt)

        cancelled = self.is_cancelled(task_id) or bool(
            (getattr(result, "metadata", None) or {}).get("cancelled")
        )
        if attempt > 1:
            # Give the producer the full picture: the winning attempt is filed
            # under its own number too, so attempts 1..n are all on record.
            self.reporter.send_attempt(task, result, attempt)

        outcome = Outcome(task=task, result=result, attempts=attempt, cancelled=cancelled)
        self._report(task, result)
        if self.metrics is not None:
            self.metrics.task_completed(task_id, outcome.status, getattr(task, "kind", None))
        self.queue.mark_completed(task_id, ok=outcome.status == "ok")
        with self._lock:
            self._cancelled.discard(task_id)
        if self.on_finished is not None:
            self.on_finished(outcome)
        return outcome

    def _should_retry(self, result: Any, task_id: str) -> bool:
        if result is None or getattr(result, "ok", False):
            return False
        if self.is_cancelled(task_id):
            return False
        decision = str(getattr(result, "decision", "") or "")
        if decision in NON_RETRYABLE_DECISIONS:
            return False
        return not (getattr(result, "metadata", None) or {}).get("cancelled")

    def _report(self, task: Any, result: Any) -> None:
        """Send the result, unless the executor already emitted a final chunk.

        Streaming kinds close their own stream; re-sending here would put two
        ``is_final`` chunks on the wire and break the consumer contract.
        """
        if result is None:
            return
        if (getattr(result, "metadata", None) or {}).get("final_chunk"):
            return
        self.reporter.send(task, result)

    # -- pool ---------------------------------------------------------------

    def pump(self, *, now: Any = None) -> int:
        """Start as many eligible tasks as there are free slots.

        Non-blocking: returns the number of workers started. Expired tasks are
        reported as ``deadline_exceeded`` rather than silently dropped.
        """
        self._report_expired(now)
        started = 0
        while True:
            if not self._slots.acquire(blocking=False):
                break
            task = self.queue.next_task(now)
            if task is None:
                self._slots.release()
                break
            self._spawn(task)
            started += 1
        return started

    def _report_expired(self, now: Any = None) -> None:
        for entry in self.queue.drain_expired(now):
            task = entry.task
            deadline = entry.schedule.deadline
            body = {
                "ok": False,
                "decision": "skipped",
                "status": "deadline_exceeded",
                "error": f"deadline {deadline.isoformat() if deadline else '?'} passed before execution",
            }
            send_chunk = getattr(self.reporter, "send_chunk", None)
            if send_chunk is not None:
                send_chunk(task, body, 0, True)
            else:  # pragma: no cover
                self.reporter.send_error(task, body["error"])
            if self.metrics is not None:
                self.metrics.task_completed(str(task.id), "deadline_exceeded", getattr(task, "kind", None))

    def _spawn(self, task: Any) -> threading.Thread:
        task_id = str(task.id)

        def work() -> None:
            try:
                self.run_task(task)
            except Exception:  # pragma: no cover - a worker must never die silently
                log.exception(f"task {task_id}: worker crashed")
            finally:
                with self._lock:
                    self._workers.pop(task_id, None)
                    self._in_flight -= 1
                self._slots.release()

        thread = threading.Thread(target=work, name=f"task-{task_id}", daemon=True)
        with self._lock:
            self._workers[task_id] = thread
            self._in_flight += 1
            self._peak_concurrency = max(self._peak_concurrency, self._in_flight)
        thread.start()
        return thread

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until no worker is running. False on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                threads = list(self._workers.values())
            if not threads:
                return True
            for thread in threads:
                thread.join(timeout=0.05)
        return not self.running_task_ids()

    def drain(self, timeout: float = 30.0) -> bool:
        """Run the queue to empty, then wait for the workers. Used by --once."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if not self.queue.depth and not self.in_flight:
                return True
            time.sleep(0.02)
        return False


class _TargetStub:
    """Minimal task-shaped object for reporting about a task we no longer hold.

    A cancelled queue entry is gone by the time we write its result, and a
    legacy (non-AMP) shape is right here: we have no envelope to reply to.
    """

    __slots__ = ("id", "kind", "payload", "envelope")

    def __init__(self, task_id: str) -> None:
        self.id = task_id
        self.kind = CANCEL_KIND
        self.payload: dict[str, Any] = {}
        self.envelope = None


def _error_result(task_id: str, message: str) -> Any:
    return Result(task_id=task_id, ok=False, decision="error", error=message)

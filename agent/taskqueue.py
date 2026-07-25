"""
Priority queue with scheduling constraints.

v1/v2 executed whatever ``poller.fetch_pending_tasks()`` handed over, in
filename order. v3 puts a scheduler in between so a producer can express
*when* and *in what order* work should happen:

* ``priority``   — ``high`` | ``normal`` | ``low``; higher wins, FIFO within a band.
* ``not_before`` — ISO-8601 instant; the task is invisible until then.
* ``deadline``   — ISO-8601 instant; once past, the task is dropped as expired
  (a ``high`` task runs anyway — late beats never for something urgent).
* ``depends_on`` — task ids that must have completed successfully first.

All four are read from the task payload, i.e. the AMP ``payload.body``, so
``agent/amp.py`` needs no envelope changes. A packet carrying none of them
behaves exactly as it did in v2: plain FIFO within the ``normal`` band.

Named ``taskqueue`` and not ``queue``: ``agent/`` sits on ``sys.path`` in both
the script and installed layouts, so a ``queue.py`` here would shadow the
stdlib ``queue`` that ``executor_v2`` uses for its streaming pipes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger("iddo-harness.queue")

PRIORITIES = ("high", "normal", "low")
PRIORITY_RANK: dict[str, int] = {name: rank for rank, name in enumerate(PRIORITIES)}
DEFAULT_PRIORITY = "normal"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(raw: Any) -> datetime | None:
    """Parse an ISO-8601 instant, tolerating a trailing ``Z`` and naive input.

    Naive timestamps are read as UTC rather than local time: the producer is a
    cloud brick and the harness may sit in any timezone, so local would make
    the same packet mean different things on different machines. Unparseable
    input returns ``None`` — a malformed deadline must not wedge the queue.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        log.warning("unparseable timestamp %r — ignoring constraint", raw)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalise_priority(raw: Any) -> str:
    """Map arbitrary producer input onto one of the three known bands."""
    text = str(raw or "").strip().lower()
    if text in PRIORITY_RANK:
        return text
    if text in {"urgent", "critical", "p0"}:
        return "high"
    if text in {"background", "batch", "p3"}:
        return "low"
    if text:
        log.warning("unknown priority %r — treating as %s", raw, DEFAULT_PRIORITY)
    return DEFAULT_PRIORITY


@dataclass
class RetryPolicy:
    """How many times to re-run a failing task, and how long to wait between."""

    max_attempts: int = 1
    backoff_seconds: list[float] = field(default_factory=list)

    def delay_before(self, attempt: int) -> float:
        """Seconds to sleep before ``attempt`` (1-based; attempt 1 never waits).

        A backoff list shorter than ``max_attempts`` repeats its last entry, so
        ``[1, 5]`` with 4 attempts waits 1s, 5s, 5s.
        """
        if attempt <= 1 or not self.backoff_seconds:
            return 0.0
        index = min(attempt - 2, len(self.backoff_seconds) - 1)
        return float(self.backoff_seconds[index])


@dataclass
class Schedule:
    """Scheduling metadata extracted from a task payload."""

    priority: str = DEFAULT_PRIORITY
    not_before: datetime | None = None
    deadline: datetime | None = None
    depends_on: list[str] = field(default_factory=list)
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    @property
    def rank(self) -> int:
        return PRIORITY_RANK[self.priority]


def _as_id_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    log.warning("depends_on: expected a list, got %r — ignoring", raw)
    return []


def parse_schedule(
    task: Any,
    *,
    default_max_attempts: int = 1,
    default_backoff_seconds: Iterable[float] | None = None,
) -> Schedule:
    """Read scheduling metadata off a ``Task``.

    Looks in ``task.payload`` (the AMP ``payload.body``), optionally nested
    under a ``schedule`` key. ``task.priority`` — already populated by the
    poller for both AMP and legacy packets — wins over a payload ``priority``.
    """
    payload = getattr(task, "payload", None) or {}
    nested = payload.get("schedule") if isinstance(payload.get("schedule"), dict) else {}

    def pick(key: str) -> Any:
        if key in payload:
            return payload[key]
        return nested.get(key)

    priority_raw = getattr(task, "priority", None) or pick("priority")

    retry_raw = pick("retry")
    if not isinstance(retry_raw, dict):
        retry_raw = {}
    backoff_raw = retry_raw.get("backoff_seconds")
    if isinstance(backoff_raw, (list, tuple)):
        backoff = [float(item) for item in backoff_raw]
    else:
        backoff = [float(item) for item in (default_backoff_seconds or [])]
    try:
        max_attempts = max(1, int(retry_raw.get("max_attempts", default_max_attempts)))
    except (TypeError, ValueError):
        log.warning("retry.max_attempts %r is not an integer — using %d",
                    retry_raw.get("max_attempts"), default_max_attempts)
        max_attempts = max(1, default_max_attempts)

    return Schedule(
        priority=normalise_priority(priority_raw),
        not_before=parse_timestamp(pick("not_before")),
        deadline=parse_timestamp(pick("deadline")),
        depends_on=_as_id_list(pick("depends_on")),
        retry=RetryPolicy(max_attempts=max_attempts, backoff_seconds=backoff),
    )


@dataclass
class QueueEntry:
    """A queued task plus its schedule and arrival order."""

    task: Any
    schedule: Schedule
    seq: int

    @property
    def task_id(self) -> str:
        return str(getattr(self.task, "id", ""))

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.schedule.rank, self.seq)


class ResultIndex:
    """Answers "did task X already succeed?" by looking at the results dir.

    Reads the bridge's own output, so a dependency satisfied in an earlier
    polling cycle (or by a different harness sharing the bridge) still counts.
    """

    def __init__(self, result_dir: Path | str | None) -> None:
        self.result_dir = Path(result_dir) if result_dir else None

    def succeeded(self, task_id: str) -> bool:
        if not self.result_dir or not self.result_dir.is_dir():
            return False
        for path in self._candidates(task_id):
            if self._is_ok(path):
                return True
        return False

    def _candidates(self, task_id: str) -> list[Path]:
        assert self.result_dir is not None
        direct = self.result_dir / f"{task_id}.json"
        found = [direct] if direct.exists() else []
        # Chunked and retried results land under suffixed names.
        found.extend(sorted(self.result_dir.glob(f"{task_id}-chunk-*.json")))
        found.extend(sorted(self.result_dir.glob(f"{task_id}-attempt-*.json")))
        return found

    @staticmethod
    def _is_ok(path: Path) -> bool:
        import json

        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        # Unwrap an AMP envelope if present.
        body = doc
        if isinstance(doc.get("payload"), dict) and isinstance(doc["payload"].get("body"), dict):
            body = doc["payload"]["body"]
        if body.get("cancelled"):
            return False
        if "ok" in body:
            return bool(body["ok"])
        return str(body.get("status", "")).lower() == "ok"


class TaskQueue:
    """Ordered, constraint-aware holding pen for pending tasks.

    Not thread-safe by itself for mutation *ordering* guarantees, but every
    public method takes an internal lock, so the concurrent runner can call
    ``next_task()`` from several worker threads.
    """

    def __init__(
        self,
        *,
        result_index: ResultIndex | None = None,
        dependency_check: Callable[[str], bool] | None = None,
        clock: Callable[[], datetime] = _now,
        default_max_attempts: int = 1,
        default_backoff_seconds: Iterable[float] | None = None,
    ) -> None:
        import threading

        self._entries: list[QueueEntry] = []
        self._seq = count()
        self._lock = threading.RLock()
        self._clock = clock
        self._result_index = result_index
        self._dependency_check = dependency_check
        self._default_max_attempts = default_max_attempts
        self._default_backoff_seconds = list(default_backoff_seconds or [])
        self._known_ids: set[str] = set()
        self._completed: set[str] = set()

    # -- membership ---------------------------------------------------------

    def add(self, task: Any) -> QueueEntry | None:
        """Queue a task. Re-adding an id already queued is a no-op."""
        with self._lock:
            task_id = str(getattr(task, "id", ""))
            if task_id and task_id in self._known_ids:
                return None
            entry = QueueEntry(
                task=task,
                schedule=parse_schedule(
                    task,
                    default_max_attempts=self._default_max_attempts,
                    default_backoff_seconds=self._default_backoff_seconds,
                ),
                seq=next(self._seq),
            )
            self._entries.append(entry)
            if task_id:
                self._known_ids.add(task_id)
            return entry

    def extend(self, tasks: Iterable[Any]) -> list[QueueEntry]:
        return [entry for task in tasks if (entry := self.add(task)) is not None]

    def remove(self, task_id: str) -> Any | None:
        """Pull a task out without running it. Returns the task, or None."""
        with self._lock:
            for index, entry in enumerate(self._entries):
                if entry.task_id == task_id:
                    self._entries.pop(index)
                    self._known_ids.discard(task_id)
                    return entry.task
            return None

    def mark_completed(self, task_id: str, ok: bool = True) -> None:
        """Record an in-process completion so dependants unblock immediately.

        Without this, a dependant would have to wait for the result to be
        pushed and re-read through :class:`ResultIndex`.
        """
        with self._lock:
            self._known_ids.discard(task_id)
            if ok:
                self._completed.add(task_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def depth(self) -> int:
        return len(self)

    def snapshot(self) -> list[QueueEntry]:
        """Entries in scheduling order — for metrics and debugging."""
        with self._lock:
            return sorted(self._entries, key=lambda e: e.sort_key)

    # -- scheduling ---------------------------------------------------------

    def dependencies_met(self, schedule: Schedule) -> bool:
        for dep in schedule.depends_on:
            if dep in self._completed:
                continue
            if self._dependency_check is not None and self._dependency_check(dep):
                self._completed.add(dep)
                continue
            if self._result_index is not None and self._result_index.succeeded(dep):
                self._completed.add(dep)
                continue
            return False
        return True

    def is_expired(self, schedule: Schedule, now: datetime | None = None) -> bool:
        """True when the deadline has passed. ``high`` never expires."""
        if schedule.deadline is None or schedule.priority == "high":
            return False
        return (now or self._clock()) > schedule.deadline

    def blocked_reason(self, entry: QueueEntry, now: datetime | None = None) -> str | None:
        """Why this entry cannot run right now, or None if it can."""
        now = now or self._clock()
        schedule = entry.schedule
        if schedule.not_before is not None and now < schedule.not_before:
            return f"not_before {schedule.not_before.isoformat()}"
        if not self.dependencies_met(schedule):
            missing = [d for d in schedule.depends_on if d not in self._completed]
            return f"awaiting dependencies: {', '.join(missing)}"
        return None

    def drain_expired(self, now: datetime | None = None) -> list[QueueEntry]:
        """Remove and return every entry whose deadline has passed.

        Callers are expected to log a warning and write a ``deadline_exceeded``
        result for each, so the producer learns the task was never run.
        """
        now = now or self._clock()
        with self._lock:
            expired = [e for e in self._entries if self.is_expired(e.schedule, now)]
            for entry in expired:
                self._entries.remove(entry)
                self._known_ids.discard(entry.task_id)
            for entry in expired:
                log.warning(
                    "task %s skipped: deadline %s passed",
                    entry.task_id,
                    entry.schedule.deadline.isoformat() if entry.schedule.deadline else "?",
                )
            return expired

    def next_task(self, now: datetime | None = None) -> Any | None:
        """Pop and return the highest-priority eligible task, or ``None``.

        Expired tasks are dropped first, so they never win a slot. An entry
        blocked by ``not_before`` or ``depends_on`` is left in place and the
        scan moves on to the next candidate.
        """
        self.drain_expired(now)
        now = now or self._clock()
        with self._lock:
            for entry in sorted(self._entries, key=lambda e: e.sort_key):
                reason = self.blocked_reason(entry, now)
                if reason is None:
                    self._entries.remove(entry)
                    return entry.task
                log.debug("task %s not eligible: %s", entry.task_id, reason)
            return None

    def next_entry(self, now: datetime | None = None) -> QueueEntry | None:
        """Like :meth:`next_task` but keeps the schedule alongside the task."""
        self.drain_expired(now)
        now = now or self._clock()
        with self._lock:
            for entry in sorted(self._entries, key=lambda e: e.sort_key):
                if self.blocked_reason(entry, now) is None:
                    self._entries.remove(entry)
                    return entry
            return None

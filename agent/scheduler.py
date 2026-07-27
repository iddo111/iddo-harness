"""
Scheduler — tasks that fire on their own.

Everything else in the harness is reactive: a producer writes a packet, the
harness runs it. That leaves "every morning at 06:00, pull and run the tests"
as somebody else's problem — a cron entry on the host, invisible to the audit
trail and unable to use any harness kind.

``schedule_task`` puts recurring work inside the harness instead. A schedule is
a cron expression (or a fixed interval, or a one-shot timestamp) plus an
ordinary task packet, persisted to JSON so it survives a restart.

``croniter`` is an *optional* dependency. Without it a built-in five-field cron
parser takes over, covering ``*``, numbers, ``a-b`` ranges, ``a,b`` lists and
``*/n`` steps — which is the whole of what a schedule in this harness has ever
needed. The fallback exists so a bare install still schedules; croniter is
preferred because it handles the corners (day-of-month *or* day-of-week, month
names) that a compact parser gets subtly wrong.

Times are UTC. A naive timestamp is read as UTC rather than local, because the
producer is a cloud brick and the harness can be in any timezone — guessing
"local" would make the same schedule mean different things on two machines.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger("harness.scheduler")

try:  # optional
    from croniter import croniter as _croniter
except ImportError:  # pragma: no cover - exercised on bare installs
    _croniter = None  # type: ignore[assignment]

MAX_SCHEDULES = 200
MIN_INTERVAL_SECONDS = 1.0

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DOWS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


class ScheduleError(ValueError):
    """Raised for an unusable schedule: bad cron, no trigger, no task."""


def croniter_available() -> bool:
    """Whether the optional ``croniter`` package is importable."""
    return _croniter is not None


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp, treating a naive one as UTC."""
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ScheduleError(f"unparseable timestamp: {value!r}") from exc
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------
def _parse_cron_field(text: str, low: int, high: int, names: dict[str, int] | None = None) -> set[int]:
    """Expand one cron field into the set of values it matches."""
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip().lower()
        if not part:
            raise ScheduleError(f"empty cron field element in {text!r}")
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError(f"bad cron step: {step_text!r}")
            step = int(step_text)
            part = part or "*"

        if part == "*":
            start, end = low, high
        elif "-" in part[1:]:
            start_text, _, end_text = part.partition("-")
            start, end = _cron_number(start_text, names), _cron_number(end_text, names)
        else:
            start = end = _cron_number(part, names)

        if start > end:
            raise ScheduleError(f"inverted cron range: {part!r}")
        if start < low or end > high:
            raise ScheduleError(f"cron value out of range {low}-{high}: {part!r}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ScheduleError(f"cron field matches nothing: {text!r}")
    return values


def _cron_number(text: str, names: dict[str, int] | None) -> int:
    text = text.strip().lower()
    if names and text[:3] in names:
        return names[text[:3]]
    if not text.isdigit():
        raise ScheduleError(f"bad cron value: {text!r}")
    return int(text)


def validate_cron(expression: str) -> str:
    """Check a five-field cron expression, returning it normalised.

    Validated with the built-in parser even when croniter is installed: a
    schedule should be rejected identically on every install, not depend on
    which optional packages happen to be present.
    """
    text = " ".join(str(expression or "").split())
    fields = text.split(" ")
    if len(fields) != 5:
        raise ScheduleError(
            f"cron expression needs 5 fields (min hour dom month dow), got {len(fields)}: {expression!r}"
        )
    names = (None, None, None, _MONTHS, _DOWS)
    for field_text, (low, high), name_map in zip(fields, _FIELD_RANGES, names):
        _parse_cron_field(field_text, low, high, name_map)
    return text


def _cron_matches(expression: str, moment: datetime) -> bool:
    """Whether ``moment`` (minute resolution) satisfies the expression."""
    minute, hour, dom, month, dow = expression.split(" ")
    if moment.minute not in _parse_cron_field(minute, 0, 59):
        return False
    if moment.hour not in _parse_cron_field(hour, 0, 23):
        return False
    if moment.month not in _parse_cron_field(month, 1, 12, _MONTHS):
        return False

    # Cron's day rule: when both day fields are restricted, either may match.
    dom_set = _parse_cron_field(dom, 1, 31)
    dow_set = _parse_cron_field(dow, 0, 6, _DOWS)
    dow_set = {0 if d == 7 else d for d in dow_set}
    dom_restricted = dom.strip() != "*"
    dow_restricted = dow.strip() != "*"
    weekday = (moment.weekday() + 1) % 7  # Python Mon=0 → cron Sun=0

    if dom_restricted and dow_restricted:
        return moment.day in dom_set or weekday in dow_set
    if dom_restricted:
        return moment.day in dom_set
    if dow_restricted:
        return weekday in dow_set
    return True


def next_cron_time(expression: str, after: datetime) -> datetime:
    """First minute strictly after ``after`` that matches ``expression``."""
    if _croniter is not None:
        return _croniter(expression, after).get_next(datetime).astimezone(timezone.utc)

    moment = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Four years covers every Feb-29 schedule; beyond that the expression
    # matches nothing and saying so beats looping.
    limit = moment + timedelta(days=366 * 4)
    while moment <= limit:
        if _cron_matches(expression, moment):
            return moment
        moment += timedelta(minutes=1)
    raise ScheduleError(f"cron expression never fires: {expression!r}")


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------
@dataclass
class Schedule:
    """One recurring (or one-shot) task."""

    id: str
    task: dict[str, Any]
    cron: str | None = None
    interval_seconds: float | None = None
    at: str | None = None
    enabled: bool = True
    max_runs: int | None = None
    run_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_run_at: float | None = None
    next_run_at: float | None = None
    last_error: str = ""

    @property
    def exhausted(self) -> bool:
        """True when the schedule has fired as often as it ever will."""
        if self.at is not None and self.run_count >= 1:
            return True
        return self.max_runs is not None and self.run_count >= self.max_runs

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form, used both for persistence and for task results."""
        return {
            "id": self.id,
            "task": self.task,
            "cron": self.cron,
            "interval_seconds": self.interval_seconds,
            "at": self.at,
            "enabled": self.enabled,
            "max_runs": self.max_runs,
            "run_count": self.run_count,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "last_error": self.last_error,
            "exhausted": self.exhausted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Schedule":
        """Rebuild from :meth:`to_dict`, ignoring derived fields."""
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001 - dataclass API
        return cls(**{k: v for k, v in data.items() if k in known})


def parse_schedule(payload: dict[str, Any], *, now: datetime | None = None) -> Schedule:
    """Validate a ``schedule_task`` payload into a :class:`Schedule`.

    Exactly one trigger is required. Accepting two would mean silently picking
    one, and a schedule that fires at a time nobody asked for is worse than a
    rejected packet.
    """
    if not isinstance(payload, dict):
        raise ScheduleError("schedule payload must be an object")

    task = payload.get("task")
    if not isinstance(task, dict) or not str(task.get("kind") or "").strip():
        raise ScheduleError("schedule needs a 'task' object with a 'kind'")

    cron = payload.get("cron")
    interval = payload.get("interval_seconds")
    at = payload.get("at")
    triggers = [t for t in (cron, interval, at) if t not in (None, "")]
    if not triggers:
        raise ScheduleError("schedule needs one of: cron, interval_seconds, at")
    if len(triggers) > 1:
        raise ScheduleError("schedule accepts exactly one of: cron, interval_seconds, at")

    if cron:
        cron = validate_cron(str(cron))
    if interval is not None and interval != "":
        try:
            interval = float(interval)
        except (TypeError, ValueError) as exc:
            raise ScheduleError(f"interval_seconds must be a number, got {interval!r}") from exc
        if interval < MIN_INTERVAL_SECONDS:
            raise ScheduleError(f"interval_seconds must be >= {MIN_INTERVAL_SECONDS}")
    else:
        interval = None
    if at:
        at = parse_timestamp(at).isoformat()

    max_runs = payload.get("max_runs")
    if max_runs is not None:
        max_runs = int(max_runs)
        if max_runs < 1:
            raise ScheduleError("max_runs must be >= 1")

    schedule = Schedule(
        id=str(payload.get("id") or f"sched-{uuid.uuid4().hex[:10]}"),
        task=dict(task),
        cron=cron or None,
        interval_seconds=interval,
        at=at or None,
        enabled=bool(payload.get("enabled", True)),
        max_runs=max_runs,
    )
    schedule.next_run_at = compute_next_run(schedule, now=now).timestamp()
    return schedule


def compute_next_run(schedule: Schedule, *, now: datetime | None = None) -> datetime:
    """When this schedule should fire next, counting from ``now``."""
    moment = now or utcnow()
    if schedule.cron:
        return next_cron_time(schedule.cron, moment)
    if schedule.interval_seconds:
        return moment + timedelta(seconds=schedule.interval_seconds)
    if schedule.at:
        return parse_timestamp(schedule.at)
    raise ScheduleError(f"schedule {schedule.id} has no trigger")


class Scheduler:
    """Holds schedules, says which are due, and optionally fires them itself.

    ``dispatch`` is injected — the scheduler decides *when*, never *how*. Run
    it passively (call :meth:`due` from an existing polling loop) or actively
    (:meth:`start` spins a ticker thread); both use the same bookkeeping, so a
    test can drive time by hand without a thread in sight.
    """

    def __init__(
        self,
        dispatch: Callable[[Schedule], Any] | None = None,
        *,
        path: str | Path | None = None,
        tick_seconds: float = 5.0,
    ) -> None:
        self.dispatch = dispatch
        self.path = Path(path) if path is not None else None
        self.tick_seconds = max(0.05, float(tick_seconds))
        self._lock = threading.RLock()
        self._schedules: dict[str, Schedule] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        if self.path is not None and self.path.exists():
            self.load()

    # -- registry -----------------------------------------------------------

    def add(self, schedule: Schedule) -> Schedule:
        """Register a schedule, replacing any existing one with the same id."""
        with self._lock:
            if schedule.id not in self._schedules and len(self._schedules) >= MAX_SCHEDULES:
                raise ScheduleError(f"too many schedules (limit {MAX_SCHEDULES})")
            self._schedules[schedule.id] = schedule
        self.save()
        log.info("scheduled %s (cron=%s interval=%s at=%s)",
                 schedule.id, schedule.cron, schedule.interval_seconds, schedule.at)
        return schedule

    def remove(self, schedule_id: str) -> bool:
        """Delete a schedule. Returns whether it existed."""
        with self._lock:
            removed = self._schedules.pop(schedule_id, None) is not None
        if removed:
            self.save()
        return removed

    def get(self, schedule_id: str) -> Schedule | None:
        with self._lock:
            return self._schedules.get(schedule_id)

    def list(self, *, include_exhausted: bool = True) -> list[Schedule]:
        """Every schedule, soonest next-run first."""
        with self._lock:
            items = list(self._schedules.values())
        if not include_exhausted:
            items = [s for s in items if not s.exhausted]
        return sorted(items, key=lambda s: (s.next_run_at or float("inf"), s.id))

    def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        """Pause or resume a schedule without losing it."""
        with self._lock:
            schedule = self._schedules.get(schedule_id)
            if schedule is None:
                return False
            schedule.enabled = enabled
            if enabled and schedule.next_run_at is None:
                schedule.next_run_at = compute_next_run(schedule).timestamp()
        self.save()
        return True

    # -- firing -------------------------------------------------------------

    def due(self, *, now: datetime | None = None) -> list[Schedule]:
        """Schedules whose next run has arrived, soonest first."""
        moment = (now or utcnow()).timestamp()
        with self._lock:
            return sorted(
                (
                    s
                    for s in self._schedules.values()
                    if s.enabled and not s.exhausted and s.next_run_at is not None and s.next_run_at <= moment
                ),
                key=lambda s: s.next_run_at or 0.0,
            )

    def mark_fired(self, schedule: Schedule, *, now: datetime | None = None, error: str = "") -> Schedule:
        """Record a run and roll the schedule forward to its next slot.

        The next slot is computed from *now* rather than from the missed slot,
        so a harness that was asleep for six hours fires once on wake-up and
        then resumes its cadence — instead of replaying every slot it missed.
        """
        moment = now or utcnow()
        with self._lock:
            schedule.run_count += 1
            schedule.last_run_at = moment.timestamp()
            schedule.last_error = error
            if schedule.exhausted:
                schedule.next_run_at = None
                schedule.enabled = False
            else:
                try:
                    schedule.next_run_at = compute_next_run(schedule, now=moment).timestamp()
                except ScheduleError as exc:
                    schedule.next_run_at = None
                    schedule.enabled = False
                    schedule.last_error = str(exc)
        self.save()
        return schedule

    def run_due(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Fire every due schedule through ``dispatch`` and roll each forward."""
        fired: list[dict[str, Any]] = []
        for schedule in self.due(now=now):
            error = ""
            if self.dispatch is not None:
                try:
                    self.dispatch(schedule)
                except Exception as exc:  # one bad schedule must not stop the rest
                    log.exception("schedule %s dispatch failed", schedule.id)
                    error = f"{type(exc).__name__}: {exc}"
            self.mark_fired(schedule, now=now, error=error)
            fired.append({"id": schedule.id, "error": error, "next_run_at": schedule.next_run_at})
        return fired

    # -- background thread --------------------------------------------------

    def start(self) -> None:
        """Run :meth:`run_due` on a ticker thread until :meth:`stop`."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._tick_forever, name="scheduler", daemon=True)
        self._thread.start()

    def _tick_forever(self) -> None:
        while not self._stop.wait(self.tick_seconds):
            try:
                self.run_due()
            except Exception:  # pragma: no cover - defensive
                log.exception("scheduler tick failed")

    def stop(self) -> None:
        """Stop the ticker thread. Safe before :meth:`start`."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # -- persistence --------------------------------------------------------

    def save(self) -> None:
        """Write every schedule to ``path``. A no-op when no path was given."""
        if self.path is None:
            return
        with self._lock:
            data = [s.to_dict() for s in self._schedules.values()]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:  # persistence is a convenience, not a precondition
            log.exception("could not persist schedules to %s", self.path)

    def load(self) -> int:
        """Read schedules back from ``path``. Returns how many were restored."""
        if self.path is None or not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("could not read schedules from %s", self.path)
            return 0
        restored: dict[str, Schedule] = {}
        for item in data if isinstance(data, list) else []:
            try:
                schedule = Schedule.from_dict(item)
            except (TypeError, ValueError):
                log.warning("skipping unreadable schedule row: %r", item)
                continue
            # A schedule whose slot passed while the harness was down fires
            # once now, rather than being replayed for every slot it missed.
            if schedule.next_run_at is None and schedule.enabled and not schedule.exhausted:
                try:
                    schedule.next_run_at = compute_next_run(schedule).timestamp()
                except ScheduleError:
                    schedule.enabled = False
            restored[schedule.id] = schedule
        with self._lock:
            self._schedules = restored
        return len(restored)


def schedules_from(specs: Iterable[dict[str, Any]]) -> list[Schedule]:
    """Parse a list of schedule payloads, for config-file bootstrapping."""
    return [parse_schedule(spec) for spec in specs]

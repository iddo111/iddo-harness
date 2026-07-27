"""
Tests for the scheduler (agent/scheduler.py).

Time is passed in explicitly wherever it matters, so nothing here waits on a
wall clock. The built-in cron parser is exercised directly *and* through the
public helpers, since it is the path a bare install takes.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import scheduler as scheduler_mod
from scheduler import (
    MAX_SCHEDULES,
    MIN_INTERVAL_SECONDS,
    Schedule,
    ScheduleError,
    Scheduler,
    _cron_matches,
    _parse_cron_field,
    compute_next_run,
    croniter_available,
    next_cron_time,
    parse_schedule,
    parse_timestamp,
    schedules_from,
    utcnow,
    validate_cron,
)

TASK = {"kind": "shell", "payload": {"command": "echo hi"}}


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Cron field parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,low,high,expected",
    [
        ("*", 0, 3, {0, 1, 2, 3}),
        ("2", 0, 5, {2}),
        ("1-3", 0, 5, {1, 2, 3}),
        ("1,4", 0, 5, {1, 4}),
        ("*/2", 0, 5, {0, 2, 4}),
        ("1-5/2", 0, 5, {1, 3, 5}),
        ("0,2-4", 0, 5, {0, 2, 3, 4}),
    ],
)
def test_cron_field_expansion(text: str, low: int, high: int, expected: set[int]) -> None:
    assert _parse_cron_field(text, low, high) == expected


def test_cron_field_accepts_names() -> None:
    assert _parse_cron_field("mon-wed", 0, 6, {"sun": 0, "mon": 1, "tue": 2, "wed": 3}) == {1, 2, 3}


@pytest.mark.parametrize("text", ["", "x", "5-1", "9", "*/0", "1-/2"])
def test_bad_cron_field_is_rejected(text: str) -> None:
    with pytest.raises(ScheduleError):
        _parse_cron_field(text, 0, 5)


# ---------------------------------------------------------------------------
# validate_cron
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "expression",
    ["* * * * *", "0 6 * * *", "*/15 * * * 1-5", "0 0 1 jan *", "30 2 * * sun"],
)
def test_valid_cron_expressions(expression: str) -> None:
    assert validate_cron(expression) == " ".join(expression.split())


def test_cron_whitespace_is_normalised() -> None:
    assert validate_cron("  0   6  *  * * ") == "0 6 * * *"


@pytest.mark.parametrize("expression", ["", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *"])
def test_invalid_cron_expressions_are_rejected(expression: str) -> None:
    with pytest.raises(ScheduleError):
        validate_cron(expression)


def test_validation_does_not_depend_on_croniter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schedule must be accepted or rejected identically on every install."""
    monkeypatch.setattr(scheduler_mod, "_croniter", None)
    assert validate_cron("0 6 * * *") == "0 6 * * *"
    with pytest.raises(ScheduleError):
        validate_cron("99 * * * *")


# ---------------------------------------------------------------------------
# Cron matching / next time (built-in parser)
# ---------------------------------------------------------------------------
@pytest.fixture()
def no_croniter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the built-in parser, which is what a bare install uses."""
    monkeypatch.setattr(scheduler_mod, "_croniter", None)


def test_cron_matches_minute_and_hour() -> None:
    assert _cron_matches("30 6 * * *", at(2026, 3, 4, 6, 30)) is True
    assert _cron_matches("30 6 * * *", at(2026, 3, 4, 6, 31)) is False


def test_cron_day_fields_are_an_or_when_both_restricted() -> None:
    """Cron's oddest rule: dom OR dow, not dom AND dow."""
    expr = "0 0 15 * sun"
    assert _cron_matches(expr, at(2026, 3, 15)) is True  # the 15th
    assert _cron_matches(expr, at(2026, 3, 8)) is True  # a Sunday
    assert _cron_matches(expr, at(2026, 3, 10)) is False  # neither


def test_cron_weekday_mapping_is_sunday_zero() -> None:
    assert _cron_matches("0 0 * * 0", at(2026, 3, 8)) is True  # Sunday
    assert _cron_matches("0 0 * * 1", at(2026, 3, 9)) is True  # Monday


def test_next_cron_time_with_the_builtin_parser(no_croniter: None) -> None:
    assert next_cron_time("0 6 * * *", at(2026, 3, 4, 5, 0)) == at(2026, 3, 4, 6, 0)


def test_next_cron_time_rolls_to_tomorrow(no_croniter: None) -> None:
    assert next_cron_time("0 6 * * *", at(2026, 3, 4, 7, 0)) == at(2026, 3, 5, 6, 0)


def test_next_cron_time_is_strictly_after(no_croniter: None) -> None:
    """Standing still on a matching minute would fire the same slot twice."""
    assert next_cron_time("* * * * *", at(2026, 3, 4, 6, 0)) == at(2026, 3, 4, 6, 1)


def test_builtin_and_croniter_agree_when_both_available() -> None:
    if not croniter_available():
        pytest.skip("croniter is not installed")
    moment = at(2026, 3, 4, 5, 0)
    with_croniter = next_cron_time("0 6 * * *", moment)
    assert with_croniter == at(2026, 3, 4, 6, 0)


def test_an_impossible_cron_expression_reports_rather_than_loops(no_croniter: None) -> None:
    # Feb 30 never happens.
    with pytest.raises(ScheduleError, match="never fires"):
        next_cron_time("0 0 30 2 *", at(2026, 1, 1))


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
def test_naive_timestamps_are_read_as_utc() -> None:
    """The producer is a cloud brick; guessing 'local' would mean two things."""
    assert parse_timestamp("2026-03-04T06:00:00") == at(2026, 3, 4, 6)


def test_z_suffix_is_understood() -> None:
    assert parse_timestamp("2026-03-04T06:00:00Z") == at(2026, 3, 4, 6)


def test_offsets_are_converted_to_utc() -> None:
    assert parse_timestamp("2026-03-04T08:00:00+02:00") == at(2026, 3, 4, 6)


def test_a_datetime_passes_through() -> None:
    assert parse_timestamp(at(2026, 3, 4, 6)) == at(2026, 3, 4, 6)


def test_unparseable_timestamp_is_rejected() -> None:
    with pytest.raises(ScheduleError, match="unparseable"):
        parse_timestamp("next tuesday")


def test_utcnow_is_timezone_aware() -> None:
    assert utcnow().tzinfo is not None


# ---------------------------------------------------------------------------
# parse_schedule
# ---------------------------------------------------------------------------
def test_cron_schedule_parses() -> None:
    schedule = parse_schedule({"task": TASK, "cron": "0 6 * * *"}, now=at(2026, 3, 4, 5))
    assert schedule.cron == "0 6 * * *"
    assert schedule.next_run_at == at(2026, 3, 4, 6).timestamp()
    assert schedule.id.startswith("sched-")


def test_interval_schedule_parses() -> None:
    schedule = parse_schedule({"task": TASK, "interval_seconds": 60}, now=at(2026, 3, 4, 5))
    assert schedule.interval_seconds == 60.0
    assert schedule.next_run_at == at(2026, 3, 4, 5, 1).timestamp()


def test_one_shot_schedule_parses() -> None:
    schedule = parse_schedule({"task": TASK, "at": "2026-03-04T06:00:00"})
    assert schedule.next_run_at == at(2026, 3, 4, 6).timestamp()


def test_explicit_id_is_kept() -> None:
    assert parse_schedule({"id": "nightly", "task": TASK, "cron": "0 0 * * *"}).id == "nightly"


def test_exactly_one_trigger_is_required() -> None:
    with pytest.raises(ScheduleError, match="needs one of"):
        parse_schedule({"task": TASK})


def test_two_triggers_are_refused_rather_than_silently_picked() -> None:
    with pytest.raises(ScheduleError, match="exactly one of"):
        parse_schedule({"task": TASK, "cron": "* * * * *", "interval_seconds": 60})


@pytest.mark.parametrize("task", [None, {}, {"payload": {}}, "shell"])
def test_a_schedule_needs_a_real_task(task: Any) -> None:
    with pytest.raises(ScheduleError, match="'task' object"):
        parse_schedule({"task": task, "cron": "* * * * *"})


def test_non_object_payload_is_rejected() -> None:
    with pytest.raises(ScheduleError, match="must be an object"):
        parse_schedule([])  # type: ignore[arg-type]


def test_sub_minimum_interval_is_rejected() -> None:
    with pytest.raises(ScheduleError, match=f">= {MIN_INTERVAL_SECONDS}"):
        parse_schedule({"task": TASK, "interval_seconds": 0.1})


def test_non_numeric_interval_is_rejected() -> None:
    with pytest.raises(ScheduleError, match="must be a number"):
        parse_schedule({"task": TASK, "interval_seconds": "soon"})


def test_max_runs_below_one_is_rejected() -> None:
    with pytest.raises(ScheduleError, match="max_runs must be >= 1"):
        parse_schedule({"task": TASK, "cron": "* * * * *", "max_runs": 0})


def test_schedules_from_parses_a_list() -> None:
    assert len(schedules_from([{"task": TASK, "cron": "* * * * *"}] * 2)) == 2


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------
def test_a_one_shot_is_exhausted_after_one_run() -> None:
    schedule = Schedule(id="s", task=TASK, at="2026-03-04T06:00:00")
    assert schedule.exhausted is False
    schedule.run_count = 1
    assert schedule.exhausted is True


def test_max_runs_exhausts_a_recurring_schedule() -> None:
    schedule = Schedule(id="s", task=TASK, cron="* * * * *", max_runs=2, run_count=2)
    assert schedule.exhausted is True


def test_a_plain_cron_schedule_never_exhausts() -> None:
    assert Schedule(id="s", task=TASK, cron="* * * * *", run_count=999).exhausted is False


def test_round_trips_through_a_dict() -> None:
    original = parse_schedule({"task": TASK, "cron": "0 6 * * *"})
    rebuilt = Schedule.from_dict(original.to_dict())
    assert rebuilt.id == original.id
    assert rebuilt.cron == original.cron
    assert rebuilt.next_run_at == original.next_run_at


def test_from_dict_ignores_derived_fields() -> None:
    """to_dict() emits 'exhausted', which is a property and not a field."""
    Schedule.from_dict({"id": "s", "task": TASK, "cron": "* * * * *", "exhausted": True})


def test_compute_next_run_needs_a_trigger() -> None:
    with pytest.raises(ScheduleError, match="no trigger"):
        compute_next_run(Schedule(id="s", task=TASK))


# ---------------------------------------------------------------------------
# Scheduler registry
# ---------------------------------------------------------------------------
def test_add_get_remove() -> None:
    sched = Scheduler()
    schedule = sched.add(parse_schedule({"id": "s", "task": TASK, "cron": "* * * * *"}))
    assert sched.get("s") is schedule
    assert sched.remove("s") is True
    assert sched.remove("s") is False
    assert sched.get("s") is None


def test_add_replaces_by_id() -> None:
    sched = Scheduler()
    sched.add(parse_schedule({"id": "s", "task": TASK, "cron": "0 1 * * *"}))
    sched.add(parse_schedule({"id": "s", "task": TASK, "cron": "0 2 * * *"}))
    assert len(sched.list()) == 1
    assert sched.get("s").cron == "0 2 * * *"


def test_list_is_soonest_first() -> None:
    sched = Scheduler()
    sched.add(parse_schedule({"id": "late", "task": TASK, "interval_seconds": 600}))
    sched.add(parse_schedule({"id": "soon", "task": TASK, "interval_seconds": 10}))
    assert [s.id for s in sched.list()] == ["soon", "late"]


def test_list_can_hide_exhausted_schedules() -> None:
    sched = Scheduler()
    schedule = sched.add(parse_schedule({"id": "s", "task": TASK, "cron": "* * * * *", "max_runs": 1}))
    schedule.run_count = 1
    assert sched.list(include_exhausted=False) == []
    assert len(sched.list()) == 1


def test_the_schedule_limit_is_enforced() -> None:
    sched = Scheduler()
    for i in range(MAX_SCHEDULES):
        sched.add(parse_schedule({"id": f"s{i}", "task": TASK, "cron": "* * * * *"}))
    with pytest.raises(ScheduleError, match="too many schedules"):
        sched.add(parse_schedule({"id": "one-more", "task": TASK, "cron": "* * * * *"}))


def test_set_enabled_pauses_without_losing_the_schedule() -> None:
    sched = Scheduler()
    sched.add(parse_schedule({"id": "s", "task": TASK, "cron": "* * * * *"}))
    assert sched.set_enabled("s", False) is True
    assert sched.get("s").enabled is False
    assert sched.set_enabled("ghost", False) is False


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------
def test_due_returns_only_arrived_schedules() -> None:
    sched = Scheduler()
    now = at(2026, 3, 4, 6)
    sched.add(parse_schedule({"id": "ready", "task": TASK, "at": "2026-03-04T05:00:00"}))
    sched.add(parse_schedule({"id": "later", "task": TASK, "at": "2026-03-04T07:00:00"}))
    assert [s.id for s in sched.due(now=now)] == ["ready"]


def test_disabled_schedules_are_never_due() -> None:
    sched = Scheduler()
    sched.add(parse_schedule({"id": "s", "task": TASK, "at": "2026-03-04T05:00:00", "enabled": False}))
    assert sched.due(now=at(2026, 3, 4, 6)) == []


def test_run_due_dispatches_and_rolls_forward() -> None:
    fired: list[str] = []
    sched = Scheduler(dispatch=lambda s: fired.append(s.id))
    sched.add(parse_schedule({"id": "s", "task": TASK, "interval_seconds": 3600},
                             now=at(2026, 3, 4, 5)))
    now = at(2026, 3, 4, 7)
    assert [f["id"] for f in sched.run_due(now=now)] == ["s"]
    assert fired == ["s"]
    assert sched.get("s").next_run_at == at(2026, 3, 4, 8).timestamp()
    assert sched.get("s").run_count == 1


def test_a_missed_slot_fires_once_not_once_per_slot() -> None:
    """The next slot counts from now, so a six-hour outage is not replayed."""
    sched = Scheduler(dispatch=lambda s: None)
    sched.add(parse_schedule({"id": "s", "task": TASK, "interval_seconds": 60},
                             now=at(2026, 3, 4, 0)))
    wake = at(2026, 3, 4, 6)
    assert len(sched.run_due(now=wake)) == 1
    assert sched.due(now=wake) == []
    assert sched.get("s").next_run_at == at(2026, 3, 4, 6, 1).timestamp()


def test_an_exhausted_schedule_disables_itself() -> None:
    sched = Scheduler(dispatch=lambda s: None)
    sched.add(parse_schedule({"id": "s", "task": TASK, "at": "2026-03-04T05:00:00"}))
    sched.run_due(now=at(2026, 3, 4, 6))
    schedule = sched.get("s")
    assert schedule.enabled is False
    assert schedule.next_run_at is None


def test_a_failing_dispatch_is_recorded_not_raised() -> None:
    def boom(schedule: Schedule) -> None:
        raise RuntimeError("kaboom")

    sched = Scheduler(dispatch=boom)
    sched.add(parse_schedule({"id": "s", "task": TASK, "interval_seconds": 60},
                             now=at(2026, 3, 4, 5)))
    fired = sched.run_due(now=at(2026, 3, 4, 6))
    assert "RuntimeError: kaboom" in fired[0]["error"]
    assert sched.get("s").enabled is True  # one bad run does not kill the schedule


def test_run_due_without_a_dispatch_still_rolls_forward() -> None:
    sched = Scheduler()
    sched.add(parse_schedule({"id": "s", "task": TASK, "interval_seconds": 60},
                             now=at(2026, 3, 4, 5)))
    sched.run_due(now=at(2026, 3, 4, 6))
    assert sched.get("s").run_count == 1


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_schedules_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    first = Scheduler(path=path)
    first.add(parse_schedule({"id": "nightly", "task": TASK, "cron": "0 6 * * *"}))

    second = Scheduler(path=path)
    assert [s.id for s in second.list()] == ["nightly"]
    assert second.get("nightly").cron == "0 6 * * *"


def test_saved_file_is_readable_json(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    sched = Scheduler(path=path)
    sched.add(parse_schedule({"id": "s", "task": TASK, "cron": "* * * * *"}))
    assert json.loads(path.read_text())[0]["id"] == "s"


def test_removal_is_persisted(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    sched = Scheduler(path=path)
    sched.add(parse_schedule({"id": "s", "task": TASK, "cron": "* * * * *"}))
    sched.remove("s")
    assert Scheduler(path=path).list() == []


def test_a_corrupt_file_does_not_crash_startup(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text("{ not json")
    assert Scheduler(path=path).list() == []


def test_unreadable_rows_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text(json.dumps([{"nonsense": True}, {"id": "ok", "task": TASK, "cron": "* * * * *"}]))
    assert [s.id for s in Scheduler(path=path).list()] == ["ok"]


def test_save_is_a_no_op_without_a_path() -> None:
    Scheduler().save()  # must not raise


def test_load_is_a_no_op_without_a_path() -> None:
    assert Scheduler().load() == 0


# ---------------------------------------------------------------------------
# Ticker thread
# ---------------------------------------------------------------------------
def test_the_ticker_fires_due_schedules() -> None:
    fired: list[str] = []
    sched = Scheduler(dispatch=lambda s: fired.append(s.id), tick_seconds=0.05)
    sched.add(parse_schedule({"id": "s", "task": TASK, "interval_seconds": 1},
                             now=utcnow() - timedelta(seconds=10)))
    sched.start()
    sched.start()  # idempotent
    deadline = time.time() + 3
    while not fired and time.time() < deadline:
        time.sleep(0.02)
    sched.stop()
    assert fired == ["s"]


def test_stop_before_start_is_safe() -> None:
    Scheduler().stop()

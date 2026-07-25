"""
Tests for agent/taskqueue.py — priority ordering and scheduling constraints.

The queue is the piece that decides *what runs next*, so the cases that matter
are the ones where a naive FIFO would get it wrong: a high-priority packet
arriving last, a dependency that has not landed yet, a deadline that passed
while the agent was asleep.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from tests.helpers import FakeTask

from taskqueue import (
    DEFAULT_PRIORITY,
    ResultIndex,
    RetryPolicy,
    TaskQueue,
    normalise_priority,
    parse_schedule,
    parse_timestamp,
)

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


def at(**delta) -> str:
    return (NOW + timedelta(**delta)).isoformat()


@pytest.fixture
def queue() -> TaskQueue:
    return TaskQueue(clock=lambda: NOW)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_parse_timestamp_accepts_trailing_z():
    assert parse_timestamp("2026-07-25T12:00:00Z") == NOW


def test_parse_timestamp_treats_naive_input_as_utc():
    """A naive stamp must not mean different instants on differently-zoned hosts."""
    assert parse_timestamp("2026-07-25T12:00:00") == NOW


def test_parse_timestamp_returns_none_for_garbage():
    assert parse_timestamp("last tuesday") is None


def test_parse_timestamp_passes_through_none_and_empty():
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None


@pytest.mark.parametrize(
    "raw,expected",
    [("high", "high"), ("LOW", "low"), ("urgent", "high"), ("batch", "low"),
     ("", DEFAULT_PRIORITY), (None, DEFAULT_PRIORITY), ("nonsense", DEFAULT_PRIORITY)],
)
def test_normalise_priority(raw, expected):
    assert normalise_priority(raw) == expected


def test_parse_schedule_reads_from_payload_body():
    task = FakeTask(
        id="t1",
        payload={
            "not_before": at(minutes=5),
            "deadline": at(hours=1),
            "depends_on": ["a", "b"],
        },
    )
    schedule = parse_schedule(task)
    assert schedule.not_before == NOW + timedelta(minutes=5)
    assert schedule.deadline == NOW + timedelta(hours=1)
    assert schedule.depends_on == ["a", "b"]


def test_parse_schedule_accepts_a_nested_schedule_block():
    task = FakeTask(id="t1", payload={"schedule": {"depends_on": "only-one"}})
    assert parse_schedule(task).depends_on == ["only-one"]


def test_parse_schedule_task_priority_wins_over_payload():
    """The poller already resolved priority for both AMP and legacy packets."""
    task = FakeTask(id="t1", priority="high", payload={"priority": "low"})
    assert parse_schedule(task).priority == "high"


def test_parse_schedule_ignores_a_malformed_depends_on():
    task = FakeTask(id="t1", payload={"depends_on": {"not": "a list"}})
    assert parse_schedule(task).depends_on == []


def test_parse_schedule_falls_back_to_config_retry_defaults():
    schedule = parse_schedule(
        FakeTask(id="t1"), default_max_attempts=4, default_backoff_seconds=[2, 4]
    )
    assert schedule.retry.max_attempts == 4
    assert schedule.retry.backoff_seconds == [2.0, 4.0]


def test_parse_schedule_packet_retry_overrides_the_default():
    task = FakeTask(id="t1", payload={"retry": {"max_attempts": 3, "backoff_seconds": [1, 5, 15]}})
    retry = parse_schedule(task, default_max_attempts=1).retry
    assert retry.max_attempts == 3
    assert retry.backoff_seconds == [1.0, 5.0, 15.0]


def test_retry_policy_backoff_repeats_its_last_entry():
    """A short backoff list must not IndexError on a long retry budget."""
    policy = RetryPolicy(max_attempts=4, backoff_seconds=[1, 5])
    assert [policy.delay_before(n) for n in (1, 2, 3, 4)] == [0.0, 1.0, 5.0, 5.0]


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------
def test_plain_packets_keep_fifo_order(queue):
    """v2 behaviour: nothing declared, nothing reordered."""
    for name in ("a", "b", "c"):
        queue.add(FakeTask(id=name))
    assert [queue.next_task().id for _ in range(3)] == ["a", "b", "c"]


def test_high_priority_jumps_the_line(queue):
    queue.add(FakeTask(id="normal"))
    queue.add(FakeTask(id="urgent", priority="high"))
    assert queue.next_task().id == "urgent"


def test_low_priority_sinks_below_normal(queue):
    queue.add(FakeTask(id="later", priority="low"))
    queue.add(FakeTask(id="sooner"))
    assert [queue.next_task().id for _ in range(2)] == ["sooner", "later"]


def test_fifo_is_preserved_within_a_priority_band(queue):
    queue.add(FakeTask(id="h1", priority="high"))
    queue.add(FakeTask(id="h2", priority="high"))
    assert [queue.next_task().id for _ in range(2)] == ["h1", "h2"]


def test_empty_queue_returns_none(queue):
    assert queue.next_task() is None


def test_adding_the_same_id_twice_is_a_noop(queue):
    assert queue.add(FakeTask(id="dup")) is not None
    assert queue.add(FakeTask(id="dup")) is None
    assert queue.depth == 1


def test_remove_pulls_a_task_out_without_running_it(queue):
    queue.add(FakeTask(id="doomed"))
    assert queue.remove("doomed").id == "doomed"
    assert queue.next_task() is None


def test_remove_returns_none_for_an_unknown_id(queue):
    assert queue.remove("never-existed") is None


# ---------------------------------------------------------------------------
# not_before
# ---------------------------------------------------------------------------
def test_not_before_hides_a_task_until_its_time(queue):
    queue.add(FakeTask(id="future", payload={"not_before": at(minutes=10)}))
    assert queue.next_task() is None
    assert queue.depth == 1, "a deferred task stays queued, it is not dropped"


def test_not_before_in_the_past_runs_immediately(queue):
    queue.add(FakeTask(id="ready", payload={"not_before": at(minutes=-10)}))
    assert queue.next_task().id == "ready"


def test_a_deferred_task_does_not_block_the_one_behind_it(queue):
    queue.add(FakeTask(id="deferred", priority="high", payload={"not_before": at(hours=1)}))
    queue.add(FakeTask(id="runnable"))
    assert queue.next_task().id == "runnable"


# ---------------------------------------------------------------------------
# deadline
# ---------------------------------------------------------------------------
def test_an_expired_task_is_dropped(queue):
    queue.add(FakeTask(id="stale", payload={"deadline": at(minutes=-1)}))
    assert queue.next_task() is None
    assert queue.depth == 0, "an expired task is removed, not left to retry forever"


def test_a_high_priority_task_outlives_its_deadline(queue):
    """Late beats never for something the producer marked urgent."""
    queue.add(FakeTask(id="urgent", priority="high", payload={"deadline": at(minutes=-1)}))
    assert queue.next_task().id == "urgent"


def test_drain_expired_reports_what_it_dropped(queue):
    queue.add(FakeTask(id="stale", payload={"deadline": at(seconds=-1)}))
    queue.add(FakeTask(id="fresh", payload={"deadline": at(hours=1)}))
    assert [e.task_id for e in queue.drain_expired()] == ["stale"]
    assert queue.depth == 1


# ---------------------------------------------------------------------------
# depends_on
# ---------------------------------------------------------------------------
def test_a_task_waits_for_its_dependency(queue):
    queue.add(FakeTask(id="child", payload={"depends_on": ["parent"]}))
    assert queue.next_task() is None


def test_mark_completed_unblocks_a_dependant(queue):
    queue.add(FakeTask(id="child", payload={"depends_on": ["parent"]}))
    queue.mark_completed("parent", ok=True)
    assert queue.next_task().id == "child"


def test_a_failed_dependency_does_not_unblock_a_dependant(queue):
    queue.add(FakeTask(id="child", payload={"depends_on": ["parent"]}))
    queue.mark_completed("parent", ok=False)
    assert queue.next_task() is None


def test_all_dependencies_must_land_not_just_one(queue):
    queue.add(FakeTask(id="child", payload={"depends_on": ["a", "b"]}))
    queue.mark_completed("a")
    assert queue.next_task() is None
    queue.mark_completed("b")
    assert queue.next_task().id == "child"


def test_dependencies_are_satisfied_from_the_results_directory(tmp_path):
    """A dependency met in an earlier polling cycle still counts."""
    (tmp_path / "parent.json").write_text('{"ok": true}', encoding="utf-8")
    queue = TaskQueue(clock=lambda: NOW, result_index=ResultIndex(tmp_path))
    queue.add(FakeTask(id="child", payload={"depends_on": ["parent"]}))
    assert queue.next_task().id == "child"


def test_a_failed_result_on_disk_does_not_satisfy_a_dependency(tmp_path):
    (tmp_path / "parent.json").write_text('{"ok": false}', encoding="utf-8")
    queue = TaskQueue(clock=lambda: NOW, result_index=ResultIndex(tmp_path))
    queue.add(FakeTask(id="child", payload={"depends_on": ["parent"]}))
    assert queue.next_task() is None


def test_result_index_unwraps_an_amp_envelope(tmp_path):
    (tmp_path / "parent-chunk-0.json").write_text(
        '{"v": 1, "payload": {"type": "harness_result", "body": {"ok": true}}}', encoding="utf-8"
    )
    assert ResultIndex(tmp_path).succeeded("parent") is True


def test_result_index_rejects_a_cancelled_result(tmp_path):
    (tmp_path / "parent.json").write_text('{"ok": true, "cancelled": true}', encoding="utf-8")
    assert ResultIndex(tmp_path).succeeded("parent") is False


def test_result_index_tolerates_corrupt_json(tmp_path):
    (tmp_path / "parent.json").write_text("{not json", encoding="utf-8")
    assert ResultIndex(tmp_path).succeeded("parent") is False


def test_result_index_without_a_directory_never_claims_success():
    assert ResultIndex(None).succeeded("anything") is False


def test_blocked_reason_names_the_missing_dependency(queue):
    entry = queue.add(FakeTask(id="child", payload={"depends_on": ["parent"]}))
    assert "parent" in queue.blocked_reason(entry)


def test_snapshot_is_in_scheduling_order(queue):
    queue.add(FakeTask(id="low", priority="low"))
    queue.add(FakeTask(id="high", priority="high"))
    assert [e.task_id for e in queue.snapshot()] == ["high", "low"]

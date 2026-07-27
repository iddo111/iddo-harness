"""
Tests for sub-tasks (agent/subtasks.py).

The manager is driven with stub ``execute`` callables rather than a real
executor: this module owns scheduling, and testing it against a live shell
would only prove that ``echo`` works.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from subtasks import (
    DEFAULT_MAX_ACTIVE,
    FINISHED_STATES,
    SubTask,
    SubtaskError,
    SubtaskManager,
    SubtaskRecord,
    _result_summary,
    build_subtask,
)


@dataclass
class FakeResult:
    ok: bool = True
    decision: str = "auto"
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = 0
    error: str = ""


@dataclass
class FakeParent:
    id: str = "parent-1"
    envelope: Any = None
    payload: dict = field(default_factory=dict)


@pytest.fixture()
def manager() -> SubtaskManager:
    m = SubtaskManager(lambda task: FakeResult(stdout=task.id))
    yield m
    m.shutdown()


# ---------------------------------------------------------------------------
# build_subtask
# ---------------------------------------------------------------------------
def test_build_subtask_derives_a_legible_id_from_the_parent() -> None:
    task = build_subtask({"kind": "shell"}, parent=FakeParent(id="build-42"), index=3)
    assert task.id == "build-42-sub-3"
    assert task.parent_id == "build-42"


def test_build_subtask_generates_an_id_without_a_parent() -> None:
    task = build_subtask({"kind": "shell"})
    assert task.id.startswith("sub-")
    assert task.parent_id is None


def test_explicit_id_wins() -> None:
    assert build_subtask({"id": "mine", "kind": "shell"}, parent=FakeParent()).id == "mine"


def test_flattened_payload_form_is_accepted() -> None:
    task = build_subtask({"kind": "shell", "command": "ls", "timeout_sec": 5})
    assert task.payload == {"command": "ls", "timeout_sec": 5}


def test_explicit_payload_wins_over_flattening() -> None:
    task = build_subtask({"kind": "shell", "payload": {"command": "ls"}, "stray": 1})
    assert task.payload == {"command": "ls"}


def test_child_inherits_the_parent_envelope() -> None:
    """A result has to know its way home, so the envelope rides along."""
    envelope = object()
    task = build_subtask({"kind": "shell"}, parent=FakeParent(envelope=envelope))
    assert task.envelope is envelope


@pytest.mark.parametrize("spec", [{"kind": ""}, {}, {"payload": {}}])
def test_spec_without_a_kind_is_rejected(spec: dict) -> None:
    with pytest.raises(SubtaskError, match="kind"):
        build_subtask(spec)


def test_non_object_spec_is_rejected() -> None:
    with pytest.raises(SubtaskError, match="must be an object"):
        build_subtask("shell")  # type: ignore[arg-type]


def test_non_object_payload_is_rejected() -> None:
    with pytest.raises(SubtaskError, match="payload must be an object"):
        build_subtask({"kind": "shell", "payload": ["ls"]})


# ---------------------------------------------------------------------------
# spawning
# ---------------------------------------------------------------------------
def test_spawn_returns_immediately_and_finishes_later(manager: SubtaskManager) -> None:
    record = manager.spawn(build_subtask({"id": "c1", "kind": "shell"}))
    assert isinstance(record, SubtaskRecord)
    assert manager.await_tasks(["c1"], timeout=5)["ok"] is True
    assert manager.status("c1").state == "succeeded"


def test_failed_child_is_recorded_as_failed() -> None:
    manager = SubtaskManager(lambda task: FakeResult(ok=False, error="boom"))
    manager.spawn(build_subtask({"id": "c1", "kind": "shell"}))
    manager.await_tasks(["c1"], timeout=5)
    assert manager.status("c1").state == "failed"
    manager.shutdown()


def test_a_raising_child_does_not_kill_the_manager() -> None:
    def blow_up(task: Any) -> Any:
        raise RuntimeError("kaboom")

    manager = SubtaskManager(blow_up)
    manager.spawn(build_subtask({"id": "c1", "kind": "shell"}))
    manager.await_tasks(["c1"], timeout=5)
    record = manager.status("c1")
    assert record.state == "failed"
    assert "RuntimeError: kaboom" in record.error
    manager.shutdown()


def test_spawn_many_returns_one_record_per_child(manager: SubtaskManager) -> None:
    tasks = [build_subtask({"kind": "shell"}, parent=FakeParent(), index=i) for i in range(3)]
    records = manager.spawn_many(tasks)
    assert len(records) == 3
    assert manager.await_tasks([r.id for r in records], timeout=5)["ok"] is True


def test_depth_beyond_the_limit_is_refused() -> None:
    manager = SubtaskManager(lambda t: FakeResult(), max_depth=2)
    deep = build_subtask({"id": "deep", "kind": "shell"}, depth=3)
    with pytest.raises(SubtaskError, match="max_depth"):
        manager.spawn(deep)
    manager.shutdown()


def test_too_many_in_flight_is_refused() -> None:
    gate = threading.Event()
    manager = SubtaskManager(lambda t: gate.wait(5) or FakeResult(), max_active=2)
    manager.spawn(build_subtask({"id": "a", "kind": "shell"}))
    manager.spawn(build_subtask({"id": "b", "kind": "shell"}))
    with pytest.raises(SubtaskError, match="max_active"):
        manager.spawn(build_subtask({"id": "c", "kind": "shell"}))
    gate.set()
    manager.shutdown()


def test_duplicate_id_while_in_flight_is_refused() -> None:
    gate = threading.Event()
    manager = SubtaskManager(lambda t: gate.wait(5) or FakeResult())
    manager.spawn(build_subtask({"id": "dup", "kind": "shell"}))
    with pytest.raises(SubtaskError, match="already in flight"):
        manager.spawn(build_subtask({"id": "dup", "kind": "shell"}))
    gate.set()
    manager.shutdown()


def test_children_actually_run_in_parallel() -> None:
    seen = threading.Barrier(3, timeout=5)

    def wait_for_siblings(task: Any) -> Any:
        seen.wait()
        return FakeResult()

    manager = SubtaskManager(wait_for_siblings, max_active=3)
    ids = [f"p{i}" for i in range(3)]
    manager.spawn_many([build_subtask({"id": i, "kind": "shell"}) for i in ids])
    # The barrier only releases if all three are running at once.
    assert manager.await_tasks(ids, timeout=5)["ok"] is True
    manager.shutdown()


# ---------------------------------------------------------------------------
# awaiting
# ---------------------------------------------------------------------------
def test_await_with_no_ids_is_trivially_ok(manager: SubtaskManager) -> None:
    assert manager.await_tasks([]) == {
        "ok": True, "completed": [], "pending": [], "unknown": [], "timed_out": False
    }


def test_unknown_ids_are_reported_not_waited_on(manager: SubtaskManager) -> None:
    """Waiting forever for a task that was never spawned is the worst option."""
    started = time.time()
    outcome = manager.await_tasks(["ghost"], timeout=30)
    assert outcome["unknown"] == ["ghost"]
    assert outcome["ok"] is False
    assert time.time() - started < 5


def test_await_times_out_on_a_slow_child() -> None:
    gate = threading.Event()
    manager = SubtaskManager(lambda t: gate.wait(10) or FakeResult())
    manager.spawn(build_subtask({"id": "slow", "kind": "shell"}))
    outcome = manager.await_tasks(["slow"], timeout=0.1)
    assert outcome["timed_out"] is True
    assert outcome["ok"] is False
    assert [p["task_id"] for p in outcome["pending"]] == ["slow"]
    gate.set()
    manager.shutdown()


def test_require_all_false_returns_on_the_first_finisher() -> None:
    gate = threading.Event()

    def maybe_wait(task: Any) -> Any:
        if task.id == "slow":
            gate.wait(10)
        return FakeResult()

    manager = SubtaskManager(maybe_wait, max_active=4)
    manager.spawn(build_subtask({"id": "fast", "kind": "shell"}))
    manager.await_tasks(["fast"], timeout=5)
    manager.spawn(build_subtask({"id": "slow", "kind": "shell"}))

    outcome = manager.await_tasks(["fast", "slow"], timeout=0.3, require_all=False)
    assert any(c["task_id"] == "fast" for c in outcome["completed"])
    gate.set()
    manager.shutdown()


def test_await_is_not_ok_when_a_child_failed() -> None:
    manager = SubtaskManager(lambda t: FakeResult(ok=False))
    manager.spawn(build_subtask({"id": "c", "kind": "shell"}))
    assert manager.await_tasks(["c"], timeout=5)["ok"] is False
    manager.shutdown()


# ---------------------------------------------------------------------------
# introspection & lifecycle
# ---------------------------------------------------------------------------
def test_status_of_an_unknown_id_is_none(manager: SubtaskManager) -> None:
    assert manager.status("nope") is None


def test_active_count_drops_as_children_finish(manager: SubtaskManager) -> None:
    manager.spawn(build_subtask({"id": "c", "kind": "shell"}))
    manager.await_tasks(["c"], timeout=5)
    assert manager.active_count() == 0


def test_all_records_is_oldest_first(manager: SubtaskManager) -> None:
    manager.spawn(build_subtask({"id": "first", "kind": "shell"}))
    time.sleep(0.01)
    manager.spawn(build_subtask({"id": "second", "kind": "shell"}))
    manager.await_tasks(["first", "second"], timeout=5)
    assert [r.id for r in manager.all_records()] == ["first", "second"]


def test_a_cancelled_id_never_reaches_the_executor() -> None:
    """Cancelling before the worker picks the task up short-circuits execution."""
    ran: list[str] = []
    manager = SubtaskManager(lambda t: ran.append(t.id) or FakeResult())
    manager.cancel("doomed")  # id is not known yet, but the veto is remembered
    manager.spawn(build_subtask({"id": "doomed", "kind": "shell"}))
    manager.await_tasks(["doomed"], timeout=5)
    assert manager.status("doomed").state == "cancelled"
    assert ran == []
    manager.shutdown()


def test_cancel_of_a_running_child_is_not_this_managers_job() -> None:
    """Killing a live process belongs to the executor, so cancel() declines."""
    gate = threading.Event()
    manager = SubtaskManager(lambda t: gate.wait(5) or FakeResult())
    manager.spawn(build_subtask({"id": "live", "kind": "shell"}))
    while manager.status("live").state != "running":
        time.sleep(0.005)
    assert manager.cancel("live") is False
    gate.set()
    manager.shutdown()


def test_cancel_of_an_unknown_id_is_false(manager: SubtaskManager) -> None:
    assert manager.cancel("ghost") is False


def test_forget_finished_reclaims_records(manager: SubtaskManager) -> None:
    manager.spawn(build_subtask({"id": "c", "kind": "shell"}))
    manager.await_tasks(["c"], timeout=5)
    assert manager.forget_finished() == 1
    assert manager.status("c") is None


def test_manager_clamps_silly_limits() -> None:
    manager = SubtaskManager(lambda t: FakeResult(), max_active=0, max_depth=-3)
    assert manager.max_active == 1
    assert manager.max_depth == 1
    manager.shutdown()


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------
def test_finished_covers_every_terminal_state() -> None:
    for state in FINISHED_STATES:
        assert SubtaskRecord(id="x", kind="shell", parent_id=None, depth=0, state=state).finished


def test_duration_is_none_before_it_starts() -> None:
    assert SubtaskRecord(id="x", kind="shell", parent_id=None, depth=0).duration is None


def test_to_dict_can_omit_the_result() -> None:
    record = SubtaskRecord(id="x", kind="shell", parent_id=None, depth=0, result=FakeResult())
    assert "result" in record.to_dict()
    assert "result" not in record.to_dict(include_result=False)


def test_result_summary_truncates_long_output() -> None:
    summary = _result_summary(FakeResult(stdout="x" * 9000, stderr="y" * 5000))
    assert len(summary["stdout"]) == 4000
    assert len(summary["stderr"]) == 2000
    assert summary["stdout_truncated"] is True


def test_default_max_active_is_sane() -> None:
    assert DEFAULT_MAX_ACTIVE >= 1
    assert SubTask(id="x", kind="shell").depth == 0

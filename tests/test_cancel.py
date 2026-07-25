"""
Tests for task cancellation — agent/runner.py and ExecutorV2.stop().

Cancellation has three shapes and they must not be conflated: a queued task is
pulled out and owed a synthetic result; a running task gets signalled and
reports its own final chunk; an unknown id is an honest failure rather than a
silent success.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.helpers import FakeTask, StubReporter

from executor import Result
from executor_v2 import ExecutorV2
from policy import PolicyEngine
from runner import CANCEL_KIND, TaskRunner

IS_WINDOWS = sys.platform.startswith("win")


class SlowExecutor:
    """Blocks until released, so a task can be cancelled while it is running."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.stopped: list[str] = []
        self.started: set[str] = set()

    def run(self, task):
        self.started.add(str(task.id))
        self.entered.set()
        self.release.wait(timeout=5)
        return Result(task_id=task.id, ok=True, decision="auto")

    def stop(self, task_id: str, **_kwargs) -> bool:
        """Mirrors ExecutorV2.stop: False when there is no live handle."""
        if task_id not in self.started:
            return False
        self.stopped.append(task_id)
        self.release.set()
        return True


@pytest.fixture
def reporter() -> StubReporter:
    return StubReporter()


def make_runner(executor, reporter, **kwargs) -> TaskRunner:
    return TaskRunner(executor=executor, reporter=reporter, sleep=lambda _s: None, **kwargs)


def cancel_packet(target: str, task_id: str = "c1") -> FakeTask:
    return FakeTask(id=task_id, kind=CANCEL_KIND, payload={"task_id": target})


# ---------------------------------------------------------------------------
# queued targets
# ---------------------------------------------------------------------------
def test_cancelling_a_queued_task_removes_it(reporter):
    runner = make_runner(SlowExecutor(), reporter)
    runner.submit(FakeTask(id="victim"))
    assert runner.cancel("victim") == "queued"
    assert runner.queue.depth == 0


def test_a_cancelled_queued_task_never_runs(reporter):
    executor = SlowExecutor()
    runner = make_runner(executor, reporter)
    runner.submit(FakeTask(id="victim"))
    runner.cancel("victim")
    assert runner.pump() == 0
    assert not executor.entered.is_set()


def test_a_cancelled_queued_task_gets_a_final_chunk(reporter):
    """The producer must not be left waiting for a chunk that will never come."""
    runner = make_runner(SlowExecutor(), reporter)
    runner.submit(FakeTask(id="victim"))
    runner.handle_cancel(cancel_packet("victim"))

    finals = dict(reporter.final_chunks())
    assert "victim" in finals
    assert finals["victim"]["cancelled"] is True
    assert finals["victim"]["status"] == "cancelled"


def test_the_cancelled_target_records_who_cancelled_it(reporter):
    runner = make_runner(SlowExecutor(), reporter)
    runner.submit(FakeTask(id="victim"))
    runner.handle_cancel(cancel_packet("victim", task_id="the-canceller"))
    assert dict(reporter.final_chunks())["victim"]["cancelled_by"] == "the-canceller"


def test_the_cancel_task_reports_the_target_state(reporter):
    runner = make_runner(SlowExecutor(), reporter)
    runner.submit(FakeTask(id="victim"))
    result = runner.handle_cancel(cancel_packet("victim"))
    assert result.ok is True
    assert result.metadata["target_state"] == "queued"


# ---------------------------------------------------------------------------
# running targets
# ---------------------------------------------------------------------------
def test_cancelling_a_running_task_signals_the_executor(reporter):
    executor = SlowExecutor()
    runner = make_runner(executor, reporter)
    runner.submit(FakeTask(id="busy"))
    runner.pump()
    assert executor.entered.wait(timeout=5)

    assert runner.cancel("busy") == "running"
    assert executor.stopped == ["busy"]
    assert runner.wait_idle(timeout=5)


def test_a_running_target_is_not_reported_twice(reporter):
    """The executor owns the final chunk for a task it actually started."""
    executor = SlowExecutor()
    runner = make_runner(executor, reporter)
    runner.submit(FakeTask(id="busy"))
    runner.pump()
    executor.entered.wait(timeout=5)
    runner.handle_cancel(cancel_packet("busy"))
    runner.wait_idle(timeout=5)

    synthesised = [tid for tid, body in reporter.final_chunks() if body.get("cancelled_by")]
    assert synthesised == [], "the runner must not synthesise a result for a started task"


def test_cancel_falls_back_to_the_nested_v2_executor(reporter):
    """Executor delegates the v2 kinds, so stop() may live one level down."""
    inner = SlowExecutor()
    facade = SimpleNamespace(run=inner.run, v2=inner)
    runner = make_runner(facade, reporter)
    runner.submit(FakeTask(id="busy"))
    runner.pump()
    inner.entered.wait(timeout=5)
    assert runner.cancel("busy") == "running"
    assert inner.stopped == ["busy"]


# ---------------------------------------------------------------------------
# bad input
# ---------------------------------------------------------------------------
def test_cancelling_an_unknown_id_is_reported_as_unknown(reporter):
    runner = make_runner(SlowExecutor(), reporter)
    assert runner.cancel("ghost") == "unknown"


def test_a_cancel_for_an_unknown_id_fails_honestly(reporter):
    runner = make_runner(SlowExecutor(), reporter)
    result = runner.handle_cancel(cancel_packet("ghost"))
    assert result.ok is False
    assert "ghost" in result.error


def test_cancel_without_a_target_is_rejected(reporter):
    runner = make_runner(SlowExecutor(), reporter)
    result = runner.handle_cancel(FakeTask(id="c1", kind=CANCEL_KIND, payload={}))
    assert result.ok is False
    assert "payload.task_id" in result.error


def test_a_cancel_packet_bypasses_the_queue(reporter):
    """Queueing a cancellation behind the work it stops would defeat the point."""
    runner = make_runner(SlowExecutor(), reporter)
    runner.submit(cancel_packet("whatever"))
    assert runner.queue.depth == 0


# ---------------------------------------------------------------------------
# ExecutorV2.stop()
# ---------------------------------------------------------------------------
def make_cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        version=1,
        owner="tester",
        # sys.executable is an absolute path, so the glob has to be leading-*.
        auto_allow={"commands": ["*python*", "sleep*", "echo*"], "paths": {"read": [f"{tmp_path}/**"]}},
        require_confirm={"commands": [], "paths": {}},
        block={"commands": [], "paths": {}},
        polling={},
        paths={},
        transport={"repo": "iddo111/iddo-harness-bridge", "result_dir": "results/"},
        confirm={"timeout_minutes": 30},
    )


@pytest.fixture
def v2(tmp_path):
    chunks: list[dict] = []
    executor = ExecutorV2(
        PolicyEngine(make_cfg(tmp_path)),
        chunk_sink=lambda task, body, seq, is_final: chunks.append(
            {**body, "seq": seq, "is_final": is_final}
        ),
    )
    executor.recorded = chunks
    yield executor
    executor.shutdown()


def test_stop_on_an_idle_task_id_returns_false(v2):
    assert v2.stop("not-running") is False


def test_stop_still_records_the_cancellation_request(v2):
    """The flag has to outlive the miss: the task may start a moment later."""
    v2.stop("about-to-start")
    assert v2.cancel_requested("about-to-start") is True


def test_a_task_cancelled_before_it_starts_refuses_to_run(v2):
    v2.stop("t1")
    result = v2.run(FakeTask(id="t1", kind="shell_stream", payload={"command": "echo hi"}))
    assert result.ok is False
    assert result.decision == "cancelled"


def test_clear_cancel_forgets_the_flag(v2):
    v2.stop("t1")
    v2.clear_cancel("t1")
    assert v2.cancel_requested("t1") is False


@pytest.mark.skipif(IS_WINDOWS, reason="uses a POSIX sleep loop")
def test_stopping_a_streaming_shell_kills_the_process(v2):
    task = FakeTask(
        id="long",
        kind="shell_stream",
        payload={"command": f"{sys.executable} -u -c \"import time;print('go',flush=True);time.sleep(30)\"",
                 "timeout_sec": 60},
    )
    done: list[Result] = []
    worker = threading.Thread(target=lambda: done.append(v2.run(task)), daemon=True)
    worker.start()

    deadline = time.time() + 10
    while time.time() < deadline and "long" not in v2.running_task_ids():
        time.sleep(0.05)
    assert v2.stop("long") is True

    worker.join(timeout=15)
    assert done, "the cancelled task must return rather than hang"
    result = done[0]
    assert result.ok is False
    assert result.metadata["cancelled"] is True
    assert result.decision == "cancelled"


@pytest.mark.skipif(IS_WINDOWS, reason="uses a POSIX sleep loop")
def test_a_cancelled_stream_emits_exactly_one_final_chunk(v2):
    task = FakeTask(
        id="long",
        kind="shell_stream",
        payload={"command": f"{sys.executable} -u -c \"import time;print('go',flush=True);time.sleep(30)\"",
                 "timeout_sec": 60},
    )
    worker = threading.Thread(target=lambda: v2.run(task), daemon=True)
    worker.start()
    deadline = time.time() + 10
    while time.time() < deadline and "long" not in v2.running_task_ids():
        time.sleep(0.05)
    v2.stop("long")
    worker.join(timeout=15)

    finals = [c for c in v2.recorded if c["is_final"]]
    assert len(finals) == 1
    assert finals[0]["cancelled"] is True


def test_a_stopped_task_is_unregistered(v2):
    """A stale handle would let a later task with the same id be killed."""
    task = FakeTask(id="quick", kind="shell_stream", payload={"command": "echo hello"})
    v2.run(task)
    assert v2.running_task_ids() == []


def test_shutdown_clears_the_cancellation_bookkeeping(v2):
    v2.stop("t1")
    v2.shutdown()
    assert v2.cancel_requested("t1") is False


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX session semantics")
def test_stopping_an_open_session_closes_the_process(v2):
    opened = v2.run(
        FakeTask(id="sess", kind="shell_session_open",
                 payload={"command": f"{sys.executable} -i -u"})
    )
    assert opened.ok is True
    session_id = opened.metadata["session_id"]

    assert v2.stop("sess") is True
    deadline = time.time() + 5
    session = v2.sessions[session_id]
    while time.time() < deadline and session.alive:
        time.sleep(0.05)
    assert not session.alive, "cancelling the opening task must kill the REPL"


def test_cancel_kind_is_not_a_v2_kind():
    """`cancel` is scheduling, so the runner owns it — not the executor."""
    from executor_v2 import V2_KINDS

    assert CANCEL_KIND not in V2_KINDS


def test_subprocess_handles_are_terminated_generically(v2):
    """stop() works on anything Popen-shaped, not just what we opened."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        v2._register_running("external", proc)
        assert v2.stop("external") is True
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()

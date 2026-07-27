"""
Tests for retries and the concurrent worker pool — agent/runner.py.

Two things are easy to get wrong here and both are covered: retrying something
that should never be retried (a policy block is not a flaky failure), and
emitting two ``is_final`` chunks for one task because the retry path reported
alongside the executor's own stream.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from tests.helpers import FakeTask, StubReporter

from executor import Result
from runner import TaskRunner


class ScriptedExecutor:
    """Returns a queued sequence of results, one per call."""

    def __init__(self, *results) -> None:
        self.scripted = list(results)
        self.calls: list[str] = []

    def run(self, task):
        self.calls.append(str(task.id))
        if self.scripted:
            outcome = self.scripted.pop(0)
        else:
            outcome = Result(task_id=task.id, ok=True, decision="auto")
        if isinstance(outcome, Exception):
            raise outcome
        return Result(
            task_id=task.id,
            ok=outcome.ok,
            decision=outcome.decision,
            error=outcome.error,
            metadata=dict(outcome.metadata),
        )


def fail(decision: str = "auto", error: str = "boom", **metadata) -> Result:
    return Result(task_id="", ok=False, decision=decision, error=error, metadata=metadata)


def succeed() -> Result:
    return Result(task_id="", ok=True, decision="auto")


@pytest.fixture
def reporter() -> StubReporter:
    return StubReporter()


def make_runner(executor, reporter, **kwargs) -> TaskRunner:
    kwargs.setdefault("sleep", lambda _s: None)
    return TaskRunner(executor=executor, reporter=reporter, **kwargs)


def retrying(**retry) -> FakeTask:
    return FakeTask(id="t1", payload={"retry": retry})


# ---------------------------------------------------------------------------
# attempt counting
# ---------------------------------------------------------------------------
def test_a_task_runs_once_by_default(reporter):
    """v2 behaviour: no retry policy means no retries."""
    executor = ScriptedExecutor(fail())
    runner = make_runner(executor, reporter)
    outcome = runner.run_task(FakeTask(id="t1"))
    assert outcome.attempts == 1
    assert len(executor.calls) == 1


def test_a_failing_task_is_retried_up_to_max_attempts(reporter):
    executor = ScriptedExecutor(fail(), fail(), fail())
    outcome = make_runner(executor, reporter).run_task(retrying(max_attempts=3))
    assert outcome.attempts == 3
    assert len(executor.calls) == 3


def test_retrying_stops_as_soon_as_it_succeeds(reporter):
    executor = ScriptedExecutor(fail(), succeed(), fail())
    outcome = make_runner(executor, reporter).run_task(retrying(max_attempts=3))
    assert outcome.attempts == 2
    assert outcome.status == "ok"


def test_a_successful_task_is_never_retried(reporter):
    executor = ScriptedExecutor(succeed())
    outcome = make_runner(executor, reporter).run_task(retrying(max_attempts=3))
    assert outcome.attempts == 1


def test_the_config_default_applies_when_the_packet_is_silent(reporter):
    executor = ScriptedExecutor(fail(), fail())
    runner = make_runner(executor, reporter, default_max_attempts=2)
    assert runner.run_task(FakeTask(id="t1")).attempts == 2


def test_a_packet_retry_policy_overrides_the_config_default(reporter):
    executor = ScriptedExecutor(fail(), fail(), fail(), fail())
    runner = make_runner(executor, reporter, default_max_attempts=1)
    assert runner.run_task(retrying(max_attempts=4)).attempts == 4


def test_an_executor_exception_is_a_retryable_failure(reporter):
    """A crash in one kind must not take the agent down with it."""
    executor = ScriptedExecutor(RuntimeError("kaboom"), succeed())
    outcome = make_runner(executor, reporter).run_task(retrying(max_attempts=2))
    assert outcome.attempts == 2
    assert outcome.status == "ok"


def test_an_exception_on_the_last_attempt_becomes_an_error_result(reporter):
    executor = ScriptedExecutor(RuntimeError("kaboom"))
    outcome = make_runner(executor, reporter).run_task(FakeTask(id="t1"))
    assert outcome.status == "error"
    assert "kaboom" in outcome.result.error


# ---------------------------------------------------------------------------
# what must not be retried
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("decision", ["block", "blocked", "confirm_required", "cancelled"])
def test_a_policy_verdict_is_not_retried(reporter, decision):
    """Retrying a blocked command just re-blocks it, three times as loudly."""
    executor = ScriptedExecutor(fail(decision=decision), fail(decision=decision))
    outcome = make_runner(executor, reporter).run_task(retrying(max_attempts=3))
    assert outcome.attempts == 1


def test_a_cancelled_result_is_not_retried(reporter):
    executor = ScriptedExecutor(fail(cancelled=True), succeed())
    outcome = make_runner(executor, reporter).run_task(retrying(max_attempts=3))
    assert outcome.attempts == 1
    assert outcome.cancelled is True


def test_cancelling_mid_backoff_abandons_the_remaining_attempts(reporter):
    executor = ScriptedExecutor(fail(), fail(), fail())
    runner = make_runner(executor, reporter)

    def cancel_during_backoff(_seconds):
        runner.cancel("t1")

    runner.sleep = cancel_during_backoff
    outcome = runner.run_task(retrying(max_attempts=3, backoff_seconds=[1]))
    assert outcome.attempts == 1
    assert outcome.cancelled is True


# ---------------------------------------------------------------------------
# backoff
# ---------------------------------------------------------------------------
def test_backoff_is_applied_between_attempts_but_not_before_the_first(reporter):
    slept: list[float] = []
    executor = ScriptedExecutor(fail(), fail(), fail())
    runner = make_runner(executor, reporter, sleep=slept.append)
    runner.run_task(retrying(max_attempts=3, backoff_seconds=[1, 5, 15]))
    assert slept == [1.0, 5.0]


def test_no_backoff_configured_means_no_sleeping(reporter):
    slept: list[float] = []
    executor = ScriptedExecutor(fail(), fail())
    runner = make_runner(executor, reporter, sleep=slept.append)
    runner.run_task(retrying(max_attempts=2))
    assert slept == []


# ---------------------------------------------------------------------------
# attempt files
# ---------------------------------------------------------------------------
def test_every_attempt_is_filed_separately(reporter):
    executor = ScriptedExecutor(fail(), fail(), fail())
    make_runner(executor, reporter).run_task(retrying(max_attempts=3))
    assert [attempt for _tid, _res, attempt in reporter.attempts] == [1, 2, 3]


def test_a_single_attempt_task_writes_no_attempt_file(reporter):
    """Attempt files are for retries; an un-retried task keeps the v2 shape."""
    make_runner(ScriptedExecutor(fail()), reporter).run_task(FakeTask(id="t1"))
    assert reporter.attempts == []


def test_the_winning_attempt_is_filed_too(reporter):
    executor = ScriptedExecutor(fail(), succeed())
    make_runner(executor, reporter).run_task(retrying(max_attempts=2))
    assert [a for _t, _r, a in reporter.attempts] == [1, 2]


def test_the_final_result_is_still_reported_once(reporter):
    executor = ScriptedExecutor(fail(), fail())
    make_runner(executor, reporter).run_task(retrying(max_attempts=2))
    assert len(reporter.results) == 1


def test_a_streamed_result_is_not_reported_again(reporter):
    """The executor already closed the stream; a second final breaks consumers."""
    executor = ScriptedExecutor(
        Result(task_id="t1", ok=True, decision="auto", metadata={"final_chunk": {"seq": 4}})
    )
    make_runner(executor, reporter).run_task(FakeTask(id="t1"))
    assert reporter.results == []
    assert reporter.chunks == []


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------
class BlockingExecutor:
    """Holds every task until released, to observe how many run at once."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.concurrent = 0
        self.peak = 0
        self._lock = threading.Lock()

    def run(self, task):
        with self._lock:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
        try:
            self.release.wait(timeout=5)
        finally:
            with self._lock:
                self.concurrent -= 1
        return Result(task_id=task.id, ok=True, decision="auto")


def test_the_pool_honours_max_concurrent_tasks(reporter):
    executor = BlockingExecutor()
    runner = make_runner(executor, reporter, max_concurrent_tasks=3)
    for n in range(10):
        runner.submit(FakeTask(id=f"t{n}"))

    assert runner.pump() == 3
    assert runner.in_flight == 3
    assert runner.queue.depth == 7

    executor.release.set()
    assert runner.wait_idle(timeout=10)
    assert executor.peak <= 3


def test_pump_refills_freed_slots(reporter):
    executor = BlockingExecutor()
    runner = make_runner(executor, reporter, max_concurrent_tasks=2)
    for n in range(4):
        runner.submit(FakeTask(id=f"t{n}"))
    runner.pump()
    executor.release.set()
    runner.wait_idle(timeout=10)

    assert runner.pump() == 2
    assert runner.wait_idle(timeout=10)
    assert runner.queue.depth == 0


def test_pump_on_an_empty_queue_starts_nothing(reporter):
    runner = make_runner(BlockingExecutor(), reporter)
    assert runner.pump() == 0


def test_a_worker_crash_still_frees_its_slot(reporter):
    """A leaked semaphore permit would wedge the pool for good."""

    class Exploding:
        def run(self, task):
            raise RuntimeError("worker died")

    runner = make_runner(Exploding(), reporter, max_concurrent_tasks=1)
    runner.submit(FakeTask(id="t1"))
    runner.pump()
    assert runner.wait_idle(timeout=5)

    runner.submit(FakeTask(id="t2"))
    assert runner.pump() == 1


def test_drain_runs_the_whole_queue(reporter):
    executor = ScriptedExecutor()
    runner = make_runner(executor, reporter, max_concurrent_tasks=2)
    for n in range(6):
        runner.submit(FakeTask(id=f"t{n}"))
    assert runner.drain(timeout=15) is True
    assert sorted(executor.calls) == [f"t{n}" for n in range(6)]


def test_priority_is_respected_across_the_pool(reporter):
    executor = BlockingExecutor()
    runner = make_runner(executor, reporter, max_concurrent_tasks=1)
    runner.submit(FakeTask(id="normal"))
    runner.submit(FakeTask(id="urgent", priority="high"))
    runner.pump()
    assert runner.running_task_ids() == ["urgent"]
    executor.release.set()
    runner.wait_idle(timeout=10)


def test_an_expired_task_is_reported_not_silently_dropped(reporter):
    """The producer needs to learn its deadline was missed."""
    past = "2020-01-01T00:00:00Z"
    runner = make_runner(ScriptedExecutor(), reporter)
    runner.submit(FakeTask(id="stale", payload={"deadline": past}))
    runner.pump()

    finals = dict(reporter.final_chunks())
    assert finals["stale"]["status"] == "deadline_exceeded"


def test_on_finished_is_called_with_the_outcome(reporter):
    seen: list[str] = []
    runner = make_runner(
        ScriptedExecutor(succeed()), reporter, on_finished=lambda o: seen.append(o.status)
    )
    runner.run_task(FakeTask(id="t1"))
    assert seen == ["ok"]


def test_a_completed_task_unblocks_its_dependants(reporter):
    runner = make_runner(ScriptedExecutor(succeed()), reporter)
    runner.submit(FakeTask(id="child", payload={"depends_on": ["parent"]}))
    assert runner.pump() == 0

    runner.run_task(FakeTask(id="parent"))
    assert runner.pump() == 1
    assert runner.wait_idle(timeout=5)


def test_metrics_see_the_full_task_lifecycle(reporter):
    from metrics import Metrics

    metrics = Metrics(enabled=True)
    runner = make_runner(ScriptedExecutor(fail(), succeed()), reporter, metrics=metrics)
    runner.submit(FakeTask(id="t1", kind="shell"))
    runner.run_task(retrying(max_attempts=2))

    snap = metrics.snapshot()
    assert snap["tasks_by_status"]["ok"] == 1
    assert snap["retries_total"] == 1
    assert snap["tasks_by_kind"]["shell"] == 2, "one entry per attempt"

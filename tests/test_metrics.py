"""
Tests for the in-memory metrics collector and its HTTP endpoint.

The collector is instrumentation, so the important properties are the ones that
protect the rest of the harness from it: a disabled collector costs nothing, a
broken gauge cannot break ``/metrics``, and nothing it does may block a worker.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from metrics import (
    LATENCY_FAMILIES,
    Metrics,
    MetricsServer,
    get_metrics,
    percentile,
    set_metrics,
)


class FakeClock:
    """A monotonic clock the test drives by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def metrics(clock) -> Metrics:
    return Metrics(enabled=True, clock=clock)


# -- percentile -------------------------------------------------------------
def test_percentile_of_nothing_is_zero():
    assert percentile([], 0.5) == 0.0


def test_percentile_of_one_sample_is_that_sample():
    assert percentile([4.2], 0.99) == 4.2


def test_percentile_picks_by_nearest_rank():
    # Nearest rank over a 0-based index: p50 of 1..100 lands on samples[50].
    samples = list(range(1, 101))
    assert percentile(samples, 0.5) == 51
    assert percentile(samples, 0.95) == 95
    assert percentile(samples, 0.99) == 99


def test_percentile_does_not_care_about_input_order():
    assert percentile([9, 1, 5], 0.5) == percentile([1, 5, 9], 0.5)


# -- counters ---------------------------------------------------------------
def test_a_fresh_collector_counts_nothing(metrics):
    doc = metrics.snapshot()
    assert doc["tasks_total"] == 0
    assert doc["tasks_by_kind"] == {}
    assert doc["tasks_by_status"] == {}


def test_completion_increments_the_total(metrics):
    metrics.task_observed("t1")
    metrics.task_started("t1", "shell")
    metrics.task_completed("t1", "ok", "shell")
    assert metrics.snapshot()["tasks_total"] == 1


def test_tasks_are_tallied_by_kind(metrics):
    for task_id, kind in (("a", "shell"), ("b", "shell"), ("c", "read_file")):
        metrics.task_started(task_id, kind)
        metrics.task_completed(task_id, "ok", kind)
    assert metrics.snapshot()["tasks_by_kind"] == {"shell": 2, "read_file": 1}


def test_tasks_are_tallied_by_status(metrics):
    for task_id, status in (("a", "ok"), ("b", "error"), ("c", "cancelled"), ("d", "ok")):
        metrics.task_started(task_id, "shell")
        metrics.task_completed(task_id, status, "shell")
    by_status = metrics.snapshot()["tasks_by_status"]
    assert by_status == {"ok": 2, "error": 1, "cancelled": 1}


def test_completion_without_a_start_still_counts_its_kind(metrics):
    """A task blocked by policy never starts, but it did happen."""
    metrics.task_completed("blocked-1", "blocked", "shell")
    doc = metrics.snapshot()
    assert doc["tasks_by_kind"] == {"shell": 1}
    assert doc["tasks_by_status"] == {"blocked": 1}


def test_chunk_pushes_and_bytes_accumulate(metrics):
    metrics.chunk_pushed(100)
    metrics.chunk_pushed(50)
    metrics.chunk_pushed()
    doc = metrics.snapshot()
    assert doc["chunks_pushed"] == 3
    assert doc["shell_bytes_streamed"] == 150


def test_retries_are_counted(metrics):
    metrics.retry_attempted("t1")
    metrics.retry_attempted("t1")
    assert metrics.snapshot()["retries_total"] == 2


def test_incr_adds_arbitrary_counters(metrics):
    metrics.incr("ws_connections")
    metrics.incr("ws_connections", 4)
    assert metrics.snapshot()["ws_connections"] == 5


def test_reset_clears_everything(metrics):
    metrics.task_started("t1", "shell")
    metrics.task_completed("t1", "ok", "shell")
    metrics.chunk_pushed(10)
    metrics.reset()
    doc = metrics.snapshot()
    assert doc["tasks_total"] == 0
    assert doc["chunks_pushed"] == 0
    assert doc["tasks_by_kind"] == {}


# -- latency ----------------------------------------------------------------
def test_poll_to_start_measures_the_queue_wait(metrics, clock):
    metrics.task_observed("t1")
    clock.advance(3.0)
    metrics.task_started("t1", "shell")
    assert metrics.snapshot()["latency_poll_to_start"]["p50"] == 3.0


def test_start_to_complete_measures_execution(metrics, clock):
    metrics.task_observed("t1")
    metrics.task_started("t1", "shell")
    clock.advance(7.5)
    metrics.task_completed("t1", "ok", "shell")
    assert metrics.snapshot()["latency_start_to_complete"]["p50"] == 7.5


def test_both_latency_families_are_always_reported(metrics):
    doc = metrics.snapshot()
    for family in LATENCY_FAMILIES:
        bucket = doc[f"latency_{family}"]
        assert bucket == {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0}


def test_latency_quantiles_over_many_samples(metrics, clock):
    for i in range(1, 101):
        metrics.task_observed(f"t{i}")
        clock.advance(float(i))
        metrics.task_started(f"t{i}", "shell")
    bucket = metrics.snapshot()["latency_poll_to_start"]
    assert bucket["count"] == 100
    assert bucket["p50"] == 51.0
    assert bucket["p99"] == 99.0


def test_a_start_with_no_prior_observation_does_not_crash(metrics):
    """The WS bridge can start a task the poller never saw."""
    metrics.task_started("surprise", "shell")
    assert metrics.snapshot()["latency_poll_to_start"]["count"] == 1


def test_completion_without_a_start_records_no_execution_latency(metrics):
    metrics.task_completed("never-ran", "blocked", "shell")
    assert metrics.snapshot()["latency_start_to_complete"]["count"] == 0


def test_latency_window_is_bounded(clock):
    """Weeks of uptime must not grow the sample buffers without bound."""
    metrics = Metrics(enabled=True, clock=clock)
    for i in range(3000):
        metrics.task_observed(f"t{i}")
        metrics.task_started(f"t{i}", "shell")
    assert metrics.snapshot()["latency_poll_to_start"]["count"] <= 1024


def test_a_clock_that_goes_backwards_never_yields_a_negative_latency(metrics, clock):
    metrics.task_observed("t1")
    clock.advance(-5.0)
    metrics.task_started("t1", "shell")
    assert metrics.snapshot()["latency_poll_to_start"]["p50"] == 0.0


# -- gauges -----------------------------------------------------------------
def test_registered_gauges_are_pulled_at_snapshot_time(metrics):
    depth = [0]
    metrics.register_gauge("queue_depth", lambda: depth[0])
    assert metrics.snapshot()["queue_depth"] == 0
    depth[0] = 12
    assert metrics.snapshot()["queue_depth"] == 12


def test_static_gauges_can_be_set(metrics):
    metrics.set_gauge("active_watches", 4)
    assert metrics.snapshot()["active_watches"] == 4


def test_the_documented_gauges_are_always_present(metrics):
    doc = metrics.snapshot()
    for name in ("queue_depth", "active_sessions", "active_watches", "active_tasks"):
        assert doc[name] == 0


def test_a_raising_gauge_cannot_break_the_snapshot(metrics):
    def boom() -> int:
        raise RuntimeError("gauge exploded")

    metrics.register_gauge("queue_depth", boom)
    assert metrics.snapshot()["queue_depth"] == -1


def test_gauges_are_pulled_outside_the_lock(metrics):
    """A gauge reaching back into the collector would otherwise deadlock."""
    metrics.register_gauge("active_tasks", lambda: metrics.snapshot()["tasks_total"])
    assert metrics.snapshot()["active_tasks"] == 0


# -- disabled ---------------------------------------------------------------
def test_a_disabled_collector_records_nothing(clock):
    metrics = Metrics(enabled=False, clock=clock)
    metrics.task_observed("t1")
    metrics.task_started("t1", "shell")
    metrics.task_completed("t1", "ok", "shell")
    metrics.chunk_pushed(999)
    metrics.incr("whatever")
    doc = metrics.snapshot()
    assert doc["enabled"] is False
    assert doc["tasks_total"] == 0
    assert doc["chunks_pushed"] == 0


def test_a_disabled_collector_still_snapshots(clock):
    """Callers never guard their instrumentation, so nothing may raise."""
    assert Metrics(enabled=False, clock=clock).snapshot()["tasks_total"] == 0
    assert Metrics(enabled=False, clock=clock).prometheus()


# -- thread safety ----------------------------------------------------------
def test_concurrent_updates_are_not_lost(metrics):
    def worker(offset: int) -> None:
        for i in range(100):
            task_id = f"t{offset}-{i}"
            metrics.task_started(task_id, "shell")
            metrics.task_completed(task_id, "ok", "shell")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert metrics.snapshot()["tasks_total"] == 800


# -- prometheus -------------------------------------------------------------
def test_prometheus_output_declares_help_and_type(metrics):
    text = metrics.prometheus()
    assert "# HELP iddo_harness_tasks_total" in text
    assert "# TYPE iddo_harness_tasks_total counter" in text


def test_prometheus_labels_kinds_and_statuses(metrics):
    metrics.task_started("t1", "shell")
    metrics.task_completed("t1", "ok", "shell")
    text = metrics.prometheus()
    assert 'iddo_harness_tasks_by_kind{kind="shell"} 1' in text
    assert 'iddo_harness_tasks_by_status{status="ok"} 1' in text


def test_prometheus_renders_latency_summaries(metrics, clock):
    metrics.task_observed("t1")
    clock.advance(2.0)
    metrics.task_started("t1", "shell")
    text = metrics.prometheus()
    assert 'iddo_harness_latency_poll_to_start_seconds{quantile="0.50"} 2.0' in text
    assert "iddo_harness_latency_poll_to_start_seconds_count 1" in text


def test_prometheus_ends_with_a_newline(metrics):
    """Scrapers reject a body whose last line is unterminated."""
    assert metrics.prometheus().endswith("\n")


def test_prometheus_renders_gauges(metrics):
    metrics.register_gauge("queue_depth", lambda: 5)
    assert "iddo_harness_queue_depth 5" in metrics.prometheus()


# -- process-wide instance --------------------------------------------------
def test_set_metrics_installs_the_process_collector():
    original = get_metrics()
    try:
        replacement = Metrics(enabled=True)
        assert set_metrics(replacement) is replacement
        assert get_metrics() is replacement
    finally:
        set_metrics(original)


# -- HTTP endpoint ----------------------------------------------------------
@pytest.fixture
def server(metrics):
    srv = MetricsServer(metrics, port=0, version="3.0.0")
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def fetch(server: MetricsServer, path: str) -> tuple[int, str, str]:
    url = f"http://127.0.0.1:{server.bound_port}{path}"
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read().decode("utf-8"), response.headers["Content-Type"]


def test_metrics_endpoint_serves_json(server, metrics):
    metrics.task_started("t1", "shell")
    metrics.task_completed("t1", "ok", "shell")
    status, body, content_type = fetch(server, "/metrics")
    assert status == 200
    assert "json" in content_type
    assert json.loads(body)["tasks_total"] == 1


def test_metrics_endpoint_serves_prometheus_on_request(server):
    status, body, content_type = fetch(server, "/metrics?format=prom")
    assert status == 200
    assert content_type.startswith("text/plain")
    assert "iddo_harness_tasks_total" in body


def test_health_endpoint_reports_version(server):
    status, body, _ = fetch(server, "/health")
    doc = json.loads(body)
    assert status == 200
    assert doc["status"] == "ok"
    assert doc["version"] == "3.0.0"
    assert doc["uptime"] >= 0


def test_unknown_route_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        fetch(server, "/secrets")
    assert excinfo.value.code == 404


def test_port_zero_binds_a_real_port(server):
    assert server.bound_port > 0


def test_server_stops_cleanly(metrics):
    srv = MetricsServer(metrics, port=0)
    srv.start()
    port = srv.bound_port
    srv.stop()
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)

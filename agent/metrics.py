"""
In-memory metrics for the harness.

Deliberately dependency-free and process-local: the harness is a single
long-lived agent on somebody's desktop, not a fleet, so a thread-safe
collector plus a ``/metrics`` endpoint on the WebSocket server is enough.
Nothing is persisted — a restart resets the counters, and that is fine
because the audit log is the durable record.

Two latency families are tracked per the v3 brief:

* ``poll_to_start``    — how long a task waited between being noticed and running.
* ``start_to_complete`` — how long the task itself took.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Iterable

# Keep the last N samples per latency family. Enough for stable p99 without
# growing without bound in a process that may run for weeks.
LATENCY_WINDOW = 1024

LATENCY_FAMILIES = ("poll_to_start", "start_to_complete")

#: Counters that snapshot() reports under a name of its own choosing. Anything
#: else recorded through incr() is passed through under its raw name.
NAMED_COUNTERS = frozenset(
    {"tasks_total", "shell_bytes_streamed", "chunks_pushed", "retries_total"}
)


def percentile(samples: Iterable[float], fraction: float) -> float:
    """Nearest-rank percentile. Returns 0.0 for an empty sample set."""
    ordered = sorted(samples)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    rank = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return float(ordered[rank])


@dataclass
class _Timer:
    """A task's in-flight timing record."""

    observed_at: float
    started_at: float | None = None


class Metrics:
    """Thread-safe counters and latency histograms.

    Every mutator is a no-op when ``enabled`` is False, so callers never need
    to guard their instrumentation with an if-statement.
    """

    def __init__(self, enabled: bool = True, clock: Callable[[], float] = time.monotonic) -> None:
        self.enabled = enabled
        self._clock = clock
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._by_kind: dict[str, int] = defaultdict(int)
        self._by_status: dict[str, int] = defaultdict(int)
        self._latencies: dict[str, deque[float]] = {
            family: deque(maxlen=LATENCY_WINDOW) for family in LATENCY_FAMILIES
        }
        self._timers: dict[str, _Timer] = {}
        self._gauges: dict[str, Callable[[], int]] = {}
        self._static_gauges: dict[str, int] = defaultdict(int)

    # -- lifecycle hooks ----------------------------------------------------

    def task_observed(self, task_id: str) -> None:
        """Called when the poller (or WS bridge) first sees a task."""
        if not self.enabled:
            return
        with self._lock:
            self._timers[task_id] = _Timer(observed_at=self._clock())

    def task_started(self, task_id: str, kind: str | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            now = self._clock()
            timer = self._timers.get(task_id)
            if timer is None:
                timer = _Timer(observed_at=now)
                self._timers[task_id] = timer
            timer.started_at = now
            self._latencies["poll_to_start"].append(max(0.0, now - timer.observed_at))
            if kind:
                self._by_kind[kind] += 1

    def task_completed(self, task_id: str, status: str, kind: str | None = None) -> None:
        """Record a terminal outcome: ``ok``, ``error``, ``cancelled``, ..."""
        if not self.enabled:
            return
        with self._lock:
            now = self._clock()
            timer = self._timers.pop(task_id, None)
            if timer is not None and timer.started_at is not None:
                self._latencies["start_to_complete"].append(max(0.0, now - timer.started_at))
            self._counters["tasks_total"] += 1
            self._by_status[status] += 1
            if kind and timer is None:
                # Completion without a matching start (e.g. blocked by policy
                # before execution) still belongs in the per-kind tally.
                self._by_kind[kind] += 1

    # -- raw counters -------------------------------------------------------

    def incr(self, name: str, amount: int = 1) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._counters[name] += amount

    def chunk_pushed(self, byte_count: int = 0) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._counters["chunks_pushed"] += 1
            if byte_count:
                self._counters["shell_bytes_streamed"] += byte_count

    def retry_attempted(self, task_id: str) -> None:
        self.incr("retries_total")

    # -- gauges -------------------------------------------------------------

    def register_gauge(self, name: str, provider: Callable[[], int]) -> None:
        """Attach a live gauge, e.g. ``queue_depth`` -> ``len(queue)``.

        Gauges are pulled at snapshot time rather than pushed, so they cannot
        drift out of sync with the thing they describe.
        """
        with self._lock:
            self._gauges[name] = provider

    def set_gauge(self, name: str, value: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._static_gauges[name] = value

    # -- reporting ----------------------------------------------------------

    def snapshot(self) -> dict:
        """A JSON-serialisable view of everything collected so far."""
        with self._lock:
            counters = dict(self._counters)
            by_kind = dict(self._by_kind)
            by_status = dict(self._by_status)
            latencies = {
                family: list(samples) for family, samples in self._latencies.items()
            }
            gauges: dict[str, int] = dict(self._static_gauges)
            providers = dict(self._gauges)

        # Gauge providers run outside the lock: they reach into the queue and
        # the executor, and holding our lock while doing so invites deadlock.
        for name, provider in providers.items():
            try:
                gauges[name] = int(provider())
            except Exception:  # a broken gauge must not break /metrics
                gauges[name] = -1

        doc: dict = {
            "enabled": self.enabled,
            "tasks_total": counters.get("tasks_total", 0),
            "tasks_by_kind": by_kind,
            "tasks_by_status": by_status,
            "shell_bytes_streamed": counters.get("shell_bytes_streamed", 0),
            "chunks_pushed": counters.get("chunks_pushed", 0),
            "retries_total": counters.get("retries_total", 0),
        }
        # Counters registered ad hoc through incr() — ws_connections and
        # friends — would otherwise be collected and never reported.
        for name, value in counters.items():
            if name not in NAMED_COUNTERS:
                doc[name] = value
        for name in ("queue_depth", "active_sessions", "active_watches", "active_tasks"):
            doc[name] = gauges.get(name, 0)
        for name, value in gauges.items():
            doc.setdefault(name, value)
        for family, samples in latencies.items():
            doc[f"latency_{family}"] = {
                "count": len(samples),
                "p50": round(percentile(samples, 0.50), 6),
                "p95": round(percentile(samples, 0.95), 6),
                "p99": round(percentile(samples, 0.99), 6),
            }
        return doc

    def prometheus(self) -> str:
        """Render the snapshot in Prometheus text exposition format."""
        doc = self.snapshot()
        lines: list[str] = []

        def emit(name: str, value: float, labels: str = "", kind: str = "counter",
                 help_text: str = "") -> None:
            if not labels:
                lines.append(f"# HELP {name} {help_text or name}")
                lines.append(f"# TYPE {name} {kind}")
            lines.append(f"{name}{labels} {value}")

        emit("iddo_harness_tasks_total", doc["tasks_total"],
             help_text="Tasks that reached a terminal state.")

        if doc["tasks_by_kind"]:
            lines.append("# HELP iddo_harness_tasks_by_kind Tasks seen, by kind.")
            lines.append("# TYPE iddo_harness_tasks_by_kind counter")
            for kind, value in sorted(doc["tasks_by_kind"].items()):
                lines.append(f'iddo_harness_tasks_by_kind{{kind="{kind}"}} {value}')

        if doc["tasks_by_status"]:
            lines.append("# HELP iddo_harness_tasks_by_status Tasks by terminal status.")
            lines.append("# TYPE iddo_harness_tasks_by_status counter")
            for status, value in sorted(doc["tasks_by_status"].items()):
                lines.append(f'iddo_harness_tasks_by_status{{status="{status}"}} {value}')

        emit("iddo_harness_shell_bytes_streamed", doc["shell_bytes_streamed"],
             help_text="Bytes of shell output streamed to consumers.")
        emit("iddo_harness_chunks_pushed", doc["chunks_pushed"],
             help_text="Result chunks written to a transport.")
        emit("iddo_harness_retries_total", doc["retries_total"],
             help_text="Task attempts beyond the first.")

        for name in ("queue_depth", "active_sessions", "active_watches", "active_tasks"):
            emit(f"iddo_harness_{name}", doc.get(name, 0), kind="gauge",
                 help_text=f"Current {name.replace('_', ' ')}.")

        for family in LATENCY_FAMILIES:
            bucket = doc[f"latency_{family}"]
            metric = f"iddo_harness_latency_{family}_seconds"
            lines.append(f"# HELP {metric} Task {family.replace('_', ' ')} latency.")
            lines.append(f"# TYPE {metric} summary")
            for quantile in ("50", "95", "99"):
                value = bucket[f"p{quantile}"]
                lines.append(f'{metric}{{quantile="0.{quantile}"}} {value}')
            lines.append(f"{metric}_count {bucket['count']}")

        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Drop all samples and counters. Used by tests."""
        with self._lock:
            self._counters.clear()
            self._by_kind.clear()
            self._by_status.clear()
            self._static_gauges.clear()
            self._timers.clear()
            for samples in self._latencies.values():
                samples.clear()


DEFAULT_METRICS_PORT = 8478


class MetricsServer:
    """Stdlib HTTP server exposing ``/metrics`` and ``/health``.

    Bound to loopback, and requests from any other address get 403 even if the
    operator widens the bind — these endpoints describe what the machine is
    doing, which is not something to hand out. Deliberately not part of the
    WebSocket bridge: metrics should be readable whether or not the low-latency
    transport is switched on.
    """

    def __init__(
        self,
        metrics: Metrics,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_METRICS_PORT,
        version: str = "3.0.0",
    ) -> None:
        self.metrics = metrics
        self.host = host
        self.port = port
        self.version = version
        self._httpd: object | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        """The real port, which differs from ``port`` when 0 was requested."""
        return self._httpd.server_address[1] if self._httpd is not None else self.port  # type: ignore[attr-defined]

    def start(self) -> None:
        import json as _json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlparse

        outer = self
        started_at = time.time()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: object) -> None:
                pass  # BaseHTTPRequestHandler logs to stderr by default.

            def _reply(self, code: int, body: str, content_type: str) -> None:
                raw = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self._reply(403, '{"error":"localhost only"}', "application/json")
                    return
                parsed = urlparse(self.path)
                route = parsed.path.rstrip("/") or "/"
                if route == "/health":
                    self._reply(
                        200,
                        _json.dumps({
                            "status": "ok",
                            "uptime": round(time.time() - started_at, 3),
                            "version": outer.version,
                        }),
                        "application/json",
                    )
                    return
                if route != "/metrics":
                    self._reply(404, '{"error":"not found"}', "application/json")
                    return
                fmt = (parse_qs(parsed.query).get("format") or ["json"])[0]
                if fmt in {"prom", "prometheus", "text"}:
                    self._reply(200, outer.metrics.prometheus(), "text/plain; version=0.0.4")
                else:
                    self._reply(200, _json.dumps(outer.metrics.snapshot(), indent=2, sort_keys=True),
                                "application/json")

        httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="metrics-http", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()  # type: ignore[attr-defined]
            httpd.server_close()  # type: ignore[attr-defined]
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


#: Process-wide default collector. Modules instrument against this; the
#: runner swaps in a configured instance at startup via :func:`set_metrics`.
METRICS = Metrics()


def get_metrics() -> Metrics:
    return METRICS


def set_metrics(instance: Metrics) -> Metrics:
    """Install ``instance`` as the process-wide collector."""
    global METRICS
    METRICS = instance
    return METRICS

"""
Local health / audit / policy HTTP endpoint.

The harness is a background service on someone's workstation. When it stops
picking up tasks, the only way to find out today is to read a log file. This
gives it a face:

    GET /health            {"status": "ok"|"degraded", "uptime_sec", "version", ...}
    GET /metrics           counters (JSON, or Prometheus text with ?format=prom)
    GET /audit/tail?n=100  the last N audit records
    GET /policy            the active policy YAML, for debugging a decision

Everything is localhost-only, twice over: the socket binds ``127.0.0.1`` so it
is not reachable off the machine, *and* every handler re-checks the peer address
and answers 403 otherwise. The second check matters because a future config
might widen the bind address, and because ``/audit/tail`` and ``/policy`` expose
exactly the information an attacker would want next — which rules are enforced,
and what the agent has already been allowed to do.

Track A's ``/metrics`` supersedes the one here: when a metrics provider is
injected (``metrics_provider``) its numbers are served instead of the small
built-in set.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("harness.health")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8478
VERSION = "3.0.0-track-b"

LOCALHOST_ADDRESSES = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

MAX_TAIL = 1000


def is_localhost(address: str) -> bool:
    """True for a loopback peer address.

    Accepts the whole 127/8 block, not just 127.0.0.1: a client bound to
    127.0.0.2 is equally local, and rejecting it would be a confusing failure.
    """
    if address in LOCALHOST_ADDRESSES:
        return True
    return address.startswith("127.")


class HealthServer:
    """A small threaded HTTP server exposing health, metrics, audit, and policy."""

    def __init__(
        self,
        cfg: Any = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        audit_log: Any | None = None,
        policy_path: Path | str | None = None,
        metrics_provider: Callable[[], dict[str, Any]] | None = None,
        status_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.host = host
        self.port = port
        self.audit_log = audit_log
        self.policy_path = Path(policy_path) if policy_path else None
        self.metrics_provider = metrics_provider
        self.status_provider = status_provider
        self.started_at = time.time()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -----------------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: Any, **kwargs: Any) -> "HealthServer":
        """Build from the ``health`` block of policy.yaml."""
        health = getattr(cfg, "health", None) or {}
        if not isinstance(health, dict):
            health = {}
        return cls(
            cfg=cfg,
            host=health.get("host", DEFAULT_HOST),
            port=int(health.get("port", DEFAULT_PORT)),
            **kwargs,
        )

    @staticmethod
    def enabled_in(cfg: Any) -> bool:
        """True when policy.yaml switches the endpoint on (default: off)."""
        health = getattr(cfg, "health", None) or {}
        return bool(isinstance(health, dict) and health.get("enabled", False))

    # -----------------------------------------------------------------------
    def start(self) -> int:
        """Start serving in a daemon thread and return the bound port.

        Port 0 binds an ephemeral port, which is how the tests avoid colliding
        with a real agent on 8478.
        """
        server = self
        handler = _make_handler(server)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="harness-health", daemon=True
        )
        self._thread.start()
        log.info(f"health endpoint listening on http://{self.host}:{self.port}")
        return self.port

    def stop(self) -> None:
        """Shut the server down and join its thread."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "HealthServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -----------------------------------------------------------------------
    # Payload builders
    # -----------------------------------------------------------------------
    def health_payload(self) -> dict[str, Any]:
        """``/health`` body. ``degraded`` when a subsystem reports trouble."""
        payload: dict[str, Any] = {
            "status": "ok",
            "uptime_sec": round(time.time() - self.started_at, 3),
            "version": VERSION,
        }
        if self.status_provider is not None:
            try:
                extra = self.status_provider() or {}
                payload.update(extra)
                if extra.get("degraded"):
                    payload["status"] = "degraded"
            except Exception as e:
                payload["status"] = "degraded"
                payload["error"] = str(e)
        return payload

    def metrics_payload(self) -> dict[str, Any]:
        """``/metrics`` body — the injected provider, else a built-in minimum."""
        if self.metrics_provider is not None:
            try:
                return dict(self.metrics_provider())
            except Exception as e:
                return {"error": str(e)}
        audit_lines = 0
        if self.audit_log is not None:
            try:
                audit_lines = len(self.audit_log.tail(MAX_TAIL))
            except Exception:
                audit_lines = 0
        return {
            "uptime_sec": round(time.time() - self.started_at, 3),
            "audit_records_recent": audit_lines,
        }

    def audit_tail(self, n: int) -> list[dict[str, Any]]:
        """Last ``n`` audit records, clamped to :data:`MAX_TAIL`."""
        if self.audit_log is None:
            return []
        return self.audit_log.tail(max(1, min(n, MAX_TAIL)))

    def policy_text(self) -> str:
        """The active policy YAML as text, for debugging a decision."""
        if self.policy_path and self.policy_path.exists():
            return self.policy_path.read_text(encoding="utf-8")
        for candidate in (
            Path.home() / ".iddo-harness" / "policy.yaml",
            Path(__file__).parent.parent / "policy.yaml",
        ):
            if candidate.exists():
                return candidate.read_text(encoding="utf-8")
        return ""


# ---------------------------------------------------------------------------
def _to_prometheus(metrics: dict[str, Any]) -> str:
    """Render flat numeric metrics in Prometheus text exposition format."""
    lines = []
    for key, value in sorted(metrics.items()):
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, (int, float)):
            lines.append(f"iddo_harness_{key} {value}")
        elif isinstance(value, dict):
            for label, inner in sorted(value.items()):
                if isinstance(inner, (int, float)) and not isinstance(inner, bool):
                    lines.append(f'iddo_harness_{key}{{name="{label}"}} {inner}')
    return "\n".join(lines) + "\n"


def _make_handler(server: HealthServer) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to one :class:`HealthServer`."""

    class Handler(BaseHTTPRequestHandler):
        server_version = f"iddo-harness/{VERSION}"

        # -- plumbing -------------------------------------------------------
        def log_message(self, fmt: str, *args: Any) -> None:
            """Route access logs through the module logger instead of stderr."""
            log.debug("health %s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json")

        # -- routing --------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            """Route a GET, rejecting any non-loopback peer with 403."""
            if not is_localhost(self.client_address[0]):
                log.warning(f"rejected non-local health request from {self.client_address[0]}")
                self._json(403, {"error": "forbidden: localhost only"})
                return

            parsed = urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if route == "/health" or route == "/":
                self._json(200, server.health_payload())
            elif route == "/metrics":
                metrics = server.metrics_payload()
                if (query.get("format") or [""])[0] == "prom":
                    self._send(200, _to_prometheus(metrics).encode("utf-8"), "text/plain; version=0.0.4")
                else:
                    self._json(200, metrics)
            elif route == "/audit/tail":
                try:
                    n = int((query.get("n") or ["100"])[0])
                except ValueError:
                    self._json(400, {"error": "n must be an integer"})
                    return
                records = server.audit_tail(n)
                self._json(200, {"count": len(records), "records": records})
            elif route == "/policy":
                text = server.policy_text()
                if not text:
                    self._json(404, {"error": "no policy.yaml found"})
                else:
                    self._send(200, text.encode("utf-8"), "text/yaml; charset=utf-8")
            else:
                self._json(404, {"error": f"no such route: {route}"})

    return Handler

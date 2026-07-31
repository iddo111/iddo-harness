"""
WebSocket bridge — the low-latency transport, alongside the git bridge.

The git bridge is the reason this harness works from anywhere: no open port, no
tunnel, fine behind NAT. Its cost is latency — a task waits up to one poll
interval, and each result waits for a push. When the consumer happens to be on
the same machine (or the far side of an existing tunnel), that tradeoff is not
worth paying.

So: a consumer opens ``ws://<host>:<port>/tasks``, sends a task packet as JSON,
and gets the chunk stream back down the same socket as it is produced. Same
packets, same policy engine, same chunk contract as ``docs/v2_spec.md`` §3 —
only the transport differs. Nothing here replaces the git bridge; both run at
once and the executor cannot tell them apart.

Off unless ``ws.enabled`` is true in ``config.yaml``, because it does open a
port. Auth is a bearer token from ``~/.iddo-harness/ws-token``: no token, or
the wrong one, closes the connection before a single packet is read.

``websockets`` is an optional dependency. Absent, :func:`serve` refuses to
start and says so — it never silently degrades into an unauthenticated or
non-existent listener.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from http import HTTPStatus
from typing import Any, Callable, Iterable

log = logging.getLogger("iddo-harness.ws")

DEFAULT_PORT = 8477
TASK_PATH = "/tasks"

#: 4401 is in the WebSocket private-use range; consumers can distinguish "your
#: token is wrong" from "the harness went away" (1001/1006).
CLOSE_UNAUTHORIZED = 4401
CLOSE_BAD_REQUEST = 4400


class WsUnavailable(RuntimeError):
    """The ``websockets`` package is not installed."""


def websockets_available() -> bool:
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


def token_matches(presented: str | None, expected: str) -> bool:
    """Constant-time bearer-token comparison, tolerating the ``Bearer`` prefix."""
    if not presented or not expected:
        return False
    text = presented.strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return secrets.compare_digest(text, expected)


def extract_token(headers: Any) -> str | None:
    """Pull the credential out of a handshake.

    Browsers cannot set headers on a WebSocket handshake, so the
    ``Sec-WebSocket-Protocol`` subprotocol form (``bearer.<token>``) is
    accepted as well as ``Authorization``.
    """
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    for name in ("Authorization", "authorization", "X-Auth-Token", "x-auth-token"):
        value = getter(name)
        if value:
            return str(value)
    protocols = getter("Sec-WebSocket-Protocol") or getter("sec-websocket-protocol")
    if protocols:
        for part in str(protocols).split(","):
            part = part.strip()
            if part.startswith("bearer."):
                return part[len("bearer."):]
    return None


def task_from_json(doc: dict[str, Any]) -> Any:
    """Build a :class:`agent.poller.Task` from an inbound packet.

    Delegates to ``poller.task_from_packet`` rather than re-implementing the
    AMP/legacy split, so a packet means exactly the same thing arriving over a
    socket as it does arriving in the bridge repo — including the relaxed id
    check that lets migration-era envelopes through.

    An id is minted for a legacy packet that omits one: over a socket the reply
    address *is* the socket, so the id only has to correlate chunks.
    """
    try:
        import amp
        from poller import task_from_packet
    except ImportError:  # pragma: no cover - package layout
        from agent import amp  # type: ignore[no-redef]
        from agent.poller import task_from_packet  # type: ignore[no-redef]

    if not amp.is_amp_shaped(doc):
        if not doc.get("kind"):
            raise ValueError("task packet needs a 'kind'")
        if not doc.get("id"):
            doc = {**doc, "id": f"ws-{secrets.token_hex(6)}"}
    return task_from_packet(doc, None)


class WsBridge:
    """Serves ``/tasks`` over WebSocket, streaming chunks back per connection.

    One :class:`WsBridge` per process. ``execute`` is injected — in production
    it is a closure over the shared executor, in tests a stub — so the bridge
    stays a transport and holds no execution logic of its own.
    """

    def __init__(
        self,
        *,
        execute: Callable[[Any, Callable[[dict[str, Any], int, bool], None]], Any],
        token: str,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        metrics: Any = None,
    ) -> None:
        if not token:
            raise ValueError("ws bridge refuses to start without a token")
        self.execute = execute
        self.token = token
        self.host = host
        self.port = port
        self.metrics = metrics
        self._server: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._shutdown: asyncio.Event | None = None

    @property
    def bound_port(self) -> int:
        """The real port, which differs from ``port`` when 0 was requested."""
        if self._server is None:
            return self.port
        for sock in getattr(self._server, "sockets", ()) or ():
            return int(sock.getsockname()[1])
        return self.port

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.bound_port}{TASK_PATH}"

    # -- connection handling ------------------------------------------------

    async def handle(self, connection: Any) -> None:
        """Authenticate, then run one task per inbound message."""
        if not self._authorised(connection):
            log.warning("ws: rejected unauthenticated connection")
            await connection.close(CLOSE_UNAUTHORIZED, "unauthorized")
            if self.metrics is not None:
                self.metrics.incr("ws_rejected")
            return

        path = _connection_path(connection)
        if path and path.split("?")[0].rstrip("/") not in {TASK_PATH, ""}:
            await connection.close(CLOSE_BAD_REQUEST, f"unknown path: {path}")
            return

        if self.metrics is not None:
            self.metrics.incr("ws_connections")

        async for message in connection:
            await self._run_one(connection, message)

    def process_request(self, connection: Any, request: Any) -> Any:
        """Reject during the handshake, before a WebSocket even exists.

        An unauthenticated caller gets a plain HTTP ``401`` rather than an
        upgrade followed by a close frame: there is no reason to hand someone a
        socket only to take it away, and a 401 is what a non-WebSocket probe
        (curl, a scanner, a misconfigured consumer) can actually understand.
        :meth:`handle` re-checks anyway, so a transport that cannot reject early
        is still safe.
        """
        headers = getattr(request, "headers", None)
        if not token_matches(extract_token(headers), self.token):
            log.warning("ws: rejected unauthenticated handshake")
            if self.metrics is not None:
                self.metrics.incr("ws_rejected")
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")

        path = str(getattr(request, "path", "") or "")
        if path.split("?")[0].rstrip("/") not in {TASK_PATH, ""}:
            return connection.respond(HTTPStatus.NOT_FOUND, f"unknown path: {path}\n")
        return None

    def _authorised(self, connection: Any) -> bool:
        request = getattr(connection, "request", None)
        headers = getattr(request, "headers", None) if request is not None else None
        if headers is None:
            headers = getattr(connection, "request_headers", None)
        return token_matches(extract_token(headers), self.token)

    async def _run_one(self, connection: Any, message: Any) -> None:
        try:
            doc = json.loads(message)
            if not isinstance(doc, dict):
                raise ValueError("task packet must be a JSON object")
            task = task_from_json(doc)
        except Exception as exc:
            await connection.send(json.dumps({"ok": False, "error": f"bad packet: {exc}"}))
            return

        loop = asyncio.get_running_loop()

        def emit(body: dict[str, Any], seq: int, is_final: bool) -> None:
            """Called from the executor's thread; hop back onto the loop."""
            frame = json.dumps({**body, "task_id": task.id, "seq": seq, "is_final": is_final})
            asyncio.run_coroutine_threadsafe(connection.send(frame), loop)

        if self.metrics is not None:
            self.metrics.task_observed(task.id)
        # The executor blocks and this is the event loop, so run it on a worker.
        await loop.run_in_executor(None, self.execute, task, emit)

    # -- lifecycle ----------------------------------------------------------

    def start(self, timeout: float = 10.0) -> None:
        """Start the server on a private event loop in a background thread."""
        if not websockets_available():
            raise WsUnavailable(
                "ws.enabled is true but the 'websockets' package is missing — "
                "pip install 'iddo-harness[agentfabric]'"
            )
        if self._thread is not None:
            return

        self._thread = threading.Thread(target=self._serve_forever, name="ws-bridge", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("ws bridge failed to start")
        log.info("ws bridge listening on %s", self.url)

    def _serve_forever(self) -> None:
        import websockets

        async def main() -> None:
            self._loop = asyncio.get_running_loop()
            # Created inside the loop that will wait on it; stop() sets it from
            # the caller's thread via call_soon_threadsafe.
            self._shutdown = asyncio.Event()
            async with websockets.serve(
                self.handle, self.host, self.port, process_request=self.process_request
            ) as server:
                self._server = server
                self._ready.set()
                await self._shutdown.wait()

        try:
            asyncio.run(main())
        except (asyncio.CancelledError, KeyboardInterrupt):  # pragma: no cover
            pass
        except Exception:  # pragma: no cover
            log.exception("ws bridge crashed")
        finally:
            self._ready.set()

    def stop(self) -> None:
        """Close the listener and join the serving thread.

        The shutdown is a signal rather than ``loop.stop()``: stopping the loop
        outright returns from ``asyncio.run`` without unwinding
        ``websockets.serve``, which leaves the listening socket open and the
        port unusable until the process exits.
        """
        loop, self._loop = self._loop, None
        shutdown, self._shutdown = self._shutdown, None
        self._server = None
        if loop is not None and shutdown is not None:
            loop.call_soon_threadsafe(shutdown.set)
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._ready.clear()


def build_execute(executor: Any, reporter: Any = None) -> Callable[..., Any]:
    """Adapt an executor into the ``execute(task, emit)`` shape WsBridge wants.

    Chunks go to the socket *and*, when a reporter is supplied, to the bridge
    repo — so a task submitted over the socket still leaves the same audit
    trail in git as one that arrived through it.
    """

    def execute(task: Any, emit: Callable[[dict[str, Any], int, bool], None]) -> Any:
        seq = 0

        def sink(sink_task: Any, body: dict[str, Any], chunk_seq: int, is_final: bool) -> None:
            emit(body, chunk_seq, is_final)
            if reporter is not None:
                reporter.send_chunk(sink_task, body, chunk_seq, is_final)

        previous = getattr(executor, "chunk_sink", None)
        v2 = getattr(executor, "v2", None)
        targets: Iterable[Any] = [t for t in (executor, v2) if t is not None]
        for target in targets:
            try:
                target.chunk_sink = sink
            except Exception:  # pragma: no cover - read-only attribute
                pass
        try:
            result = executor.run(task)
        finally:
            for target in targets:
                try:
                    target.chunk_sink = previous
                except Exception:  # pragma: no cover
                    pass

        # Streaming kinds closed their own stream; everything else needs one
        # final frame so the consumer's is_final contract still holds.
        if not (getattr(result, "metadata", None) or {}).get("final_chunk"):
            from dataclasses import asdict, is_dataclass

            body = asdict(result) if is_dataclass(result) and not isinstance(result, type) else dict(result)
            emit(body, seq, True)
            if reporter is not None:
                reporter.send_chunk(task, body, seq, True)
        return result

    return execute


def _connection_path(connection: Any) -> str:
    request = getattr(connection, "request", None)
    if request is not None and getattr(request, "path", None):
        return str(request.path)
    return str(getattr(connection, "path", "") or "")

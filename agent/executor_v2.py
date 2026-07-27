"""
Executor v2 — the Agent Fabric verbs.

This module implements the 11 task kinds introduced by Iddo Harness v2
(see ``docs/v2_spec.md``):

===========================  ==========================================
kind                         what it does
===========================  ==========================================
``shell_stream``             run a command, stream stdout/stderr chunks
``shell_session_open``       start a long-lived interactive process
``shell_session_write``      send stdin to a live session, drain output
``shell_session_close``      terminate a live session
``grep``                     recursive regex search (pure Python)
``glob``                     recursive path matching, mtime-sorted
``patch_file``               atomic search/replace editing with backup
``watch_start``              begin watching a directory for changes
``watch_poll``               drain buffered filesystem events
``watch_stop``               stop a watch
``process_list``             enumerate running processes
``process_kill``             SIGTERM (then SIGKILL) a process
``http_local``               HTTP request from the *user's* machine
``read_file_chunked``        byte-exact paginated file reads
===========================  ==========================================

Nothing here touches ``agent/executor.py``'s v1 code path: ``Executor.run()``
routes v2 kinds to :class:`ExecutorV2` and leaves everything else alone, so
every v1 task packet keeps producing byte-identical results.

``psutil`` and ``watchdog`` are *optional*. When they are missing the process
and watch kinds fall back to ``ps``/``tasklist`` parsing and to an mtime
snapshot poller respectively, so a bare install still works.
"""
from __future__ import annotations

import fnmatch
import ipaddress
import json
import logging
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

try:
    from policy import Decision, PolicyEngine
except ImportError:  # pragma: no cover - packaged import
    from agent.policy import Decision, PolicyEngine

try:
    from confirm import ConfirmManager
except ImportError:  # pragma: no cover - packaged import
    from agent.confirm import ConfirmManager

try:
    from executor import Result
except ImportError:  # pragma: no cover - packaged import
    from agent.executor import Result

try:
    import sandbox as sandbox_mod
    import secrets_vault
except ImportError:  # pragma: no cover - packaged import
    from agent import sandbox as sandbox_mod
    from agent import secrets_vault

try:  # optional
    import psutil
except ImportError:  # pragma: no cover - exercised on bare installs
    psutil = None  # type: ignore[assignment]

try:  # optional
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - exercised on bare installs
    FileSystemEventHandler = None  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]

log = logging.getLogger("harness.executor_v2")


#: Every kind handled by this module. ``Executor.run()`` uses this set to
#: decide whether to delegate, so adding a kind here is all that is needed to
#: wire it into the v1 router.
V2_KINDS: frozenset[str] = frozenset(
    {
        "shell_stream",
        "shell_session_open",
        "shell_session_write",
        "shell_session_close",
        "grep",
        "glob",
        "patch_file",
        "watch_start",
        "watch_poll",
        "watch_stop",
        "process_list",
        "process_kill",
        "http_local",
        "read_file_chunked",
    }
)

#: Directories pruned during ``grep``/``glob`` walks unless overridden.
DEFAULT_PRUNE_DIRS: frozenset[str] = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}
)

_BINARY_SNIFF_BYTES = 8192
_DEFAULT_MAX_SESSIONS = 16
_DEFAULT_IDLE_TIMEOUT_SEC = 900.0
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# Live shell sessions
# ---------------------------------------------------------------------------
@dataclass
class ShellSession:
    """A long-lived interactive subprocess kept alive across task packets."""

    session_id: str
    command: str
    proc: subprocess.Popen
    idle_timeout_sec: float = _DEFAULT_IDLE_TIMEOUT_SEC
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    #: Secret values that were substituted into this session's command or into
    #: anything typed at it, kept so every drain can be scrubbed. A REPL echoes
    #: its input, so a credential typed once comes straight back out.
    secret_values: tuple[str, ...] = ()
    _stdout: list[str] = field(default_factory=list)
    _stderr: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _threads: list[threading.Thread] = field(default_factory=list)

    # -- reader plumbing ----------------------------------------------------
    def start_readers(self) -> None:
        """Spawn one daemon reader thread per piped stream."""
        for pipe, sink in ((self.proc.stdout, self._stdout), (self.proc.stderr, self._stderr)):
            if pipe is None:
                continue
            t = threading.Thread(target=self._pump, args=(pipe, sink), daemon=True)
            t.start()
            self._threads.append(t)

    def _pump(self, pipe: Any, sink: list[str]) -> None:
        """Append everything the pipe yields into ``sink`` until EOF."""
        try:
            while True:
                data = pipe.read(1)
                if not data:
                    break
                with self._lock:
                    sink.append(data)
        except (ValueError, OSError):  # pipe closed under us
            pass

    # -- public API ---------------------------------------------------------
    def drain(self) -> tuple[str, str]:
        """Remove and return everything buffered so far as ``(stdout, stderr)``."""
        with self._lock:
            out = "".join(self._stdout)
            err = "".join(self._stderr)
            self._stdout.clear()
            self._stderr.clear()
        return out, err

    def write(self, text: str) -> None:
        """Write ``text`` to the process's stdin and flush."""
        if self.proc.stdin is None:
            raise RuntimeError("session has no stdin")
        self.proc.stdin.write(text)
        self.proc.stdin.flush()
        self.last_activity = time.time()

    @property
    def alive(self) -> bool:
        """True while the underlying process has not exited."""
        return self.proc.poll() is None

    @property
    def idle_for(self) -> float:
        """Seconds elapsed since the last write."""
        return time.time() - self.last_activity

    def close(self) -> int | None:
        """Terminate, then (after 3 s) kill the process. Returns the exit code."""
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:  # pragma: no cover - process already reaped
                    pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # pragma: no cover
                pass
        return self.proc.poll()


# ---------------------------------------------------------------------------
# Filesystem watches
# ---------------------------------------------------------------------------
class FileWatch:
    """A directory watch with a bounded event buffer.

    Uses ``watchdog`` when it is importable and falls back to an mtime
    snapshot poller otherwise. Both backends produce the same event shape,
    so callers never need to know which one is running.
    """

    def __init__(
        self,
        watch_id: str,
        path: Path,
        recursive: bool = True,
        patterns: list[str] | None = None,
        max_buffer: int = 1000,
        poll_interval: float = 1.0,
        force_polling: bool = False,
    ) -> None:
        self.watch_id = watch_id
        self.path = path
        self.recursive = recursive
        self.patterns = patterns or ["*"]
        self.poll_interval = poll_interval
        self.events: deque[dict[str, Any]] = deque(maxlen=max_buffer)
        self.dropped = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._observer: Any = None
        self._thread: threading.Thread | None = None
        self.backend = "polling" if (force_polling or Observer is None) else "watchdog"

    # -----------------------------------------------------------------------
    def matches(self, path: str) -> bool:
        """True when ``path``'s file name matches any configured pattern."""
        name = os.path.basename(path)
        return any(fnmatch.fnmatch(name, pat) for pat in self.patterns)

    def record(self, event_type: str, path: str, dest: str | None = None) -> None:
        """Buffer one event, counting an overflow as a drop."""
        if not self.matches(path):
            return
        with self._lock:
            if len(self.events) == self.events.maxlen:
                self.dropped += 1
            evt: dict[str, Any] = {"ts": _now_iso(), "type": event_type, "path": path}
            if dest is not None:
                evt["dest_path"] = dest
            self.events.append(evt)

    def drain(self, max_events: int) -> tuple[list[dict[str, Any]], int]:
        """Pop up to ``max_events`` buffered events plus the drop counter."""
        with self._lock:
            out = [self.events.popleft() for _ in range(min(max_events, len(self.events)))]
            dropped, self.dropped = self.dropped, 0
        return out, dropped

    # -----------------------------------------------------------------------
    def start(self) -> None:
        """Begin watching with whichever backend is available."""
        if self.backend == "watchdog":
            handler = _WatchdogBridge(self)
            self._observer = Observer()
            self._observer.schedule(handler, str(self.path), recursive=self.recursive)
            self._observer.daemon = True
            self._observer.start()
        else:
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the backend and release its thread."""
        self._stop.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:  # pragma: no cover
                pass
            self._observer = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # -----------------------------------------------------------------------
    def _snapshot(self) -> dict[str, float]:
        """Map every watched file path to its mtime."""
        snap: dict[str, float] = {}
        try:
            it = self.path.rglob("*") if self.recursive else self.path.glob("*")
            for p in it:
                try:
                    if p.is_file():
                        snap[str(p)] = p.stat().st_mtime
                except OSError:
                    continue
        except OSError:  # pragma: no cover - watched dir vanished
            pass
        return snap

    def _poll_loop(self) -> None:
        """Diff successive mtime snapshots into created/modified/deleted events."""
        prev = self._snapshot()
        while not self._stop.wait(self.poll_interval):
            cur = self._snapshot()
            for path, mtime in cur.items():
                if path not in prev:
                    self.record("created", path)
                elif prev[path] != mtime:
                    self.record("modified", path)
            for path in prev:
                if path not in cur:
                    self.record("deleted", path)
            prev = cur


if FileSystemEventHandler is not None:  # pragma: no cover - requires watchdog

    class _WatchdogBridge(FileSystemEventHandler):  # type: ignore[misc,valid-type]
        """Translate watchdog callbacks into :class:`FileWatch` events."""

        def __init__(self, watch: FileWatch) -> None:
            super().__init__()
            self._watch = watch

        def on_any_event(self, event: Any) -> None:
            if getattr(event, "is_directory", False):
                return
            self._watch.record(event.event_type, event.src_path, getattr(event, "dest_path", None))

else:  # pragma: no cover - bare install

    class _WatchdogBridge:  # type: ignore[no-redef]
        """Placeholder used when watchdog is not installed."""

        def __init__(self, watch: FileWatch) -> None:
            raise RuntimeError("watchdog is not installed")


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------
class ExecutorV2:
    """Runs the v2 Agent Fabric kinds, subject to the same policy engine as v1.

    ``chunk_sink`` receives ``(task, body, seq, is_final)`` for every streamed
    chunk. In production this is :meth:`agent.reporter_v2.ReporterV2.send_chunk`;
    in tests it is a list appender. When it is ``None`` chunks are buffered and
    concatenated into the returned :class:`Result` instead, so ``shell_stream``
    degrades to a (still correct) blocking call.
    """

    def __init__(
        self,
        policy: PolicyEngine,
        confirm_manager: ConfirmManager | None = None,
        chunk_sink: Callable[[Any, dict[str, Any], int, bool], None] | None = None,
        max_sessions: int = _DEFAULT_MAX_SESSIONS,
        vault: Any | None = None,
        audit_log: Any | None = None,
    ) -> None:
        self.policy = policy
        self.confirm_manager = confirm_manager or ConfirmManager(getattr(policy, "cfg", None))
        self.chunk_sink = chunk_sink
        self.max_sessions = max_sessions
        # v3 Track B: secret resolution and sandboxing. Both are opt-in —
        # `vault` is None on installs that never ran `installer.gen_keys`, and
        # the sandbox level comes from policy.yaml and defaults to "none".
        self.vault = vault
        self.audit_log = audit_log
        self.sessions: dict[str, ShellSession] = {}
        self.watches: dict[str, FileWatch] = {}
        self._seq: dict[str, int] = {}

    # -----------------------------------------------------------------------
    # Dispatch
    # -----------------------------------------------------------------------
    def handles(self, kind: str) -> bool:
        """True when ``kind`` is one of the v2 Agent Fabric kinds."""
        return kind in V2_KINDS

    def run(self, task: Any) -> Result:
        """Policy-gate and execute ``task``, returning a v1-shaped Result."""
        handler = self._gated_handlers().get(task.kind)
        if handler is None:
            return Result(task_id=task.id, ok=False, decision="unknown_kind", error=f"unknown kind: {task.kind}")
        try:
            return handler(task)
        except Exception as e:  # never let a v2 kind take the poller down
            log.exception(f"task={task.id} kind={task.kind} raised")
            return Result(task_id=task.id, ok=False, decision="error", error=f"{type(e).__name__}: {e}")

    def resume_after_confirm(self, task: Any, approved: bool) -> Result:
        """Execute a previously parked task once a human has answered."""
        if not approved:
            return Result(task_id=task.id, ok=False, decision="denied", error="confirmation denied by user")
        handler = self._exec_handlers().get(task.kind)
        if handler is None:
            return Result(task_id=task.id, ok=False, decision="unknown_kind", error=f"unknown kind: {task.kind}")
        try:
            return handler(task, "approved")
        except Exception as e:
            log.exception(f"task={task.id} kind={task.kind} raised during resume")
            return Result(task_id=task.id, ok=False, decision="approved", error=f"{type(e).__name__}: {e}")

    def _gated_handlers(self) -> dict[str, Callable[[Any], Result]]:
        """Map each kind to its policy-checking entry point."""
        return {
            "shell_stream": self._gate_shell_stream,
            "shell_session_open": self._gate_session_open,
            "shell_session_write": self._gate_session_write,
            "shell_session_close": self._session_close,
            "grep": self._gate_grep,
            "glob": self._gate_glob,
            "patch_file": self._gate_patch_file,
            "watch_start": self._gate_watch_start,
            "watch_poll": self._watch_poll,
            "watch_stop": self._watch_stop,
            "process_list": self._gate_process_list,
            "process_kill": self._gate_process_kill,
            "http_local": self._gate_http_local,
            "read_file_chunked": self._gate_read_chunked,
        }

    def _exec_handlers(self) -> dict[str, Callable[[Any, str], Result]]:
        """Map each kind to its post-approval executor (policy already cleared)."""
        return {
            "shell_stream": self._exec_shell_stream,
            "shell_session_open": self._exec_session_open,
            "shell_session_write": self._exec_session_write,
            "shell_session_close": lambda t, d: self._session_close(t),
            "grep": self._exec_grep,
            "glob": self._exec_glob,
            "patch_file": self._exec_patch_file,
            "watch_start": self._exec_watch_start,
            "watch_poll": lambda t, d: self._watch_poll(t),
            "watch_stop": lambda t, d: self._watch_stop(t),
            "process_list": self._exec_process_list,
            "process_kill": self._exec_process_kill,
            "http_local": self._exec_http_local,
            "read_file_chunked": self._exec_read_chunked,
        }

    # -----------------------------------------------------------------------
    # Policy / chunk helpers
    # -----------------------------------------------------------------------
    def _gate(self, task: Any, command: str, paths: list[str] | None = None) -> tuple[Decision, str, Result | None]:
        """Run the policy engine, returning an early Result for block/confirm."""
        decision, reason = self.policy.decide(command, paths or [])
        self.policy.audit(task.id, command, decision, reason)
        if decision is Decision.BLOCK:
            return decision, reason, Result(task_id=task.id, ok=False, decision="block", error=reason)
        if decision is Decision.CONFIRM:
            pc = self.confirm_manager.create(task, reason)
            return decision, reason, Result(
                task_id=task.id,
                ok=False,
                decision="confirm_required",
                error=reason,
                metadata={"message": getattr(pc, "message", reason)},
            )
        return decision, reason, None

    # -----------------------------------------------------------------------
    # Secrets / sandbox helpers (docs/security_v3.md §2-3)
    # -----------------------------------------------------------------------
    def _resolve_secrets(self, text: str) -> tuple[str, tuple[str, ...]]:
        """Substitute ``{{secret:name}}`` and return the resolved values.

        The values come back so the caller can scrub them out of whatever the
        command prints — a resolved credential that reaches ``results/`` is
        committed to git history and effectively public.
        """
        resolved, used = secrets_vault.resolve_with(self.vault, text)
        return resolved, tuple(used.values())

    def _resolve_secrets_in(self, value: Any) -> tuple[Any, tuple[str, ...]]:
        """:meth:`_resolve_secrets` for nested headers/bodies."""
        if self.vault is None or not self.vault.available:
            return value, ()
        resolved, used = self.vault.resolve_structure(value)
        return resolved, tuple(used.values())

    @staticmethod
    def _scrub(text: str, values: Iterable[str] = ()) -> str:
        """Redact secret values out of command output."""
        return secrets_vault.redact(text, values)

    def _sandbox_level(self, kind: str) -> str:
        """Sandbox level configured for ``kind`` in policy.yaml."""
        return sandbox_mod.level_for_kind(getattr(self.policy, "cfg", None), kind)

    def _audit(self, actor: str, action: str, resource: str = "", outcome: str = "ok", **meta: Any) -> None:
        """Append one audit record when an audit log is wired up."""
        if self.audit_log is None:
            return
        try:
            self.audit_log.record(actor=actor, action=action, resource=resource, outcome=outcome, meta=meta)
        except Exception:  # pragma: no cover - auditing must never break a task
            log.exception(f"audit failed for {action}")

    def _next_seq(self, task_id: str) -> int:
        """Return the next monotonically increasing chunk sequence number."""
        seq = self._seq.get(task_id, 0)
        self._seq[task_id] = seq + 1
        return seq

    def _emit(self, task: Any, body: dict[str, Any], is_final: bool = False) -> dict[str, Any]:
        """Send one chunk to the sink (when configured) and return the body."""
        seq = self._next_seq(task.id)
        body = {"task_id": task.id, "seq": seq, "is_final": is_final, **body}
        if self.chunk_sink is not None:
            try:
                self.chunk_sink(task, body, seq, is_final)
            except Exception:  # a failing transport must not kill the command
                log.exception(f"task={task.id} chunk_sink failed at seq={seq}")
        return body

    # -----------------------------------------------------------------------
    # 1. shell_stream
    # -----------------------------------------------------------------------
    def _gate_shell_stream(self, task: Any) -> Result:
        command = task.payload.get("command", "")
        decision, _reason, early = self._gate(task, command, task.payload.get("paths", []))
        return early if early is not None else self._exec_shell_stream(task, decision.value)

    def _exec_shell_stream(self, task: Any, decision: str = "auto") -> Result:
        """Run a command, emitting output chunks as the process produces them."""
        command = task.payload.get("command", "")
        cwd = task.payload.get("cwd") or None
        timeout = float(task.payload.get("timeout_sec", 600))
        flush_interval = float(task.payload.get("flush_interval_ms", 500)) / 1000.0
        max_chunk = int(task.payload.get("max_chunk_bytes", 16000))

        try:
            command, secret_values = self._resolve_secrets(command)
        except secrets_vault.VaultError as e:
            self._audit(task.id, "secret_resolve", "shell_stream", "error", error=str(e))
            body = self._emit(task, {"ok": False, "decision": decision, "error": str(e)}, is_final=True)
            return Result(task_id=task.id, ok=False, decision=decision, error=str(e), metadata={"final_chunk": body})

        level = self._sandbox_level(task.kind)
        launch = sandbox_mod.wrap_popen_args(command, level, cwd)

        try:
            proc = subprocess.Popen(
                launch,
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            body = self._emit(task, {"ok": False, "decision": decision, "error": str(e)}, is_final=True)
            return Result(task_id=task.id, ok=False, decision=decision, error=str(e), metadata={"final_chunk": body})

        q: queue.Queue[tuple[str, str] | None] = queue.Queue()
        readers = [
            threading.Thread(target=_pump_lines, args=(proc.stdout, "stdout", q), daemon=True),
            threading.Thread(target=_pump_lines, args=(proc.stderr, "stderr", q), daemon=True),
        ]
        for t in readers:
            t.start()

        buffers = {"stdout": "", "stderr": ""}
        totals = {"stdout": 0, "stderr": 0}
        collected: list[str] = []
        collected_err: list[str] = []
        chunks = 0
        open_readers = len(readers)
        deadline = time.time() + timeout
        last_flush = time.time()
        timed_out = False

        def flush(stream: str, force: bool = False) -> None:
            nonlocal chunks
            text = buffers[stream]
            if not text:
                return
            if not force and "\n" not in text and len(text) < max_chunk and (time.time() - last_flush) < flush_interval:
                return
            buffers[stream] = ""
            # Scrub before the chunk leaves the process: a command that echoes
            # its own arguments would otherwise commit the credential to git.
            text = self._scrub(text, secret_values)
            totals[stream] += len(text)
            (collected if stream == "stdout" else collected_err).append(text)
            self._emit(task, {"stream": stream, "text": text})
            chunks += 1

        while open_readers > 0:
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                item = q.get(timeout=min(flush_interval, max(remaining, 0.01)))
            except queue.Empty:
                for stream in ("stdout", "stderr"):
                    flush(stream, force=True)
                last_flush = time.time()
                continue
            if item is None:
                open_readers -= 1
                continue
            stream, text = item
            buffers[stream] += text
            if "\n" in buffers[stream] or len(buffers[stream]) >= max_chunk:
                flush(stream, force=True)
                last_flush = time.time()

        for stream in ("stdout", "stderr"):
            flush(stream, force=True)

        if timed_out:
            try:
                proc.kill()
            except Exception:  # pragma: no cover
                pass
            exit_code: int | None = None
        else:
            try:
                exit_code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - reader EOF without exit
                proc.kill()
                exit_code = None

        ok = (not timed_out) and exit_code == 0
        final = {
            "ok": ok,
            "decision": decision,
            "exit_code": exit_code,
            "stats": {"chunks": chunks, "stdout_bytes": totals["stdout"], "stderr_bytes": totals["stderr"]},
        }
        if timed_out:
            final["error"] = "timeout"
        body = self._emit(task, final, is_final=True)

        return Result(
            task_id=task.id,
            ok=ok,
            decision=decision,
            stdout="".join(collected)[-200_000:],
            stderr="".join(collected_err)[-16_000:],
            exit_code=exit_code,
            error="timeout" if timed_out else "",
            metadata={"streamed": True, "chunks": chunks, "sandbox": level, "final_chunk": body},
        )

    # -----------------------------------------------------------------------
    # 2-4. shell sessions
    # -----------------------------------------------------------------------
    def _reap_sessions(self) -> list[str]:
        """Close sessions that exited or idled past their timeout."""
        dead = [
            sid
            for sid, s in self.sessions.items()
            if not s.alive or s.idle_for > s.idle_timeout_sec
        ]
        for sid in dead:
            try:
                self.sessions[sid].close()
            except Exception:  # pragma: no cover
                pass
            del self.sessions[sid]
            log.info(f"session {sid} reaped")
        return dead

    def _gate_session_open(self, task: Any) -> Result:
        command = task.payload.get("command") or _default_shell()
        decision, _reason, early = self._gate(task, command, task.payload.get("paths", []))
        return early if early is not None else self._exec_session_open(task, decision.value)

    def _exec_session_open(self, task: Any, decision: str = "auto") -> Result:
        """Start an interactive process and register it under a new session id."""
        self._reap_sessions()
        if len(self.sessions) >= self.max_sessions:
            return Result(
                task_id=task.id, ok=False, decision=decision,
                error=f"session limit reached ({self.max_sessions})",
            )

        # `command` stays the un-substituted template everywhere it is stored or
        # reported; only `launch` carries real credentials, and it dies with the
        # Popen call. Session metadata is published to results/, so a resolved
        # command must never be written onto the session object.
        command = task.payload.get("command") or _default_shell()
        cwd = task.payload.get("cwd") or None
        try:
            resolved, secret_values = self._resolve_secrets(command)
        except secrets_vault.VaultError as e:
            return Result(task_id=task.id, ok=False, decision=decision, error=str(e))
        level = self._sandbox_level(task.kind)
        proc = subprocess.Popen(
            sandbox_mod.wrap_popen_args(resolved, level, cwd),
            shell=True,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        session = ShellSession(
            session_id=f"s-{uuid.uuid4().hex[:12]}",
            command=command,
            proc=proc,
            idle_timeout_sec=float(task.payload.get("idle_timeout_sec", _DEFAULT_IDLE_TIMEOUT_SEC)),
        )
        session.secret_values = secret_values
        session.start_readers()
        self.sessions[session.session_id] = session
        meta = {"session_id": session.session_id, "pid": proc.pid, "command": command, "sandbox": level}
        log.info(f"session {session.session_id} opened pid={proc.pid}")
        return Result(task_id=task.id, ok=True, decision=decision, stdout=session.session_id, metadata=meta)

    def _gate_session_write(self, task: Any) -> Result:
        """Gate a session write against the *block* rules only.

        Opening the session already cleared its command through the full
        auto/confirm/block ladder, and that grant covers typing into it —
        otherwise every line of a `python -i` transcript would park a
        confirmation. Block patterns still apply, so `rm -rf /` cannot sneak
        in through a live REPL.
        """
        line = task.payload.get("input", "").strip()
        decision, reason = self.policy.decide(line, task.payload.get("paths", []))
        self.policy.audit(task.id, f"session_write {line!r}", decision, reason)
        if decision is Decision.BLOCK:
            return Result(task_id=task.id, ok=False, decision="block", error=reason)
        return self._exec_session_write(task, "auto")

    def _exec_session_write(self, task: Any, decision: str = "auto") -> Result:
        """Write stdin into a live session and drain whatever it emits."""
        self._reap_sessions()
        sid = task.payload.get("session_id", "")
        session = self.sessions.get(sid)
        if session is None:
            return Result(task_id=task.id, ok=False, decision=decision, error=f"no such session: {sid}")

        text = task.payload.get("input", "")
        try:
            text, used = self._resolve_secrets(text)
        except secrets_vault.VaultError as e:
            return Result(task_id=task.id, ok=False, decision=decision, error=str(e))
        if used:
            session.secret_values = tuple({*session.secret_values, *used})
        if text and not text.endswith("\n"):
            text += "\n"
        read_timeout = float(task.payload.get("read_timeout_sec", 2.0))

        try:
            if text:
                session.write(text)
        except Exception as e:
            return Result(task_id=task.id, ok=False, decision=decision, error=f"write failed: {e}")

        # Drain window: stop early once output has arrived and gone quiet.
        out_parts, err_parts = [], []
        deadline = time.time() + read_timeout
        quiet_since: float | None = None
        while time.time() < deadline:
            o, e = session.drain()
            if o or e:
                out_parts.append(o)
                err_parts.append(e)
                quiet_since = None
            elif out_parts or err_parts:
                quiet_since = quiet_since or time.time()
                if time.time() - quiet_since > 0.15:
                    break
            time.sleep(0.02)
        o, e = session.drain()
        out_parts.append(o)
        err_parts.append(e)

        stdout = self._scrub("".join(out_parts), session.secret_values)
        stderr = self._scrub("".join(err_parts), session.secret_values)
        session.last_activity = time.time()
        return Result(
            task_id=task.id, ok=True, decision=decision, stdout=stdout, stderr=stderr,
            metadata={"session_id": sid, "alive": session.alive},
        )

    def _session_close(self, task: Any) -> Result:
        """Terminate a session and drop it from the registry."""
        sid = task.payload.get("session_id", "")
        session = self.sessions.pop(sid, None)
        if session is None:
            return Result(task_id=task.id, ok=False, decision="auto", error=f"no such session: {sid}")
        stdout, stderr = session.drain()
        exit_code = session.close()
        stdout = self._scrub(stdout, session.secret_values)
        stderr = self._scrub(stderr, session.secret_values)
        return Result(
            task_id=task.id, ok=True, decision="auto", stdout=stdout, stderr=stderr, exit_code=exit_code,
            metadata={"session_id": sid, "closed": True, "uptime_sec": round(time.time() - session.created_at, 3)},
        )

    def shutdown(self) -> None:
        """Close every session and watch. Called on agent shutdown."""
        for sid in list(self.sessions):
            try:
                self.sessions.pop(sid).close()
            except Exception:  # pragma: no cover
                pass
        for wid in list(self.watches):
            try:
                self.watches.pop(wid).stop()
            except Exception:  # pragma: no cover
                pass

    # -----------------------------------------------------------------------
    # 5. grep
    # -----------------------------------------------------------------------
    def _gate_grep(self, task: Any) -> Result:
        path = str(Path(task.payload.get("path", ".")).expanduser())
        decision, _reason, early = self._gate(task, f"grep {path}", [path])
        return early if early is not None else self._exec_grep(task, decision.value)

    def _exec_grep(self, task: Any, decision: str = "auto") -> Result:
        """Walk a tree and collect regex matches line by line."""
        p = task.payload
        root = Path(p.get("path", ".")).expanduser()
        if not root.exists():
            return Result(task_id=task.id, ok=False, decision=decision, error=f"path not found: {root}")

        flags = re.IGNORECASE if p.get("ignore_case") else 0
        try:
            rx = re.compile(p.get("pattern", ""), flags)
        except re.error as e:
            return Result(task_id=task.id, ok=False, decision=decision, error=f"bad regex: {e}")

        include = p.get("include", "*")
        max_results = int(p.get("max_results", 500))
        max_file_bytes = int(p.get("max_file_bytes", 5_000_000))
        context = int(p.get("context", 0))
        include_hidden = bool(p.get("include_hidden", False))

        matches: list[dict[str, Any]] = []
        files_scanned = 0
        truncated = False

        for fp in _walk_files(root, include, include_hidden):
            if truncated:
                break
            try:
                if fp.stat().st_size > max_file_bytes:
                    continue
                raw = fp.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
                continue
            files_scanned += 1
            lines = raw.decode("utf-8", errors="replace").splitlines()
            for i, line in enumerate(lines):
                if not rx.search(line):
                    continue
                entry: dict[str, Any] = {"path": str(fp), "line_no": i + 1, "line": line[:2000]}
                if context:
                    entry["before"] = lines[max(0, i - context): i]
                    entry["after"] = lines[i + 1: i + 1 + context]
                matches.append(entry)
                if len(matches) >= max_results:
                    truncated = True
                    break

        body = {"matches": matches, "count": len(matches), "files_scanned": files_scanned, "truncated": truncated}
        stdout = "\n".join(f"{m['path']}:{m['line_no']}: {m['line']}" for m in matches)
        return Result(task_id=task.id, ok=True, decision=decision, stdout=stdout, metadata=body)

    # -----------------------------------------------------------------------
    # 6. glob
    # -----------------------------------------------------------------------
    def _gate_glob(self, task: Any) -> Result:
        path = str(Path(task.payload.get("path", ".")).expanduser())
        decision, _reason, early = self._gate(task, f"glob {path}", [path])
        return early if early is not None else self._exec_glob(task, decision.value)

    def _exec_glob(self, task: Any, decision: str = "auto") -> Result:
        """Expand a glob pattern under a root, newest match first."""
        p = task.payload
        root = Path(p.get("path", ".")).expanduser()
        if not root.exists():
            return Result(task_id=task.id, ok=False, decision=decision, error=f"path not found: {root}")

        pattern = p.get("pattern", "*")
        recursive = bool(p.get("recursive", True))
        files_only = bool(p.get("files_only", True))
        max_results = int(p.get("max_results", 1000))

        # `rglob` already implies a leading `**/`, so only add it when the
        # caller has not written their own `**` into the pattern.
        if recursive and "**" not in pattern:
            it: Iterable[Path] = root.rglob(pattern)
        else:
            it = root.glob(pattern)

        found: list[tuple[float, Path]] = []
        for hit in it:
            try:
                if files_only and not hit.is_file():
                    continue
                found.append((hit.stat().st_mtime, hit))
            except OSError:
                continue

        found.sort(key=lambda t: t[0], reverse=True)
        truncated = len(found) > max_results
        paths = [str(hit) for _mtime, hit in found[:max_results]]
        body = {"paths": paths, "count": len(paths), "truncated": truncated, "pattern": pattern, "root": str(root)}
        return Result(task_id=task.id, ok=True, decision=decision, stdout="\n".join(paths), metadata=body)

    # -----------------------------------------------------------------------
    # 7. patch_file
    # -----------------------------------------------------------------------
    def _gate_patch_file(self, task: Any) -> Result:
        path = str(Path(task.payload.get("path", "")).expanduser())
        decision, _reason, early = self._gate(task, f"patch {path}", [path])
        return early if early is not None else self._exec_patch_file(task, decision.value)

    def _exec_patch_file(self, task: Any, decision: str = "auto") -> Result:
        """Apply search/replace edits atomically, with a timestamped backup."""
        p = task.payload
        path = Path(p.get("path", "")).expanduser()
        if not path.is_file():
            return Result(task_id=task.id, ok=False, decision=decision, error=f"file not found: {path}")

        edits = p.get("edits")
        if not edits:
            edits = [{
                "old_string": p.get("old_string", ""),
                "new_string": p.get("new_string", ""),
                "replace_all": bool(p.get("replace_all", False)),
            }]
        if not isinstance(edits, list):
            return Result(task_id=task.id, ok=False, decision=decision, error="edits must be a list")

        original = path.read_text(encoding="utf-8", errors="replace")
        content = original
        replacements = 0

        # Validate + apply against an in-memory copy first: if any edit fails,
        # nothing reaches disk.
        for i, edit in enumerate(edits):
            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            replace_all = bool(edit.get("replace_all", False))
            if not old:
                return Result(task_id=task.id, ok=False, decision=decision, error=f"edit[{i}]: old_string is empty")
            if old == new:
                return Result(task_id=task.id, ok=False, decision=decision, error=f"edit[{i}]: old_string == new_string (no-op)")
            occurrences = content.count(old)
            if occurrences == 0:
                return Result(
                    task_id=task.id, ok=False, decision=decision,
                    error=f"edit[{i}]: old_string not found",
                    metadata={"edit_index": i, "occurrences": 0},
                )
            if occurrences > 1 and not replace_all:
                return Result(
                    task_id=task.id, ok=False, decision=decision,
                    error=f"edit[{i}]: old_string is not unique ({occurrences} occurrences); pass replace_all=true to replace them all",
                    metadata={"edit_index": i, "occurrences": occurrences},
                )
            content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
            replacements += occurrences if replace_all else 1

        diff = "".join(
            unified_diff(
                original.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{path.name}",
                tofile=f"b/{path.name}",
            )
        )
        body: dict[str, Any] = {
            "path": str(path), "applied": len(edits), "replacements": replacements,
            "diff": diff[:200_000], "bytes_before": len(original.encode("utf-8")),
            "bytes_after": len(content.encode("utf-8")), "backup_path": None,
            "dry_run": bool(p.get("dry_run", False)),
        }

        if p.get("dry_run"):
            return Result(task_id=task.id, ok=True, decision=decision, stdout=diff[:200_000], metadata=body)

        if p.get("backup", True):
            backup = path.with_name(f"{path.name}.bak-{datetime.now().strftime('%Y%m%dT%H%M%S')}")
            shutil.copy2(path, backup)
            body["backup_path"] = str(backup)

        path.write_text(content, encoding="utf-8")
        log.info(f"patched {path}: {len(edits)} edit(s), {replacements} replacement(s)")
        return Result(task_id=task.id, ok=True, decision=decision, stdout=diff[:200_000], metadata=body)

    # -----------------------------------------------------------------------
    # 8. watch_start / watch_poll / watch_stop
    # -----------------------------------------------------------------------
    def _gate_watch_start(self, task: Any) -> Result:
        path = str(Path(task.payload.get("path", ".")).expanduser())
        decision, _reason, early = self._gate(task, f"watch_start {path}", [path])
        return early if early is not None else self._exec_watch_start(task, decision.value)

    def _exec_watch_start(self, task: Any, decision: str = "auto") -> Result:
        """Begin buffering filesystem events for a directory."""
        p = task.payload
        path = Path(p.get("path", ".")).expanduser()
        if not path.is_dir():
            return Result(task_id=task.id, ok=False, decision=decision, error=f"not a directory: {path}")

        watch = FileWatch(
            watch_id=f"w-{uuid.uuid4().hex[:12]}",
            path=path,
            recursive=bool(p.get("recursive", True)),
            patterns=p.get("patterns") or ["*"],
            max_buffer=int(p.get("max_buffer", 1000)),
            poll_interval=float(p.get("poll_interval_sec", 1.0)),
            force_polling=bool(p.get("force_polling", False)),
        )
        watch.start()
        self.watches[watch.watch_id] = watch
        meta = {"watch_id": watch.watch_id, "backend": watch.backend, "path": str(path), "recursive": watch.recursive}
        return Result(task_id=task.id, ok=True, decision=decision, stdout=watch.watch_id, metadata=meta)

    def _watch_poll(self, task: Any) -> Result:
        """Drain buffered events for a watch."""
        wid = task.payload.get("watch_id", "")
        watch = self.watches.get(wid)
        if watch is None:
            return Result(task_id=task.id, ok=False, decision="auto", error=f"no such watch: {wid}")
        events, dropped = watch.drain(int(task.payload.get("max_events", 200)))
        body = {"watch_id": wid, "events": events, "count": len(events), "dropped": dropped, "alive": True}
        return Result(
            task_id=task.id, ok=True, decision="auto",
            stdout="\n".join(f"{e['type']} {e['path']}" for e in events), metadata=body,
        )

    def _watch_stop(self, task: Any) -> Result:
        """Stop a watch and discard its buffer."""
        wid = task.payload.get("watch_id", "")
        watch = self.watches.pop(wid, None)
        if watch is None:
            return Result(task_id=task.id, ok=False, decision="auto", error=f"no such watch: {wid}")
        watch.stop()
        return Result(task_id=task.id, ok=True, decision="auto", metadata={"watch_id": wid, "stopped": True})

    # -----------------------------------------------------------------------
    # 9. process_list / process_kill
    # -----------------------------------------------------------------------
    def _gate_process_list(self, task: Any) -> Result:
        decision, _reason, early = self._gate(task, "process_list", [])
        return early if early is not None else self._exec_process_list(task, decision.value)

    def _exec_process_list(self, task: Any, decision: str = "auto") -> Result:
        """Enumerate processes via psutil, falling back to ps/tasklist."""
        p = task.payload
        needle = (p.get("filter") or "").lower()
        limit = int(p.get("limit", 100))
        sort_by = p.get("sort_by", "memory")

        procs, backend = (_list_processes_psutil() if psutil is not None else _list_processes_fallback())
        if needle:
            procs = [
                pr for pr in procs
                if needle in str(pr.get("name", "")).lower() or needle in str(pr.get("cmdline", "")).lower()
            ]
        key = {"memory": "memory_mb", "cpu": "cpu_percent", "pid": "pid"}.get(sort_by, "memory_mb")
        procs.sort(key=lambda pr: pr.get(key) or 0, reverse=(key != "pid"))

        truncated = len(procs) > limit
        procs = procs[:limit]
        body = {"processes": procs, "count": len(procs), "backend": backend, "truncated": truncated}
        stdout = "\n".join(f"{pr['pid']:>8}  {pr.get('name', '')}" for pr in procs)
        return Result(task_id=task.id, ok=True, decision=decision, stdout=stdout, metadata=body)

    def _gate_process_kill(self, task: Any) -> Result:
        pid = task.payload.get("pid")
        decision, _reason, early = self._gate(task, f"process_kill {pid}", [])
        return early if early is not None else self._exec_process_kill(task, decision.value)

    def _exec_process_kill(self, task: Any, decision: str = "auto") -> Result:
        """SIGTERM a process, escalating to SIGKILL when it refuses to die."""
        p = task.payload
        try:
            pid = int(p.get("pid"))
        except (TypeError, ValueError):
            return Result(task_id=task.id, ok=False, decision=decision, error="pid must be an integer")

        if pid <= 1:
            return Result(task_id=task.id, ok=False, decision=decision, error=f"refusing to kill pid {pid}")
        if pid == os.getpid():
            return Result(task_id=task.id, ok=False, decision=decision, error="refusing to kill the harness itself")

        force = bool(p.get("force", False))
        timeout = float(p.get("timeout_sec", 5))
        name = ""

        if psutil is not None:
            try:
                proc = psutil.Process(pid)
                name = proc.name()
            except psutil.NoSuchProcess:
                return Result(task_id=task.id, ok=False, decision=decision, error=f"no such process: {pid}")
            except psutil.AccessDenied:
                return Result(task_id=task.id, ok=False, decision=decision, error=f"access denied for pid {pid}")
            try:
                if force:
                    proc.kill()
                    sig = "SIGKILL"
                else:
                    proc.terminate()
                    sig = "SIGTERM"
                    try:
                        proc.wait(timeout=timeout)
                    except psutil.TimeoutExpired:
                        proc.kill()
                        sig = "SIGKILL"
                killed = not proc.is_running()
            except psutil.AccessDenied:
                return Result(task_id=task.id, ok=False, decision=decision, error=f"access denied for pid {pid}")
        else:
            sig = "SIGKILL" if force else "SIGTERM"
            signum = getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                return Result(task_id=task.id, ok=False, decision=decision, error=f"no such process: {pid}")
            except PermissionError:
                return Result(task_id=task.id, ok=False, decision=decision, error=f"access denied for pid {pid}")
            killed = True

        body = {"pid": pid, "name": name, "signal": sig, "killed": killed}
        log.info(f"process_kill pid={pid} name={name} signal={sig} killed={killed}")
        self._audit(
            task.id, "process_kill", f"pid:{pid}", "ok" if killed else "error",
            name=name, signal=sig,
        )
        return Result(task_id=task.id, ok=killed, decision=decision, metadata=body)

    # -----------------------------------------------------------------------
    # 10. http_local
    # -----------------------------------------------------------------------
    def _gate_http_local(self, task: Any) -> Result:
        url = task.payload.get("url", "")
        method = (task.payload.get("method") or "GET").upper()
        decision, _reason, early = self._gate(task, f"http_local {method} {url}", [])
        return early if early is not None else self._exec_http_local(task, decision.value)

    def _exec_http_local(self, task: Any, decision: str = "auto") -> Result:
        """Make an HTTP request from the user's machine to a local/LAN host."""
        p = task.payload
        url = p.get("url", "")
        method = (p.get("method") or "GET").upper()
        if method not in _HTTP_METHODS:
            return Result(task_id=task.id, ok=False, decision=decision, error=f"method not allowed: {method}")

        ok_local, why = _assert_local_url(url)
        if not ok_local:
            return Result(task_id=task.id, ok=False, decision=decision, error=why)

        # Credentials belong in a header, so this is the one v2 kind where the
        # secret is in a nested field rather than a command line. `headers` here
        # is the resolved copy sent on the wire; the response is scrubbed below,
        # and the request headers are never echoed into the result.
        headers = {str(k): str(v) for k, v in (p.get("headers") or {}).items()}
        try:
            headers, header_secrets = self._resolve_secrets_in(headers)
            body_secrets: tuple[str, ...] = ()
            if isinstance(p.get("body"), (str, dict, list)):
                resolved_body, body_secrets = self._resolve_secrets_in(p.get("body"))
                p = {**p, "body": resolved_body}
        except secrets_vault.VaultError as e:
            return Result(task_id=task.id, ok=False, decision=decision, error=str(e))
        secret_values = tuple({*header_secrets, *body_secrets})

        body_in = p.get("body")
        data: bytes | None = None
        if body_in is not None:
            data = body_in.encode("utf-8") if isinstance(body_in, str) else json.dumps(body_in).encode("utf-8")
            headers.setdefault("Content-Type", "application/json" if not isinstance(body_in, str) else "text/plain")

        max_bytes = int(p.get("max_bytes", 1_000_000))
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=float(p.get("timeout_sec", 30))) as resp:
                raw = resp.read(max_bytes + 1)
                status, resp_headers = resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:  # a 4xx/5xx is still a real response
            raw = e.read(max_bytes + 1)
            status, resp_headers = e.code, dict(e.headers or {})
        except Exception as e:
            return Result(task_id=task.id, ok=False, decision=decision, error=f"{type(e).__name__}: {e}")

        truncated = len(raw) > max_bytes
        text = self._scrub(raw[:max_bytes].decode("utf-8", errors="replace"), secret_values)
        body = {
            "url": url, "method": method, "status": status, "headers": resp_headers,
            "body": text, "truncated": truncated, "elapsed_ms": round((time.time() - started) * 1000, 2),
        }
        return Result(
            task_id=task.id, ok=200 <= status < 400, decision=decision, stdout=text,
            exit_code=status, metadata=body,
        )

    # -----------------------------------------------------------------------
    # 11. read_file_chunked
    # -----------------------------------------------------------------------
    def _gate_read_chunked(self, task: Any) -> Result:
        path = str(Path(task.payload.get("path", "")).expanduser())
        decision, _reason, early = self._gate(task, f"read_chunk {path}", [path])
        return early if early is not None else self._exec_read_chunked(task, decision.value)

    def _exec_read_chunked(self, task: Any, decision: str = "auto") -> Result:
        """Read ``limit_bytes`` starting at a byte ``offset``, reporting pagination."""
        p = task.payload
        path = Path(p.get("path", "")).expanduser()
        if not path.is_file():
            return Result(task_id=task.id, ok=False, decision=decision, error=f"file not found: {path}")

        offset = max(0, int(p.get("offset", 0)))
        limit = max(1, int(p.get("limit_bytes", 65536)))
        encoding = p.get("encoding", "utf-8")
        total = path.stat().st_size

        with path.open("rb") as f:
            f.seek(offset)
            raw = f.read(limit)

        eof = offset + len(raw) >= total
        body = {
            "path": str(path), "offset": offset, "bytes_read": len(raw), "total_size": total,
            "next_offset": None if eof else offset + len(raw), "eof": eof,
            "content": raw.decode(encoding, errors="replace"), "encoding": encoding,
        }
        return Result(task_id=task.id, ok=True, decision=decision, stdout=body["content"], metadata=body)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------
def _default_shell() -> str:
    """Return the platform's interactive shell command."""
    if sys.platform.startswith("win"):
        return os.environ.get("COMSPEC", "cmd.exe")
    return os.environ.get("SHELL", "/bin/sh")


def _pump_lines(pipe: Any, stream: str, q: "queue.Queue[tuple[str, str] | None]") -> None:
    """Push every line from ``pipe`` onto ``q``, then a ``None`` sentinel."""
    try:
        for line in iter(pipe.readline, ""):
            q.put((stream, line))
    except (ValueError, OSError):  # pragma: no cover - pipe closed mid-read
        pass
    finally:
        q.put(None)


def _walk_files(root: Path, include: str, include_hidden: bool) -> Iterator[Path]:
    """Yield files under ``root`` matching ``include``, pruning noise directories."""
    if root.is_file():
        if fnmatch.fnmatch(root.name, include):
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in DEFAULT_PRUNE_DIRS and (include_hidden or not d.startswith("."))
        ]
        for fn in filenames:
            if not include_hidden and fn.startswith("."):
                continue
            if fnmatch.fnmatch(fn, include):
                yield Path(dirpath) / fn


def _list_processes_psutil() -> tuple[list[dict[str, Any]], str]:
    """Enumerate processes with psutil."""
    out: list[dict[str, Any]] = []
    fields = ["pid", "name", "username", "status", "memory_info", "cpu_percent", "create_time", "cmdline"]
    for proc in psutil.process_iter(fields):  # type: ignore[union-attr]
        try:
            info = proc.info
            mem = info.get("memory_info")
            created = info.get("create_time")
            out.append({
                "pid": info.get("pid"),
                "name": info.get("name") or "",
                "cmdline": " ".join(info.get("cmdline") or [])[:500],
                "username": info.get("username") or "",
                "status": info.get("status") or "",
                "cpu_percent": info.get("cpu_percent") or 0.0,
                "memory_mb": round(mem.rss / 1_048_576, 2) if mem else 0.0,
                "created": datetime.fromtimestamp(created, timezone.utc).isoformat() if created else None,
            })
        except Exception:  # process vanished mid-iteration
            continue
    return out, "psutil"


def _list_processes_fallback() -> tuple[list[dict[str, Any]], str]:
    """Enumerate processes by parsing ``tasklist`` (Windows) or ``ps`` (POSIX)."""
    out: list[dict[str, Any]] = []
    if sys.platform.startswith("win"):
        cmd = ["tasklist", "/FO", "CSV", "/NH"]
    else:
        cmd = ["ps", "-eo", "pid,comm,%cpu,rss,user,stat,args"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        return out, "unavailable"

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if sys.platform.startswith("win"):
            parts = [c.strip('"') for c in line.split('","')]
            if len(parts) < 5:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            mem_kb = re.sub(r"[^\d]", "", parts[4]) or "0"
            out.append({
                "pid": pid, "name": parts[0].strip('"'), "cmdline": parts[0].strip('"'),
                "username": "", "status": "", "cpu_percent": 0.0,
                "memory_mb": round(int(mem_kb) / 1024, 2), "created": None,
            })
        else:
            parts = line.split(None, 6)
            if len(parts) < 6 or not parts[0].isdigit():
                continue
            out.append({
                "pid": int(parts[0]), "name": parts[1],
                "cmdline": (parts[6] if len(parts) > 6 else parts[1])[:500],
                "username": parts[4], "status": parts[5],
                "cpu_percent": float(parts[2]) if _is_number(parts[2]) else 0.0,
                "memory_mb": round(int(parts[3]) / 1024, 2) if parts[3].isdigit() else 0.0,
                "created": None,
            })
    return out, "ps"


def _is_number(s: str) -> bool:
    """True when ``s`` parses as a float."""
    try:
        float(s)
        return True
    except ValueError:
        return False


def _is_local_ip(ip: str) -> bool:
    """True when ``ip`` is loopback, private, link-local, or unique-local."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local)


def _assert_local_url(url: str) -> tuple[bool, str]:
    """Validate that every IP ``url``'s host resolves to is local.

    This is the security boundary for ``http_local``: it runs before any
    socket is opened, so a public hostname (or a DNS name that resolves off
    the LAN) can never be reached from the user's machine.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"scheme not allowed: {parsed.scheme or '(none)'}"
    host = parsed.hostname
    if not host:
        return False, "url has no host"

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as e:
        return False, f"cannot resolve host {host!r}: {e}"

    ips = {info[4][0] for info in infos}
    if not ips:
        return False, f"cannot resolve host {host!r}"
    for ip in ips:
        if not _is_local_ip(ip):
            return False, f"host not local: {ip}"
    return True, ""

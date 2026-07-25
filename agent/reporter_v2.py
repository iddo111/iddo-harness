"""
Reporter v2 — chunked, streaming results.

v1's :class:`agent.reporter.Reporter` writes exactly one ``results/<id>.json``
per task, once, after the task finishes. That is fine for ``read_file`` and
useless for a ten-minute build.

:class:`ReporterV2` adds the streaming form described in ``docs/v2_spec.md``
§3: each chunk is written to ``results/<task_id>-chunk-<seq>.json`` with

* ``seq`` — strictly increasing from 0, no gaps
* ``is_final`` — exactly one chunk per task carries ``true``; it also carries
  ``ok``/``decision``/``exit_code``

AMP handling is identical to v1: when the inbound task carried a validated AMP
envelope (``task.envelope``), every chunk is a full AMP v1.0 ``harness_result``
outbound envelope whose ``payload.body`` is the chunk body, replying to
``task.envelope.reply.to_address``. For legacy (non-AMP) task packets the bare
body dict is written, preserving the pre-AMP result shape.

Pushing to git once per chunk is correct but slow under a burst of output, so
writes are batched: up to ``batch_size`` chunks or ``batch_interval_ms`` of
wall time, whichever comes first. A final chunk always forces an immediate
flush, so a consumer never waits on a timer to learn a task finished.

``agent/reporter.py`` is untouched — v1 tasks keep using it verbatim.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import amp

try:
    from locks import GIT_PUSH_LOCK
except ImportError:  # installed as a package
    from agent.locks import GIT_PUSH_LOCK

log = logging.getLogger("harness.reporter_v2")

# Same identity constants as v1's reporter — the harness is one AMP brick
# whichever reporter happens to be writing.
# TODO(amp): promote to policy.yaml / config.py alongside the v1 copies.
HARNESS_BRICK_NAME = "iddo-harness"
HARNESS_IDENTITY_CANONICAL = "brick:iddo-harness"

DEFAULT_BATCH_SIZE = 3
DEFAULT_BATCH_INTERVAL_MS = 1000


class ReporterV2:
    """Writes chunked AMP results into the bridge repo and pushes them.

    ``git_push`` exists so tests (and dry runs) can exercise the full chunk
    protocol against a temp directory without touching a remote.
    """

    def __init__(
        self,
        cfg: Any,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_interval_ms: int = DEFAULT_BATCH_INTERVAL_MS,
        git_push: bool = True,
        local_dir: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.repo = cfg.transport["repo"]
        self.result_dir = cfg.transport.get("result_dir", "results/")
        self.batch_size = max(1, batch_size)
        self.batch_interval = max(0, batch_interval_ms) / 1000.0
        self.git_push = git_push
        self._local = local_dir or (
            Path(tempfile.gettempdir()) / f"iddo-harness-bridge-{self.repo.replace('/', '_')}"
        )
        self._instance = getattr(cfg, "owner", None) or "agent-default"
        self._lock = threading.Lock()
        # Separate from _lock: git is a single-writer resource, so several
        # worker threads finishing at once must not interleave add/commit/push
        # in the same working copy. Shared process-wide because the v1 reporter
        # and the poller write into the same clone.
        self._git_lock = GIT_PUSH_LOCK
        self._pending: list[Path] = []
        self._last_flush = time.time()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------
    def send_chunk(self, task: Any, chunk_body: dict[str, Any], seq: int, is_final: bool) -> Path:
        """Write one chunk file and flush the batch when it is due.

        ``chunk_body`` is the kind-specific body; ``task_id``/``seq``/
        ``is_final`` are (re)stamped here so the on-disk contract holds even if
        a caller forgets them.
        """
        body = {**chunk_body, "task_id": task.id, "seq": seq, "is_final": is_final}
        payload = self._wrap(task, body)
        path = self._chunk_path(task.id, seq)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        with self._lock:
            self._pending.append(path)
            due = is_final or len(self._pending) >= self.batch_size or (time.time() - self._last_flush) >= self.batch_interval
        if due:
            self.flush(f"results: {task.id} chunk {seq}{' (final)' if is_final else ''}")

        log.debug(f"chunk {task.id}#{seq} is_final={is_final} → {path.name}")
        return path

    def send(self, task: Any, result: Any) -> Path:
        """Report a non-streaming result as a single ``seq=0``, final chunk.

        Lets a caller use the chunk protocol uniformly: a consumer that only
        knows about chunks still sees a well-formed one-chunk stream.
        """
        body = asdict(result) if is_dataclass(result) and not isinstance(result, type) else dict(result)
        return self.send_chunk(task, body, seq=0, is_final=True)

    def send_error(self, task: Any, err_msg: str) -> Path:
        """Report a failure as a single final chunk."""
        return self.send_chunk(
            task, {"ok": False, "decision": "error", "error": err_msg}, seq=0, is_final=True
        )

    def send_attempt(self, task: Any, result: Any, attempt: int) -> Path:
        """Record one retry attempt as ``results/<id>-attempt-<n>.json``.

        Deliberately *not* a chunk: attempt files sit outside the ``seq`` /
        ``is_final`` stream so a consumer following the chunk protocol never
        sees two finals for one task. They are diagnostics — the authoritative
        outcome is still the final chunk of the last attempt.
        """
        body = asdict(result) if is_dataclass(result) and not isinstance(result, type) else dict(result)
        body = {**body, "task_id": task.id, "attempt": attempt}
        path = self._local / self.result_dir / f"{task.id}-attempt-{attempt}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._wrap(task, body), indent=2, ensure_ascii=False), encoding="utf-8")
        with self._lock:
            self._pending.append(path)
        return path

    def flush(self, message: str = "results: chunk batch") -> None:
        """Commit and push every chunk written since the last flush."""
        with self._lock:
            pending, self._pending = self._pending, []
            self._last_flush = time.time()
        if not pending or not self.git_push:
            return
        args = ["git", "-C", str(self._local)]
        with self._git_lock:
            subprocess.run(args + ["add", *[str(p) for p in pending]], check=False, capture_output=True)
            subprocess.run(args + ["commit", "-m", message], check=False, capture_output=True)
            subprocess.run(args + ["push", "--quiet"], check=False, capture_output=True)
        log.info(f"pushed {len(pending)} chunk file(s): {message}")

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------
    def _chunk_path(self, task_id: str, seq: int) -> Path:
        """Return the on-disk path for one chunk."""
        return self._local / self.result_dir / f"{task_id}-chunk-{seq}.json"

    def _wrap(self, task: Any, body: dict[str, Any]) -> dict[str, Any]:
        """Wrap a chunk body in an AMP envelope, or pass it through for legacy tasks."""
        envelope = getattr(task, "envelope", None)
        if envelope is None:
            return body
        result_envelope = amp.build_envelope(
            direction="outbound",
            source_brick=HARNESS_BRICK_NAME,
            source_instance=self._instance,
            channel=envelope.channel,
            identity_canonical=HARNESS_IDENTITY_CANONICAL,
            identity_self=False,
            payload_type="harness_result",
            payload_body=body,
            to_channel=envelope.reply.to_channel,
            to_address=envelope.reply.to_address,
            reply_to_id=envelope.id,
        )
        return amp.serialize(result_envelope)

    # -----------------------------------------------------------------------
    def chunk_sink(self, task: Any, body: dict[str, Any], seq: int, is_final: bool) -> None:
        """Adapter matching :class:`agent.executor_v2.ExecutorV2`'s ``chunk_sink``."""
        self.send_chunk(task, body, seq, is_final)

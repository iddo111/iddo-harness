"""
GitHub-based task poller.
Pulls task packets from tasks/*.json in the bridge repo.

AMP integration (docs/amp_alignment.md, docs/amp_envelope_examples.md):
incoming task-packet JSON is now expected to be a full AMP v1.0 envelope
with payload.type == "harness_task" (see agent/amp.py). For backward
compatibility with the pre-AMP task-packet shape documented in
docs/task_packet_spec.md (top-level "kind", no envelope wrapper), a
legacy packet is still accepted — with a logged warning — and lifted into
an equivalent Task. See _task_from_amp / _task_from_legacy below.
"""
import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import amp
except ImportError:  # pragma: no cover - installed package imports
    from agent import amp

try:
    from locks import GIT_PUSH_LOCK
except ImportError:  # installed as a package
    from agent.locks import GIT_PUSH_LOCK

log = logging.getLogger("harness.poller")


@dataclass
class Task:
    id: str
    kind: str  # "shell" | "read_file" | "write_file" | "list_dir"
    payload: dict = field(default_factory=dict)
    priority: str = "normal"
    source_path: Path | None = None
    # AMP context — populated when the source packet was a valid AMP
    # envelope, so reporter.py can build a proper harness_result envelope
    # (reply.to_address, reply_to_id, etc.) without re-reading the file.
    # None for legacy (non-AMP) task packets.
    # TODO(amp): once every producer (Perplexity/Claude/GPT/Gemini) speaks
    # AMP exclusively, `envelope` can become required instead of optional.
    envelope: "amp.AmpEnvelope | None" = None
    # Set only by an authenticated transport. The AMP body's claimed
    # ``agent_id`` is producer-controlled and is never trusted for policy.
    authenticated_agent_id: str = ""
    transport: str = "git"
    client_id: str = ""


def task_from_packet(data: dict, source_path: Path | None = None) -> "Task":
    """
    Build a Task from raw packet JSON, preferring the AMP v1.0 envelope
    shape (payload.type == "harness_task") and falling back to the legacy
    free-form task-packet shape (docs/task_packet_spec.md, top-level "kind")
    for backward compatibility.

    Module-level so transports that never touch the filesystem — the v3
    WebSocket bridge — parse packets exactly the same way the git bridge does.
    `source_path` is None for those.
    """
    label = source_path.name if source_path is not None else "<ws>"

    if amp.is_amp_shaped(data):
        return _task_from_amp(data, source_path)

    if "kind" in data:
        log.warning(
            f"{label}: task packet is not AMP-shaped (no 'v'/'payload' "
            f"envelope) — accepting as legacy packet per backward-compat policy. "
            f"Producers should migrate to AMP envelopes (docs/amp_alignment.md)."
        )
        return _task_from_legacy(data, source_path)

    # Neither AMP-shaped nor legacy-shaped (no "kind") — try AMP parsing
    # anyway so the caller gets a precise AmpValidationError rather than
    # a confusing KeyError/AttributeError downstream.
    return _task_from_amp(data, source_path)


def _task_from_amp(data: dict, source_path: Path | None = None) -> "Task":
    """
    Validate `data` as an AMP envelope and lift its payload.body into a
    legacy-shaped Task (kind/payload/priority) so executor.py — which is
    AMP-agnostic by design — needs no changes. The full envelope is retained
    on Task.envelope so reporter.py can build a proper harness_result envelope.
    """
    # Legacy bridge task ids (e.g. "20260723-001-scan-dclaude") are not valid
    # ULID/UUIDv4, but may still show up wrapped in an AMP envelope during the
    # migration window; relax the `id` pattern check accordingly rather than
    # hard-failing well-formed envelopes over an id-format nitpick that AMP
    # §2.6 minor-version tolerance is meant to absorb.
    # TODO(amp): once every producer mints proper ULID/UUIDv4 ids, switch this
    # to id_strict=True unconditionally.
    env_id = data.get("id", "")
    id_strict = bool(amp._ULID_RE.match(env_id) or amp._UUID4_RE.match(str(env_id).lower()))
    envelope = amp.parse_envelope(data, id_strict=id_strict)

    body = envelope.payload.body
    return Task(
        id=envelope.id,
        kind=body.get("kind", "shell"),
        payload=body,
        priority=body.get("priority", "normal"),
        source_path=source_path,
        envelope=envelope,
    )


def _task_from_legacy(data: dict, source_path: Path | None = None) -> "Task":
    """Lift a pre-AMP, free-form task packet into a Task. envelope=None."""
    fallback_id = source_path.stem if source_path is not None else ""
    return Task(
        id=data.get("id", fallback_id),
        kind=data.get("kind", "shell"),
        payload=data.get("payload", {}),
        priority=data.get("priority", "normal"),
        source_path=source_path,
        envelope=None,
    )


class GithubPoller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.repo = cfg.transport["repo"]
        self.task_dir = cfg.transport.get("task_dir", "tasks/")
        self.result_dir = cfg.transport.get("result_dir", "results/")
        self._local = Path(tempfile.gettempdir()) / f"iddo-harness-bridge-{self.repo.replace('/', '_')}"

    # -----------------------------------------------------------------------
    def _sync(self):
        """git pull the bridge repo into a temp workdir."""
        if not self._local.exists():
            log.info(f"Cloning bridge repo {self.repo} → {self._local}")
            subprocess.run(
                ["gh", "repo", "clone", self.repo, str(self._local)],
                check=True, capture_output=True,
            )
        else:
            subprocess.run(
                ["git", "-C", str(self._local), "pull", "--quiet"],
                check=False, capture_output=True,
            )

    # -----------------------------------------------------------------------
    def fetch_pending_tasks(self) -> list[Task]:
        try:
            self._sync()
        except Exception as e:
            log.warning(f"Bridge sync failed: {e}")
            return []

        task_folder = self._local / self.task_dir
        task_folder.mkdir(parents=True, exist_ok=True)
        tasks = []
        for p in sorted(task_folder.glob("*.json")):
            if p.name.startswith("done-"):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                task = self._task_from_packet(data, p)
                tasks.append(task)
            except Exception as e:
                log.error(f"Bad task {p}: {e}")
        return tasks

    # -----------------------------------------------------------------------
    def _task_from_packet(self, data: dict, source_path: Path) -> "Task":
        """Delegate to the module-level parser (shared with the WS bridge)."""
        return task_from_packet(data, source_path)

    # -----------------------------------------------------------------------
    def scan_pending(self, confirm_manager) -> list[tuple[Task, bool]]:
        """Scan the pending-confirmation queue for tasks that now have a response.

        Looks for `approved-<id>.json` / `denied-<id>.json` files (written by
        the CLI's `confirm` command) alongside each `pending-confirm-<id>.json`.
        Also runs `confirm_manager.sweep_timeouts()` first so stale pending
        confirmations (older than `confirm.timeout_minutes`) are auto-denied.

        Returns a list of (Task, approved) tuples ready to resume via
        `Executor.resume_after_confirm`. Responded pending files are removed
        from the local queue after being picked up.
        """
        try:
            self._sync()
        except Exception as e:
            log.warning(f"Bridge sync failed during scan_pending: {e}")

        confirm_manager.sweep_timeouts()

        resolved: list[tuple[Task, bool]] = []
        for pending_path in sorted(confirm_manager.local_dir.glob("pending-confirm-*.json")):
            task_id = pending_path.stem[len("pending-confirm-"):]
            status = confirm_manager.check_response(task_id)
            if status is None:
                continue
            try:
                data = json.loads(pending_path.read_text(encoding="utf-8"))
            except Exception as e:
                log.error(f"bad pending file {pending_path}: {e}")
                continue

            task = Task(
                id=data.get("task_id", task_id),
                kind=data.get("kind", "shell"),
                payload=data.get("payload", {}),
                authenticated_agent_id=data.get("authenticated_agent_id", ""),
                transport=data.get("transport", "git"),
                client_id=data.get("client_id", ""),
            )
            approved = status == "approved"
            log.info(f"scan_pending: task={task.id} resolved as {status}")
            resolved.append((task, approved))

            # clean up the pending marker now that it's been picked up
            try:
                pending_path.unlink()
            except OSError:
                pass

        return resolved

    # -----------------------------------------------------------------------
    def mark_done(self, task: Task):
        if task.source_path and task.source_path.exists():
            new = task.source_path.parent / f"done-{task.source_path.name}"
            task.source_path.rename(new)
            self._commit_and_push(f"mark done: {task.id}")

    def mark_failed(self, task: Task):
        if task.source_path and task.source_path.exists():
            new = task.source_path.parent / f"failed-{task.source_path.name}"
            task.source_path.rename(new)
            self._commit_and_push(f"mark failed: {task.id}")

    # -----------------------------------------------------------------------
    def _commit_and_push(self, msg: str):
        # `add -A` sweeps up whatever the reporters have written, so this must
        # not interleave with their commits — hence the shared lock.
        with GIT_PUSH_LOCK:
            subprocess.run(["git", "-C", str(self._local), "add", "-A"], check=False, capture_output=True)
            subprocess.run(["git", "-C", str(self._local), "commit", "-m", msg, "--allow-empty"], check=False, capture_output=True)
            subprocess.run(["git", "-C", str(self._local), "push", "--quiet"], check=False, capture_output=True)

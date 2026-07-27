"""
Confirmation subsystem.

When the policy engine returns Decision.CONFIRM, we no longer just refuse the
task outright. Instead we park it as a *pending confirmation*:

  - a JSON file is written to the local queue (~/.iddo-harness/pending/)
  - the same JSON file is mirrored into the bridge repo's pending/ folder so
    that a human (or another AMP brick) sees it and can approve/deny it
  - a short, mobile-friendly one-liner is logged/generated so it can be
    forwarded via push/SMS/whatever notification channel is wired up later

The CLI's `confirm` command writes an `approved-<id>.json` or
`denied-<id>.json` file next to the pending file. The polling loop calls
`scan_pending()` (see poller.py) each cycle to discover those response files
and resume (or drop) the task via `Executor.resume_after_confirm`.

Pending confirmations older than `confirm.timeout_minutes` (policy.yaml,
default 30) are auto-denied.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("harness.confirm")

LOCAL_PENDING_DIR = Path.home() / ".iddo-harness" / "pending"
DEFAULT_TIMEOUT_MINUTES = 30


def local_pending_dir() -> Path:
    """The pending-confirmation directory under the *current* home.

    Resolved on each call rather than read from :data:`LOCAL_PENDING_DIR`,
    which freezes ``Path.home()`` at import time and so keeps pointing at the
    real home even after a caller (or a test) has redirected it.
    """
    return Path.home() / ".iddo-harness" / "pending"


@dataclass
class PendingConfirmation:
    task_id: str
    command: str
    reason: str
    created_ts: float
    kind: str = "shell"
    payload: dict = field(default_factory=dict)
    message: str = ""
    status: str = "pending"  # pending | approved | denied | timed_out

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def _one_liner(task_id: str, command: str) -> str:
    """Mobile-friendly one-liner used for push/SMS notifications."""
    cmd = command if len(command) <= 120 else command[:117] + "..."
    return (
        f"Approve task {task_id}? Command: {cmd}. "
        f"Reply with `iddo-harness confirm {task_id} --approve` or reject."
    )


class ConfirmManager:
    """Owns the pending-confirmation queue, both locally and in the bridge repo."""

    def __init__(self, cfg=None, local_dir: Optional[Path] = None):
        self.cfg = cfg
        self.local_dir = Path(local_dir) if local_dir else local_pending_dir()
        self.local_dir.mkdir(parents=True, exist_ok=True)

        self.timeout_minutes = DEFAULT_TIMEOUT_MINUTES
        if cfg is not None:
            confirm_cfg = getattr(cfg, "confirm", None)
            if confirm_cfg is None and hasattr(cfg, "__dict__"):
                confirm_cfg = cfg.__dict__.get("confirm")
            if isinstance(confirm_cfg, dict):
                self.timeout_minutes = confirm_cfg.get(
                    "timeout_minutes", DEFAULT_TIMEOUT_MINUTES
                )

        self._bridge_local: Optional[Path] = None
        if cfg is not None and getattr(cfg, "transport", None):
            repo = cfg.transport.get("repo")
            if repo:
                self._bridge_local = (
                    Path(tempfile.gettempdir())
                    / f"iddo-harness-bridge-{repo.replace('/', '_')}"
                )

    # -----------------------------------------------------------------------
    def _bridge_pending_dir(self) -> Optional[Path]:
        if self._bridge_local is None:
            return None
        d = self._bridge_local / "pending"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -----------------------------------------------------------------------
    def create(self, task, reason: str) -> PendingConfirmation:
        """Write a pending-confirm-<taskid>.json locally and to the bridge repo."""
        command = ""
        if isinstance(task.payload, dict):
            command = task.payload.get("command") or task.payload.get("path", "")

        pc = PendingConfirmation(
            task_id=task.id,
            command=command,
            reason=reason,
            created_ts=time.time(),
            kind=getattr(task, "kind", "shell"),
            payload=dict(task.payload) if isinstance(task.payload, dict) else {},
        )
        pc.message = _one_liner(pc.task_id, command)

        filename = f"pending-confirm-{task.id}.json"
        local_path = self.local_dir / filename
        local_path.write_text(pc.to_json(), encoding="utf-8")
        log.info(f"wrote local pending confirmation: {local_path}")

        bridge_dir = self._bridge_pending_dir()
        if bridge_dir is not None:
            try:
                bridge_path = bridge_dir / filename
                bridge_path.write_text(pc.to_json(), encoding="utf-8")
                self._commit_and_push(f"pending confirm: {task.id}")
                log.info(f"mirrored pending confirmation to bridge: {bridge_path}")
            except Exception as e:
                log.warning(f"failed to mirror pending confirmation to bridge: {e}")

        log.info(pc.message)
        return pc

    # -----------------------------------------------------------------------
    def respond(self, task_id: str, approve: bool) -> Path:
        """CLI-side: write approved-<id>.json / denied-<id>.json to both queues."""
        verb = "approved" if approve else "denied"
        filename = f"{verb}-{task_id}.json"
        payload = json.dumps(
            {"task_id": task_id, "status": verb, "ts": time.time()},
            indent=2,
        )

        local_path = self.local_dir / filename
        local_path.write_text(payload, encoding="utf-8")
        log.info(f"wrote local response: {local_path}")

        bridge_dir = self._bridge_pending_dir()
        if bridge_dir is not None:
            try:
                bridge_path = bridge_dir / filename
                bridge_path.write_text(payload, encoding="utf-8")
                self._commit_and_push(f"{verb}: {task_id}")
                log.info(f"mirrored response to bridge: {bridge_path}")
            except Exception as e:
                log.warning(f"failed to mirror response to bridge: {e}")

        return local_path

    # -----------------------------------------------------------------------
    def list_pending(self) -> list[PendingConfirmation]:
        """List all still-open pending confirmations (no response file yet)."""
        out = []
        for p in sorted(self.local_dir.glob("pending-confirm-*.json")):
            task_id = p.stem[len("pending-confirm-"):]
            if self._has_response(task_id):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                out.append(PendingConfirmation(**data))
            except Exception as e:
                log.error(f"bad pending file {p}: {e}")
        return out

    # -----------------------------------------------------------------------
    def _has_response(self, task_id: str) -> bool:
        return (self.local_dir / f"approved-{task_id}.json").exists() or (
            self.local_dir / f"denied-{task_id}.json"
        ).exists()

    # -----------------------------------------------------------------------
    def check_response(self, task_id: str) -> Optional[str]:
        """Return 'approved' / 'denied' / None by checking both local and bridge dirs."""
        for base in filter(None, [self.local_dir, self._bridge_pending_dir()]):
            if (base / f"approved-{task_id}.json").exists():
                return "approved"
            if (base / f"denied-{task_id}.json").exists():
                return "denied"
        return None

    # -----------------------------------------------------------------------
    def sweep_timeouts(self) -> list[str]:
        """Auto-deny pending confirmations older than timeout_minutes.

        Returns list of task_ids that were auto-denied.
        """
        deadline_seconds = self.timeout_minutes * 60
        now = time.time()
        denied = []
        for p in sorted(self.local_dir.glob("pending-confirm-*.json")):
            task_id = p.stem[len("pending-confirm-"):]
            if self._has_response(task_id):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                created_ts = data.get("created_ts", now)
            except Exception:
                continue
            if now - created_ts >= deadline_seconds:
                log.warning(
                    f"pending confirmation {task_id} timed out after "
                    f"{self.timeout_minutes}m — auto-denying"
                )
                self.respond(task_id, approve=False)
                denied.append(task_id)
        return denied

    # -----------------------------------------------------------------------
    def _commit_and_push(self, msg: str):
        if self._bridge_local is None:
            return
        subprocess.run(
            ["git", "-C", str(self._bridge_local), "add", "-A"],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self._bridge_local), "commit", "-m", msg, "--allow-empty"],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self._bridge_local), "push", "--quiet"],
            check=False, capture_output=True,
        )

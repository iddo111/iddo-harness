"""
Executor — actually runs the task subject to policy.
"""
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from policy import Decision, PolicyEngine

try:
    from confirm import ConfirmManager
except ImportError:  # pragma: no cover - fallback for packaged imports
    from agent.confirm import ConfirmManager

log = logging.getLogger("harness.executor")


@dataclass
class Result:
    task_id: str
    ok: bool
    decision: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    error: str = ""
    metadata: dict = field(default_factory=dict)


class Executor:
    def __init__(self, policy: PolicyEngine, confirm_manager: "ConfirmManager | None" = None):
        self.policy = policy
        self.confirm_manager = confirm_manager or ConfirmManager(getattr(policy, "cfg", None))

    # -----------------------------------------------------------------------
    def run(self, task) -> Result:
        kind = task.kind
        if kind == "shell":
            return self._run_shell(task)
        if kind == "read_file":
            return self._read_file(task)
        if kind == "write_file":
            return self._write_file(task)
        if kind == "list_dir":
            return self._list_dir(task)
        return Result(task_id=task.id, ok=False, decision="unknown_kind", error=f"unknown kind: {kind}")

    # -----------------------------------------------------------------------
    def resume_after_confirm(self, task, approved: bool) -> Result:
        """Called by the polling loop once a pending confirmation has a response.

        If approved, actually perform the original task action (bypassing the
        policy CONFIRM gate, since a human already approved it). If denied,
        return a `denied` Result without running anything.
        """
        if not approved:
            log.info(f"task={task.id} confirmation denied — dropping")
            return Result(task_id=task.id, ok=False, decision="denied", error="confirmation denied by user")

        log.info(f"task={task.id} confirmation approved — resuming execution")
        kind = task.kind
        try:
            if kind == "shell":
                return self._exec_shell(task)
            if kind == "write_file":
                return self._exec_write_file(task)
            if kind == "read_file":
                return self._read_file(task)
            if kind == "list_dir":
                return self._list_dir(task)
            return Result(task_id=task.id, ok=False, decision="unknown_kind", error=f"unknown kind: {kind}")
        except Exception as e:
            log.exception(f"task={task.id} failed during resume_after_confirm")
            return Result(task_id=task.id, ok=False, decision="approved", error=str(e))

    # -----------------------------------------------------------------------
    def _run_shell(self, task) -> Result:
        cmd = task.payload.get("command", "")
        paths = task.payload.get("paths", [])
        decision, reason = self.policy.decide(cmd, paths)
        self.policy.audit(task.id, cmd, decision, reason)

        if decision is Decision.BLOCK:
            return Result(task_id=task.id, ok=False, decision="block", error=reason)

        # CONFIRM path — park the task as a pending confirmation instead of
        # refusing outright. The polling loop will resume it via
        # resume_after_confirm() once a human responds (see confirm.py).
        if decision is Decision.CONFIRM:
            pc = self.confirm_manager.create(task, reason)
            return Result(
                task_id=task.id, ok=False, decision="confirm_required", error=reason,
                metadata={"message": pc.message},
            )

        return self._exec_shell(task)

    # -----------------------------------------------------------------------
    def _exec_shell(self, task) -> Result:
        """Actually invoke the shell command (used by auto path and by resume_after_confirm)."""
        cmd = task.payload.get("command", "")
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=task.payload.get("timeout_sec", 300),
            )
            return Result(
                task_id=task.id,
                ok=proc.returncode == 0,
                decision="auto",
                stdout=proc.stdout[-16000:],
                stderr=proc.stderr[-4000:],
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return Result(task_id=task.id, ok=False, decision="auto", error="timeout")
        except Exception as e:
            return Result(task_id=task.id, ok=False, decision="auto", error=str(e))

    # -----------------------------------------------------------------------
    def _read_file(self, task) -> Result:
        path = Path(task.payload.get("path", "")).expanduser()
        decision, reason = self.policy.decide(f"cat {path}", [str(path)])
        self.policy.audit(task.id, f"read {path}", decision, reason)
        if decision is Decision.BLOCK:
            return Result(task_id=task.id, ok=False, decision="block", error=reason)
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
            return Result(
                task_id=task.id, ok=True, decision=decision.value,
                stdout=data[:200_000],
                metadata={"path": str(path), "size": path.stat().st_size}
            )
        except Exception as e:
            return Result(task_id=task.id, ok=False, decision=decision.value, error=str(e))

    # -----------------------------------------------------------------------
    def _write_file(self, task) -> Result:
        path = Path(task.payload.get("path", "")).expanduser()
        decision, reason = self.policy.decide(f"write {path}", [str(path)])
        self.policy.audit(task.id, f"write {path}", decision, reason)
        if decision is Decision.BLOCK:
            return Result(task_id=task.id, ok=False, decision="block", error=reason)
        if decision is Decision.CONFIRM:
            pc = self.confirm_manager.create(task, reason)
            return Result(
                task_id=task.id, ok=False, decision="confirm_required", error=reason,
                metadata={"message": pc.message},
            )
        return self._exec_write_file(task)

    # -----------------------------------------------------------------------
    def _exec_write_file(self, task) -> Result:
        """Actually write the file (used by auto path and by resume_after_confirm)."""
        path = Path(task.payload.get("path", "")).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(task.payload.get("content", ""), encoding="utf-8")
            return Result(task_id=task.id, ok=True, decision="auto",
                          metadata={"path": str(path), "bytes": path.stat().st_size})
        except Exception as e:
            return Result(task_id=task.id, ok=False, decision="auto", error=str(e))

    # -----------------------------------------------------------------------
    def _list_dir(self, task) -> Result:
        path = Path(task.payload.get("path", "")).expanduser()
        decision, reason = self.policy.decide(f"ls {path}", [str(path)])
        self.policy.audit(task.id, f"list {path}", decision, reason)
        if decision is Decision.BLOCK:
            return Result(task_id=task.id, ok=False, decision="block", error=reason)
        try:
            entries = [str(p.relative_to(path)) for p in sorted(path.iterdir())]
            return Result(task_id=task.id, ok=True, decision=decision.value,
                          stdout="\n".join(entries),
                          metadata={"path": str(path), "count": len(entries)})
        except Exception as e:
            return Result(task_id=task.id, ok=False, decision=decision.value, error=str(e))

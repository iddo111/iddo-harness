"""
Executor — actually runs the task subject to policy.
"""
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

try:
    from policy import Decision, PolicyEngine
except ImportError:  # pragma: no cover
    from agent.policy import Decision, PolicyEngine

try:
    from confirm import ConfirmManager
except ImportError:  # pragma: no cover - fallback for packaged imports
    from agent.confirm import ConfirmManager

try:
    import sandbox as sandbox_mod
    import secrets_vault
except ImportError:  # pragma: no cover - fallback for packaged imports
    from agent import sandbox as sandbox_mod
    from agent import secrets_vault

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
    def __init__(
        self,
        policy: PolicyEngine,
        confirm_manager: "ConfirmManager | None" = None,
        chunk_sink=None,
        vault=None,
        audit_log=None,
    ):
        self.policy = policy
        self.confirm_manager = confirm_manager or ConfirmManager(getattr(policy, "cfg", None))
        self.chunk_sink = chunk_sink
        # v3 Track B: shared with ExecutorV2 so `{{secret:...}}` and the
        # sandbox behave the same whether a task arrives as v1 `shell` or v2
        # `shell_stream`. Both default to None → previous behaviour exactly.
        self.vault = vault
        self.audit_log = audit_log
        self._v2 = None
        self._v3 = None

    # -----------------------------------------------------------------------
    @property
    def v2(self):
        """Lazily-built ExecutorV2, cached so live shell sessions and watches
        survive across polling cycles. Imported inside the property so
        executor_v2's optional dependencies stay off the v1 import path.
        """
        if self._v2 is None:
            try:
                from executor_v2 import ExecutorV2
            except ImportError:  # pragma: no cover - packaged imports
                from agent.executor_v2 import ExecutorV2
            self._v2 = ExecutorV2(
                self.policy, self.confirm_manager, chunk_sink=self.chunk_sink,
                vault=self.vault, audit_log=self.audit_log,
            )
        return self._v2

    @staticmethod
    def _is_v2_kind(kind: str) -> bool:
        """True for the v2 Agent Fabric kinds (docs/v2_spec.md §2)."""
        try:
            from executor_v2 import V2_KINDS
        except ImportError:  # pragma: no cover - packaged imports
            from agent.executor_v2 import V2_KINDS
        return kind in V2_KINDS

    # -----------------------------------------------------------------------
    @property
    def v3(self):
        """Lazily-built ExecutorV3, cached so the memory store, scheduler and
        in-flight sub-tasks outlive a single polling cycle. It receives *this*
        executor as its parent so a sub-task, workflow node or LLM tool call of
        any kind re-enters at the router and meets policy on its own terms.
        """
        if self._v3 is None:
            try:
                from executor_v3 import ExecutorV3
            except ImportError:  # pragma: no cover - packaged imports
                from agent.executor_v3 import ExecutorV3
            self._v3 = ExecutorV3(
                self.policy, self.confirm_manager,
                chunk_sink=self.chunk_sink, parent_executor=self,
            )
        return self._v3

    @staticmethod
    def _is_v3_kind(kind: str) -> bool:
        """True for the v3 agent-native kinds (docs/v3_track_c.md)."""
        try:
            from executor_v3 import V3_KINDS
        except ImportError:  # pragma: no cover - packaged imports
            from agent.executor_v3 import V3_KINDS
        return kind in V3_KINDS

    # -----------------------------------------------------------------------
    def run(self, task) -> Result:
        kind = task.kind
        # v3 / v2 routers — everything below this line is the untouched v1 path.
        if self._is_v3_kind(kind):
            return self.v3.run(task)
        if self._is_v2_kind(kind):
            return self.v2.run(task)
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
        if self._is_v3_kind(kind):
            return self.v3.resume_after_confirm(task, approved)
        if self._is_v2_kind(kind):
            return self.v2.resume_after_confirm(task, approved)
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
            # Resolve `{{secret:name}}` here, one call before Popen, and scrub
            # the values out of the output — the command may well echo its own
            # arguments, and stdout is committed to the bridge repo verbatim.
            cmd, used = secrets_vault.resolve_with(self.vault, cmd)
            secret_values = tuple(used.values())
        except secrets_vault.VaultError as e:
            return Result(task_id=task.id, ok=False, decision="auto", error=str(e))

        level = sandbox_mod.level_for_kind(getattr(self.policy, "cfg", None), task.kind)
        try:
            proc = subprocess.run(
                sandbox_mod.wrap_popen_args(cmd, level, task.payload.get("cwd")),
                shell=True,
                capture_output=True,
                text=True,
                timeout=task.payload.get("timeout_sec", 300),
            )
            return Result(
                task_id=task.id,
                ok=proc.returncode == 0,
                decision="auto",
                stdout=secrets_vault.redact(proc.stdout, secret_values)[-16000:],
                stderr=secrets_vault.redact(proc.stderr, secret_values)[-4000:],
                exit_code=proc.returncode,
                metadata={"sandbox": level} if level != sandbox_mod.LEVEL_NONE else {},
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

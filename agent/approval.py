"""
Approval workflow — how a CONFIRM decision reaches a human.

:class:`~confirm.ConfirmManager` already parks the task as a file and waits for
``approved-<id>.json``. That works, but only if someone is looking at the
directory. This module keeps the same file queue as the source of truth and adds
the part that pulls a human's attention to it:

    local        — the v1 behaviour, files only (default)
    notification — additionally raise a desktop notification
    remote       — additionally push the request over Track A's WS bridge

The modes are cumulative, not alternative: ``remote`` still writes the pending
file, so a request delivered over a websocket that nobody answers can still be
approved later from the CLI, and a crash mid-flight loses nothing.

Two behaviours differ from ``ConfirmManager`` by design:

* **Timeout.** ``approval.timeout_seconds`` (default 300) replaces
  ``confirm.timeout_minutes`` (default 1800s). A security prompt that lingers
  for half an hour is a prompt that gets approved without being read.
* **Auditing.** Every decision — including the automatic deny on timeout — is
  appended to the hash-chained audit log with who decided and when.

Delivery is always best-effort. A missing ``notify-send``, a WS bridge that
isn't running, a notification daemon that refuses the connection: each is logged
and then ignored. Failing to *tell* someone about a pending approval must never
turn into failing to *record* it.
"""
from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

try:
    from confirm import ConfirmManager, PendingConfirmation
    from secrets_vault import mask_text
except ImportError:  # pragma: no cover - packaged imports
    from agent.confirm import ConfirmManager, PendingConfirmation
    from agent.secrets_vault import mask_text

log = logging.getLogger("harness.approval")

MODE_LOCAL = "local"
MODE_NOTIFICATION = "notification"
MODE_REMOTE = "remote"
VALID_MODES = (MODE_LOCAL, MODE_NOTIFICATION, MODE_REMOTE)

DEFAULT_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 1.0

NOTIFY_TITLE = "Iddo Harness — approval needed"


def normalise_mode(mode: Any) -> str:
    """Coerce a configured mode to one of :data:`VALID_MODES`."""
    text = str(mode or MODE_LOCAL).strip().lower()
    if text not in VALID_MODES:
        log.warning(f"unknown approval mode {text!r} — falling back to {MODE_LOCAL}")
        return MODE_LOCAL
    return text


# ---------------------------------------------------------------------------
# Desktop notification
# ---------------------------------------------------------------------------
def notify_desktop(title: str, body: str, *, timeout: int = 10) -> bool:
    """Raise a desktop notification. True when a backend accepted it.

    Three platforms, three tools, none of them guaranteed to exist — a headless
    Linux box has no ``notify-send``, and a Windows install without PowerShell
    toast support is a normal thing to meet. Returning False is not an error.
    """
    system = platform.system()
    try:
        if system == "Linux":
            if not shutil.which("notify-send"):
                log.debug("notify-send not on PATH — no desktop notification")
                return False
            subprocess.run(
                ["notify-send", "--urgency=critical", title, body],
                check=False, capture_output=True, timeout=timeout,
            )
            return True
        if system == "Darwin":
            if not shutil.which("osascript"):
                return False
            script = (
                f'display notification {json.dumps(body)} '
                f'with title {json.dumps(title)}'
            )
            subprocess.run(
                ["osascript", "-e", script],
                check=False, capture_output=True, timeout=timeout,
            )
            return True
        if system == "Windows":
            # BurntToast is not installed by default, so use the WinRT toast API
            # that ships with the OS and reachable from PowerShell.
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
                " ContentType = WindowsRuntime] > $null; "
                "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode({_ps_quote(title)})) > $null; "
                f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode({_ps_quote(body)})) > $null; "
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'Iddo Harness').Show([Windows.UI.Notifications.ToastNotification]::new($t))"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                check=False, capture_output=True, timeout=timeout,
            )
            return True
    except Exception as e:
        log.warning(f"desktop notification failed: {e}")
        return False
    log.debug(f"no notification backend for platform {system!r}")
    return False


def _ps_quote(text: str) -> str:
    """Single-quote a string for PowerShell (doubling embedded quotes)."""
    return "'" + str(text).replace("'", "''") + "'"


# ---------------------------------------------------------------------------
class ApprovalManager(ConfirmManager):
    """A :class:`ConfirmManager` that also *delivers* the request.

    Drop-in compatible: ``Executor(policy, confirm_manager=ApprovalManager(cfg))``
    behaves exactly like the v1 manager when ``approval.mode`` is ``local``.
    """

    def __init__(
        self,
        cfg: Any = None,
        local_dir: Optional[Path] = None,
        *,
        mode: Any = None,
        timeout_seconds: Optional[float] = None,
        audit_log: Any = None,
        ws_bridge: Any = None,
    ) -> None:
        super().__init__(cfg=cfg, local_dir=local_dir)
        approval_cfg = getattr(cfg, "approval", None) if cfg is not None else None
        if not isinstance(approval_cfg, dict):
            approval_cfg = {}

        self.mode = normalise_mode(mode if mode is not None else approval_cfg.get("mode"))
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else approval_cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        )
        self.audit_log = audit_log
        self.ws_bridge = ws_bridge
        # ConfirmManager.sweep_timeouts() works in minutes; keep the two views
        # of the same deadline in agreement so either entry point is correct.
        self.timeout_minutes = self.timeout_seconds / 60.0

    # -----------------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: Any, **kwargs: Any) -> "ApprovalManager":
        """Build from the ``approval`` block of policy.yaml."""
        return cls(cfg=cfg, **kwargs)

    # -----------------------------------------------------------------------
    def _audit(self, action: str, resource: str, outcome: str, **meta: Any) -> None:
        if self.audit_log is None:
            return
        try:
            self.audit_log.record(
                actor=meta.pop("actor", "harness"),
                action=action,
                resource=mask_text(resource),
                outcome=outcome,
                meta=meta,
            )
        except Exception:  # pragma: no cover - auditing must not break approval
            log.exception("audit sink failed while recording an approval event")

    # -----------------------------------------------------------------------
    def create(self, task: Any, reason: str) -> PendingConfirmation:
        """Park the request, then deliver it through the configured channels."""
        pc = super().create(task, reason)
        self._audit(
            "approval_requested",
            pc.command or pc.task_id,
            "ok",
            actor=str(pc.task_id),
            mode=self.mode,
            reason=reason,
            kind=pc.kind,
            timeout_seconds=self.timeout_seconds,
        )

        if self.mode in (MODE_NOTIFICATION, MODE_REMOTE):
            body = mask_text(pc.message or f"task {pc.task_id} needs approval")
            if notify_desktop(NOTIFY_TITLE, body):
                log.info(f"desktop notification raised for {pc.task_id}")
        if self.mode == MODE_REMOTE:
            self._send_remote(pc)
        return pc

    # -----------------------------------------------------------------------
    def _send_remote(self, pc: PendingConfirmation) -> bool:
        """Push the request over Track A's WS bridge. True when it went out.

        The bridge is a separate track and may not be installed at all, so this
        resolves it at call time and treats absence as a normal outcome.
        """
        bridge = self.ws_bridge or self._resolve_bridge()
        if bridge is None:
            log.info("remote approval mode requested but no WS bridge is running")
            return False
        message = {
            "type": "approval_request",
            "task_id": pc.task_id,
            "kind": pc.kind,
            "command": mask_text(pc.command),
            "reason": pc.reason,
            "timeout_seconds": self.timeout_seconds,
        }
        try:
            broadcast = getattr(bridge, "broadcast", None) or getattr(bridge, "send", None)
            if broadcast is None:
                log.warning("WS bridge exposes neither broadcast() nor send()")
                return False
            broadcast(message)
            log.info(f"approval request for {pc.task_id} sent over WS bridge")
            return True
        except Exception as e:
            log.warning(f"failed to send approval request over WS bridge: {e}")
            return False

    @staticmethod
    def _resolve_bridge() -> Any:
        """Return Track A's running bridge instance, or None."""
        try:
            try:
                import ws_bridge  # type: ignore
            except ImportError:
                from agent import ws_bridge  # type: ignore
        except ImportError:
            return None
        for name in ("get_bridge", "current_bridge", "instance"):
            getter = getattr(ws_bridge, name, None)
            if callable(getter):
                try:
                    return getter()
                except Exception:
                    return None
            if getter is not None:
                return getter
        return None

    # -----------------------------------------------------------------------
    def respond(self, task_id: str, approve: bool, *, actor: str = "user") -> Path:
        """Record a decision. Audited with who decided and when."""
        path = super().respond(task_id, approve=approve)
        self._audit(
            "approval_granted" if approve else "approval_denied",
            str(task_id),
            "ok" if approve else "deny",
            actor=actor,
            decided_at=time.time(),
        )
        return path

    # -----------------------------------------------------------------------
    def wait_for_response(
        self,
        task_id: str,
        *,
        timeout: Optional[float] = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> str:
        """Block until the request is answered. Returns the final status.

        ``"approved"``, ``"denied"``, or ``"denied"`` again after the deadline —
        an unanswered security prompt is a no. The auto-deny is written to the
        file queue as well, so a late human answer cannot silently revive a task
        the executor has already given up on.
        """
        deadline = time.time() + (self.timeout_seconds if timeout is None else timeout)
        while True:
            status = self.check_response(task_id)
            if status is not None:
                return status
            if time.time() >= deadline:
                log.warning(
                    f"approval for {task_id} unanswered after "
                    f"{self.timeout_seconds:.0f}s — auto-denying"
                )
                self.respond(task_id, approve=False, actor="timeout")
                self._audit(
                    "approval_timeout",
                    str(task_id),
                    "deny",
                    actor="timeout",
                    timeout_seconds=self.timeout_seconds,
                )
                return "denied"
            time.sleep(min(poll_interval, max(0.0, deadline - time.time())) or poll_interval)

    # -----------------------------------------------------------------------
    def sweep_timeouts(self) -> list[str]:
        """Auto-deny anything past ``approval.timeout_seconds``, and audit it."""
        now = time.time()
        denied: list[str] = []
        for p in sorted(self.local_dir.glob("pending-confirm-*.json")):
            task_id = p.stem[len("pending-confirm-"):]
            if self._has_response(task_id):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                created_ts = float(data.get("created_ts", now))
            except Exception as e:
                log.error(f"bad pending file {p}: {e}")
                continue
            if now - created_ts < self.timeout_seconds:
                continue
            log.warning(
                f"pending approval {task_id} timed out after "
                f"{self.timeout_seconds:.0f}s — auto-denying"
            )
            self.respond(task_id, approve=False, actor="timeout")
            self._audit(
                "approval_timeout",
                str(task_id),
                "deny",
                actor="timeout",
                age_seconds=round(now - created_ts, 3),
            )
            denied.append(task_id)
        return denied

    # -----------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """Effective configuration, for ``/health`` and for debugging."""
        return {
            "mode": self.mode,
            "timeout_seconds": self.timeout_seconds,
            "pending_dir": str(self.local_dir),
            "pending": len(self.list_pending()),
            "notifications": self.mode in (MODE_NOTIFICATION, MODE_REMOTE),
            "platform": platform.system(),
            "notify_backend": _notify_backend(),
        }


def _notify_backend() -> Optional[str]:
    """Name of the notification tool available here, or None."""
    system = platform.system()
    if system == "Linux":
        return "notify-send" if shutil.which("notify-send") else None
    if system == "Darwin":
        return "osascript" if shutil.which("osascript") else None
    if system == "Windows":
        return "powershell-toast" if shutil.which("powershell") else None
    return None

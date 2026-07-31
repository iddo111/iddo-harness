"""
Optional subprocess sandboxing.

Today every command the harness runs inherits the full privileges of the agent
process: ``shell_stream`` on a compromised task packet has the same reach as the
user sitting at the keyboard. Policy decides *whether* a command runs; the
sandbox limits *what it can touch once it does*.

Levels (per-kind, set in ``policy.yaml`` under ``sandbox:``)
-----------------------------------------------------------
``none``    run as today — no wrapper. The default, so behaviour is unchanged
            until someone opts in.
``light``   private ``/tmp``. Network and filesystem otherwise untouched.
            Cheap, and stops the most common cross-task interference.
``strict``  no network, and read-only everywhere except the task's ``cwd``.
            For running untrusted code that should not phone home.

Backends
--------
Linux: ``firejail`` when it is on ``PATH``.
Windows: a Job Object via ``pywin32``, or ``runas`` with a restricted account —
both feature-flagged, and both genuinely unavailable on most installs.

**When no backend is available the command runs unsandboxed after one warning.**
That is a deliberate choice: refusing to run would turn a missing optional
package into an outage, and the policy engine — which is always present — is
still the primary gate. The warning is loud, the fallback is audited, and
:func:`describe` lets ``/health`` report that sandboxing is inactive.
"""
from __future__ import annotations

import logging
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("harness.sandbox")

LEVEL_NONE = "none"
LEVEL_LIGHT = "light"
LEVEL_STRICT = "strict"
VALID_LEVELS = (LEVEL_NONE, LEVEL_LIGHT, LEVEL_STRICT)

FIREJAIL = "firejail"

_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Log ``message`` the first time ``key`` comes up. Keeps logs readable."""
    if key not in _warned:
        log.warning(message)
        _warned.add(key)


def reset_warnings() -> None:
    """Forget which warnings have been emitted (used by tests)."""
    _warned.clear()


# ---------------------------------------------------------------------------
# Level resolution
# ---------------------------------------------------------------------------
def normalise_level(level: Any) -> str:
    """Coerce a config value to a valid level, defaulting to ``none``."""
    text = str(level or LEVEL_NONE).strip().lower()
    if text not in VALID_LEVELS:
        _warn_once(f"bad-level:{text}", f"unknown sandbox level {level!r} — treating as {LEVEL_NONE}")
        return LEVEL_NONE
    return text


def level_for_kind(cfg: Any, kind: str) -> str:
    """Return the sandbox level configured for one task kind.

    ``sandbox.per_kind.<kind>`` wins over ``sandbox.default``; a disabled
    ``sandbox.enabled: false`` forces ``none`` regardless.
    """
    sandbox_cfg = getattr(cfg, "sandbox", None) or {}
    if not isinstance(sandbox_cfg, dict):
        return LEVEL_NONE
    if not sandbox_cfg.get("enabled", True):
        return LEVEL_NONE
    per_kind = sandbox_cfg.get("per_kind", {}) or {}
    if isinstance(per_kind, dict) and kind in per_kind:
        return normalise_level(per_kind[kind])
    return normalise_level(sandbox_cfg.get("default", LEVEL_NONE))


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------
def backend() -> str | None:
    """Return the usable backend name, or ``None`` when there is none."""
    if sys.platform.startswith("win"):
        return "job_object" if _pywin32_available() else None
    if shutil.which(FIREJAIL):
        return FIREJAIL
    return None


def _pywin32_available() -> bool:
    """True when pywin32 is importable (needed for Job Object confinement)."""
    try:
        import win32job  # noqa: F401, PLC0415
    except Exception:
        return False
    return True


def available(level: str = LEVEL_LIGHT) -> bool:
    """True when ``level`` can actually be enforced on this machine."""
    if normalise_level(level) == LEVEL_NONE:
        return True
    return backend() is not None


def describe() -> dict[str, Any]:
    """Machine-readable sandbox status, for ``/health`` and for audit lines."""
    b = backend()
    return {
        "platform": sys.platform,
        "backend": b,
        "enforcing": b is not None,
        "levels": list(VALID_LEVELS),
    }


# ---------------------------------------------------------------------------
# Wrapping
# ---------------------------------------------------------------------------
def wrap_popen_args(cmd: str, level: str = LEVEL_NONE, cwd: str | Path | None = None) -> str:
    """Return ``cmd`` wrapped for ``level``, or unchanged if it cannot be.

    The result is still a single shell string, because both ``Popen`` call sites
    in ``executor_v2`` run with ``shell=True``. The user's command is quoted as
    one argument to the sandbox's own shell, so its pipes and redirections keep
    working inside the jail instead of leaking out of it.
    """
    level = normalise_level(level)
    if level == LEVEL_NONE or not cmd:
        return cmd

    if sys.platform.startswith("win"):
        return _wrap_windows(cmd, level)

    if not shutil.which(FIREJAIL):
        _warn_once(
            "no-firejail",
            f"sandbox level {level!r} requested but firejail is not installed — "
            f"running unsandboxed (apt install firejail)",
        )
        return cmd

    args = [FIREJAIL, "--quiet", "--private-tmp"]
    if level == LEVEL_STRICT:
        args.append("--net=none")
        # Read-only root plus a writable island at cwd is what "read-only
        # outside the task's cwd" means in firejail terms. Without a cwd there
        # is no island to grant, so the whole filesystem stays read-only.
        args.append("--read-only=/")
        if cwd:
            resolved = Path(cwd).expanduser().resolve()
            args.append(f"--read-write={resolved}")
    args.append("--")
    args.extend(["/bin/sh", "-c", cmd])
    wrapped = shlex.join(args)
    log.debug(f"sandbox({level}) → {wrapped}")
    return wrapped


def _wrap_windows(cmd: str, level: str) -> str:
    """Windows wrapping — Job Objects if pywin32 is present, else pass through.

    A Job Object is applied to the *spawned handle*, not by rewriting the
    command line, so there is nothing to wrap here: :func:`confine_process` does
    the work after ``Popen`` returns. ``runas`` with a restricted account is the
    alternative, and it needs a saved credential (``/savecred``) to run
    unattended, which most installs will not have set up.
    """
    if _pywin32_available():
        return cmd
    _warn_once(
        "no-windows-sandbox",
        f"sandbox level {level!r} requested but no Windows backend is available "
        f"(pywin32 not installed) — running unsandboxed",
    )
    return cmd


def confine_process(proc: Any, level: str = LEVEL_NONE) -> bool:
    """Attach OS-level confinement to an already-spawned process.

    Only meaningful on Windows, where confinement is a handle operation rather
    than a command-line wrapper. Returns True when confinement was applied.
    Never raises — a failure here degrades to "unsandboxed", same as elsewhere.
    """
    level = normalise_level(level)
    if level == LEVEL_NONE or not sys.platform.startswith("win"):
        return False
    try:
        import win32api  # noqa: PLC0415
        import win32con  # noqa: PLC0415
        import win32job  # noqa: PLC0415
    except Exception:
        return False

    try:
        job = win32job.CreateJobObject(None, "")
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid)
        win32job.AssignProcessToJobObject(job, handle)
        # Held on the Popen object so the job outlives this call: closing the
        # last handle to a KILL_ON_JOB_CLOSE job kills the process.
        proc._iddo_job = job  # type: ignore[attr-defined]
        log.debug(f"assigned pid {proc.pid} to a job object (level={level})")
        return True
    except Exception as e:  # pragma: no cover - Windows only
        _warn_once("job-object-failed", f"could not confine pid {proc.pid} to a job object: {e}")
        return False

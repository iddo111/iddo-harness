"""
Append-only, tamper-evident audit log.

Every security-relevant thing the harness does gets one JSON line here: policy
decisions (with the rule that matched), secret reads, kills and cancels, key
use, approvals, startup and shutdown. One line, one event, never rewritten.

Tamper-evidence
---------------
An append-only file is only as trustworthy as the filesystem under it — anyone
who can edit ``audit.log`` can also delete the line that says they did. So each
line carries a keyed hash that includes the previous line's hash::

    hmac(N) = HMAC-SHA256(key, hmac(N-1) || canonical_json(record without hmac))

Editing, reordering, or deleting any line breaks every hash from that point on,
and forging a repair needs ``~/.iddo-harness/audit.key`` (0600, generated on
first use). :func:`verify_chain` reports the first line that fails.

File layout
-----------
``~/.iddo-harness/audit.jsonl``              today's log
``~/.iddo-harness/audit-YYYY-MM-DD.jsonl.gz`` rotated, gzipped

**Deviation from the brief:** the brief specifies ``~/.iddo-harness/audit.log``,
but that path is already the sink of ``logging.FileHandler`` (``agent/cli.py``,
``agent/main.py``) and holds free-form human log lines. A hash chain cannot
share a file with another writer — one interleaved log line and every
subsequent HMAC is invalid. The structured log therefore lives beside it as
``audit.jsonl``; ``audit.log`` keeps its existing meaning, and ``iddo-harness
tail`` keeps working.

The chain restarts (``prev == ""``) at the top of each daily file. A verifier
checking continuity across days must walk the rotated files in order.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import os
import shutil
import stat
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger("harness.audit")

DEFAULT_DIR = Path.home() / ".iddo-harness"
DEFAULT_LOG_NAME = "audit.jsonl"
DEFAULT_KEY_NAME = "audit.key"

#: Fields of a record that participate in the hash, in canonical order.
HASHED_FIELDS = ("ts", "actor", "action", "resource", "outcome", "meta", "prev")

VALID_OUTCOMES = frozenset({"ok", "deny", "error"})

_KEY_BYTES = 32


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    """UTC timestamp, second-resolution, Zulu suffix — same shape as amp.py."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(record: dict[str, Any]) -> bytes:
    """Canonical bytes of a record for hashing (same rules as agent/signing.py)."""
    subset = {k: record[k] for k in HASHED_FIELDS if k in record}
    return json.dumps(subset, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def _mask(value: Any) -> Any:
    """Recursively strip secret material out of anything about to be written.

    The audit log is the one file guaranteed to be read by a human later, which
    makes it the worst possible place for a credential to land. Masking happens
    here rather than at each call site so a new caller cannot forget it.
    """
    try:
        from secrets_vault import mask_text  # noqa: PLC0415
    except ImportError:  # pragma: no cover - packaged imports
        try:
            from agent.secrets_vault import mask_text  # noqa: PLC0415
        except ImportError:  # pragma: no cover - secrets module absent
            return value

    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: _mask(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
class AuditLog:
    """A hash-chained JSON-lines audit log with daily rotation."""

    def __init__(
        self,
        path: Path | str | None = None,
        key_path: Path | str | None = None,
        *,
        rotate: bool = True,
    ) -> None:
        self.path = Path(path) if path else DEFAULT_DIR / DEFAULT_LOG_NAME
        self.key_path = Path(key_path) if key_path else self.path.parent / DEFAULT_KEY_NAME
        self.rotate_daily = rotate
        self._lock = threading.Lock()
        self._key: bytes | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: Any) -> "AuditLog":
        """Build from the ``security.audit`` block of policy.yaml."""
        security = getattr(cfg, "security", None) or {}
        audit_cfg = (security.get("audit") or {}) if isinstance(security, dict) else {}
        return cls(
            path=audit_cfg.get("path"),
            key_path=audit_cfg.get("key_path"),
            rotate=bool(audit_cfg.get("rotate_daily", True)),
        )

    # -----------------------------------------------------------------------
    @property
    def key(self) -> bytes:
        """The HMAC key, generated on first use with 0600 permissions."""
        if self._key is None:
            if self.key_path.exists():
                self._key = self.key_path.read_bytes()
            else:
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                self._key = os.urandom(_KEY_BYTES)
                self.key_path.write_bytes(self._key)
                try:
                    os.chmod(self.key_path, stat.S_IRUSR | stat.S_IWUSR)
                except OSError as e:  # pragma: no cover - Windows / exotic FS
                    log.warning(f"could not restrict permissions on {self.key_path}: {e}")
                log.info(f"generated audit chain key: {self.key_path}")
        return self._key

    def _hmac(self, record: dict[str, Any]) -> str:
        """HMAC of one record, chained through its ``prev`` field."""
        mac = hmac.new(self.key, digestmod=hashlib.sha256)
        mac.update(record.get("prev", "").encode("ascii"))
        mac.update(_canonical(record))
        return mac.hexdigest()

    # -----------------------------------------------------------------------
    def record(
        self,
        actor: str,
        action: str,
        resource: str = "",
        outcome: str = "ok",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one event and return the written record.

        ``actor`` is ``"harness"``, ``"user"``, or a task id. ``outcome`` is
        ``ok`` / ``deny`` / ``error``. Never raises: an audit failure must not
        take down the operation being audited, it just logs loudly.
        """
        if outcome not in VALID_OUTCOMES:
            outcome = "error"
        record = {
            "ts": _now_iso(),
            "actor": _mask(str(actor)),
            "action": _mask(str(action)),
            "resource": _mask(str(resource)),
            "outcome": outcome,
            "meta": _mask(meta or {}),
        }
        try:
            with self._lock:
                self._maybe_rotate()
                record["prev"] = self._last_hmac()
                record["hmac"] = self._hmac(record)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except Exception:
            log.exception(f"failed to append audit record: {action}")
        return record

    # -----------------------------------------------------------------------
    def _last_hmac(self) -> str:
        """HMAC of the final line, or ``""`` when the file is empty/new."""
        if not self.path.exists():
            return ""
        last = ""
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if not last:
            return ""
        try:
            return json.loads(last).get("hmac", "")
        except json.JSONDecodeError:
            # A truncated tail (killed mid-write) would otherwise chain onto
            # nothing forever. Surface it and start a fresh genesis link so the
            # break stays visible at exactly one line instead of all of them.
            log.error(f"audit log tail is not valid JSON — chain restarts at {self.path}")
            return ""

    # -----------------------------------------------------------------------
    def _maybe_rotate(self) -> None:
        """Move yesterday's log to ``audit-<date>.jsonl.gz`` before appending."""
        if not self.rotate_daily or not self.path.exists():
            return
        try:
            mtime = datetime.fromtimestamp(self.path.stat().st_mtime).date()
        except OSError:  # pragma: no cover - stat race
            return
        if mtime >= date.today():
            return

        rotated = self.path.with_name(f"audit-{mtime.isoformat()}.jsonl")
        gz_path = rotated.with_suffix(rotated.suffix + ".gz")
        try:
            with self.path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            self.path.unlink()
            log.info(f"rotated audit log to {gz_path}")
        except Exception:
            log.exception("audit rotation failed — continuing to append to today's file")

    # -----------------------------------------------------------------------
    def tail(self, n: int = 100) -> list[dict[str, Any]]:
        """Return the last ``n`` records, oldest first. Unparseable lines are skipped."""
        if not self.path.exists():
            return []
        lines = [ln for ln in self.path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        out = []
        for line in lines[-max(0, n):]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # -----------------------------------------------------------------------
    def verify_chain(self) -> tuple[bool, int | None, str]:
        """Verify the whole chain.

        Returns ``(ok, first_bad_line_number, reason)`` with 1-based line
        numbers; ``(True, None, "")`` for an intact (or empty) log.
        """
        return verify_chain(self.path, self.key)


# ---------------------------------------------------------------------------
def verify_chain(path: Path | str, key: bytes) -> tuple[bool, int | None, str]:
    """Verify a hash-chained audit file against ``key``.

    Standalone so a verifier (or the health endpoint) can check a rotated
    ``.jsonl.gz`` without constructing an :class:`AuditLog` that would want to
    append to it.
    """
    p = Path(path)
    if not p.exists():
        return True, None, ""

    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
        lines = [ln for ln in fh if ln.strip()]

    prev = ""
    for i, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            return False, i, f"line {i} is not valid JSON: {e}"
        if record.get("prev", "") != prev:
            return False, i, f"line {i} does not chain onto line {i - 1}"
        expected = record.get("hmac", "")
        mac = hmac.new(key, digestmod=hashlib.sha256)
        mac.update(prev.encode("ascii"))
        mac.update(_canonical(record))
        if not hmac.compare_digest(mac.hexdigest(), expected):
            return False, i, f"line {i} has a bad HMAC — content was altered"
        prev = expected
    return True, None, ""


# ---------------------------------------------------------------------------
# Module-level default instance
# ---------------------------------------------------------------------------
_default: AuditLog | None = None
_default_lock = threading.Lock()


def get_audit_log(cfg: Any | None = None) -> AuditLog:
    """Return the process-wide audit log, building it on first use.

    Shared rather than per-caller so that every subsystem appends to the same
    chain — two writers with two ``_last_hmac`` views would fork it.
    """
    global _default
    with _default_lock:
        if _default is None:
            _default = AuditLog.from_config(cfg) if cfg is not None else AuditLog()
        return _default


def set_audit_log(audit: AuditLog | None) -> None:
    """Replace the process-wide audit log (used by main() and by tests)."""
    global _default
    with _default_lock:
        _default = audit


def record(
    actor: str,
    action: str,
    resource: str = "",
    outcome: str = "ok",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append to the process-wide audit log. See :meth:`AuditLog.record`."""
    return get_audit_log().record(actor, action, resource, outcome, meta)


def iter_records(path: Path | str) -> Iterator[dict[str, Any]]:
    """Yield every parseable record from a plain or gzipped audit file."""
    p = Path(path)
    if not p.exists():
        return
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
        for line in fh:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def rotated_files(directory: Path | str | None = None) -> Iterable[Path]:
    """Return rotated audit files, oldest first."""
    d = Path(directory) if directory else DEFAULT_DIR
    if not d.exists():
        return []
    return sorted(d.glob("audit-*.jsonl*"))

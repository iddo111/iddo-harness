"""
Memory store — durable key/value state that outlives a single task packet.

Every v1/v2 kind is stateless: a task arrives, it runs, its result is written,
and nothing it learned survives. That is fine for ``read_file`` and hopeless
for an agent that is supposed to notice "I already installed that", "the build
broke last night", or "the user prefers pnpm".

This module is that missing state: a SQLite table keyed by
``(namespace, key)``, holding a JSON-encoded value plus tags and an optional
expiry. SQLite because it is in the standard library, survives a restart,
tolerates two harness processes on the same file, and does not need a server.

Values are JSON, not pickle: a memory row is meant to be readable by a human
with ``sqlite3`` and by a producer that is not Python.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("harness.memory")

DEFAULT_NAMESPACE = "default"

#: Ceiling on a single stored value, in bytes of encoded JSON. The store is
#: for facts and preferences, not for build logs — a caller that wants to
#: remember a 50 MB artifact should remember its *path*.
MAX_VALUE_BYTES = 1_048_576

MAX_KEY_LENGTH = 512

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL,
    PRIMARY KEY (namespace, key)
);
CREATE INDEX IF NOT EXISTS memory_expires ON memory (expires_at);
CREATE INDEX IF NOT EXISTS memory_ns ON memory (namespace);
"""


class MemoryError(RuntimeError):
    """Raised for a caller mistake: bad key, oversized value, unusable value."""


@dataclass
class MemoryRecord:
    """One stored entry, as returned by :meth:`MemoryStore.get_record`."""

    namespace: str
    key: str
    value: Any
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float | None = None

    @property
    def ttl_remaining(self) -> float | None:
        """Seconds until expiry, or ``None`` when the entry never expires."""
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - time.time())

    def to_dict(self, *, include_value: bool = True) -> dict[str, Any]:
        """Render as a JSON-safe dict for a task result body."""
        body: dict[str, Any] = {
            "namespace": self.namespace,
            "key": self.key,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "ttl_remaining": self.ttl_remaining,
        }
        if include_value:
            body["value"] = self.value
        return body


def default_db_path() -> Path:
    """Resolve the store location against the *current* home directory.

    Resolved on call rather than at import so a test that redirects
    ``Path.home()`` actually gets a redirected database.
    """
    return Path.home() / ".iddo-harness" / "memory.db"


def _normalise_tags(tags: Iterable[str] | str | None) -> list[str]:
    """Accept a list, a comma-separated string, or nothing."""
    if tags is None:
        return []
    if isinstance(tags, str):
        raw: Iterable[str] = tags.split(",")
    else:
        raw = tags
    seen: list[str] = []
    for tag in raw:
        text = str(tag).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


class MemoryStore:
    """A namespaced, optionally-expiring key/value store backed by SQLite.

    One connection is shared across threads with ``check_same_thread=False``
    and guarded by a lock: the harness runs tasks concurrently, and SQLite's
    own locking would otherwise surface as ``database is locked`` under a
    burst of writes rather than simply serialising them.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- writing ------------------------------------------------------------

    def set(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        ttl_seconds: float | None = None,
        tags: Iterable[str] | str | None = None,
    ) -> MemoryRecord:
        """Store ``value`` under ``key``, replacing any existing entry.

        ``created_at`` is preserved across an overwrite: the interesting
        question about a remembered fact is usually "since when", and an
        update should not erase that.
        """
        key = self._check_key(key)
        namespace = self._check_namespace(namespace)
        encoded = self._encode(value)
        tag_list = _normalise_tags(tags)
        now = time.time()
        expires_at = None if ttl_seconds is None else now + float(ttl_seconds)
        if ttl_seconds is not None and float(ttl_seconds) <= 0:
            raise MemoryError("ttl_seconds must be positive")

        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM memory WHERE namespace = ? AND key = ?", (namespace, key)
            ).fetchone()
            created_at = float(row["created_at"]) if row is not None else now
            self._conn.execute(
                "INSERT OR REPLACE INTO memory"
                " (namespace, key, value, tags, created_at, updated_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (namespace, key, encoded, ",".join(tag_list), created_at, now, expires_at),
            )
            self._conn.commit()

        log.debug("memory set %s/%s (ttl=%s)", namespace, key, ttl_seconds)
        return MemoryRecord(
            namespace=namespace,
            key=key,
            value=value,
            tags=tag_list,
            created_at=created_at,
            updated_at=now,
            expires_at=expires_at,
        )

    def delete(self, key: str, *, namespace: str = DEFAULT_NAMESPACE) -> bool:
        """Remove one entry. Returns whether it was there to begin with."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM memory WHERE namespace = ? AND key = ?", (namespace, key)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def clear(self, *, namespace: str | None = None) -> int:
        """Drop a whole namespace, or everything when ``namespace`` is None."""
        with self._lock:
            if namespace is None:
                cur = self._conn.execute("DELETE FROM memory")
            else:
                cur = self._conn.execute("DELETE FROM memory WHERE namespace = ?", (namespace,))
            self._conn.commit()
            return cur.rowcount

    def purge_expired(self, *, now: float | None = None) -> int:
        """Delete every entry whose TTL has run out. Returns the count."""
        moment = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM memory WHERE expires_at IS NOT NULL AND expires_at <= ?", (moment,)
            )
            self._conn.commit()
            return cur.rowcount

    # -- reading ------------------------------------------------------------

    def get(self, key: str, *, namespace: str = DEFAULT_NAMESPACE, default: Any = None) -> Any:
        """Return the stored value, or ``default`` when absent or expired."""
        record = self.get_record(key, namespace=namespace)
        return default if record is None else record.value

    def get_record(self, key: str, *, namespace: str = DEFAULT_NAMESPACE) -> MemoryRecord | None:
        """Return the full entry, or ``None`` when absent or expired.

        An expired row is deleted on read rather than merely hidden, so a
        store that is only ever read through :meth:`get` still drains.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory WHERE namespace = ? AND key = ?", (namespace, key)
            ).fetchone()
        if row is None:
            return None
        record = self._row_to_record(row)
        if self._is_expired(record):
            self.delete(key, namespace=namespace)
            return None
        return record

    def exists(self, key: str, *, namespace: str = DEFAULT_NAMESPACE) -> bool:
        """True when a live (non-expired) entry exists."""
        return self.get_record(key, namespace=namespace) is not None

    def list(
        self,
        *,
        namespace: str | None = DEFAULT_NAMESPACE,
        prefix: str | None = None,
        tag: str | None = None,
        limit: int | None = None,
        include_expired: bool = False,
    ) -> list[MemoryRecord]:
        """List entries, newest update first.

        ``namespace=None`` searches every namespace — the way to answer "what
        does this harness know at all".
        """
        sql = "SELECT * FROM memory"
        clauses: list[str] = []
        params: list[Any] = []
        if namespace is not None:
            clauses.append("namespace = ?")
            params.append(namespace)
        if prefix:
            clauses.append("key LIKE ? ESCAPE '\\'")
            params.append(_like_prefix(prefix))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, key ASC"

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        records = [self._row_to_record(r) for r in rows]
        if not include_expired:
            records = [r for r in records if not self._is_expired(r)]
        if tag:
            records = [r for r in records if tag in r.tags]
        if limit is not None:
            records = records[: max(0, int(limit))]
        return records

    def namespaces(self) -> list[str]:
        """Every namespace that currently holds at least one row."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT namespace FROM memory ORDER BY namespace"
            ).fetchall()
        return [r["namespace"] for r in rows]

    def count(self, *, namespace: str | None = None) -> int:
        """Number of live entries, in one namespace or across all of them."""
        return len(self.list(namespace=namespace))

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection. Safe to call twice."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:  # pragma: no cover - already closed
                pass

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _is_expired(record: MemoryRecord, *, now: float | None = None) -> bool:
        if record.expires_at is None:
            return False
        return record.expires_at <= (time.time() if now is None else now)

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            namespace=row["namespace"],
            key=row["key"],
            value=json.loads(row["value"]),
            tags=[t for t in row["tags"].split(",") if t],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            expires_at=None if row["expires_at"] is None else float(row["expires_at"]),
        )

    @staticmethod
    def _check_key(key: Any) -> str:
        text = str(key or "").strip()
        if not text:
            raise MemoryError("key must be a non-empty string")
        if len(text) > MAX_KEY_LENGTH:
            raise MemoryError(f"key exceeds {MAX_KEY_LENGTH} characters")
        return text

    @staticmethod
    def _check_namespace(namespace: Any) -> str:
        text = str(namespace or "").strip()
        return text or DEFAULT_NAMESPACE

    @staticmethod
    def _encode(value: Any) -> str:
        try:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise MemoryError(f"value is not JSON-serialisable: {exc}") from exc
        if len(encoded.encode("utf-8")) > MAX_VALUE_BYTES:
            raise MemoryError(f"value exceeds {MAX_VALUE_BYTES} bytes; store a path instead")
        return encoded


def _like_prefix(prefix: str) -> str:
    """Escape LIKE wildcards so a key prefix containing ``%`` still works."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"

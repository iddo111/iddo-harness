"""
Tests for the SQLite memory store (agent/memory.py).

Every test gets its own database file under ``tmp_path`` so nothing here
touches the real ``~/.iddo-harness/memory.db``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from memory import (
    DEFAULT_NAMESPACE,
    MAX_KEY_LENGTH,
    MAX_VALUE_BYTES,
    MemoryError as MemoryStoreError,
    MemoryRecord,
    MemoryStore,
    _like_prefix,
    _normalise_tags,
    default_db_path,
)


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(tmp_path / "memory.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------
def test_set_and_get_round_trips(store: MemoryStore) -> None:
    store.set("pm", "pnpm")
    assert store.get("pm") == "pnpm"


@pytest.mark.parametrize(
    "value",
    [42, 3.5, True, None, "text", [1, 2, 3], {"nested": {"a": [1, {"b": 2}]}}],
)
def test_json_types_survive_a_round_trip(store: MemoryStore, value: object) -> None:
    store.set("k", value)
    assert store.get("k") == value


def test_get_returns_default_when_absent(store: MemoryStore) -> None:
    assert store.get("nope") is None
    assert store.get("nope", default="fallback") == "fallback"


def test_namespaces_are_isolated(store: MemoryStore) -> None:
    store.set("k", "a", namespace="one")
    store.set("k", "b", namespace="two")
    assert store.get("k", namespace="one") == "a"
    assert store.get("k", namespace="two") == "b"
    assert store.namespaces() == ["one", "two"]


def test_missing_namespace_falls_back_to_default(store: MemoryStore) -> None:
    record = store.set("k", "v", namespace="")
    assert record.namespace == DEFAULT_NAMESPACE


def test_overwrite_preserves_created_at(store: MemoryStore) -> None:
    first = store.set("k", "v1")
    time.sleep(0.01)
    second = store.set("k", "v2")
    assert second.created_at == first.created_at
    assert second.updated_at > first.updated_at
    assert store.get("k") == "v2"


def test_survives_reopening_the_file(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    first = MemoryStore(path)
    first.set("durable", {"yes": True})
    first.close()

    second = MemoryStore(path)
    assert second.get("durable") == {"yes": True}
    second.close()


def test_values_are_readable_as_plain_json(tmp_path: Path) -> None:
    """A row must be legible to sqlite3 and to a non-Python producer."""
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    store.set("k", {"a": 1})
    store.close()

    conn = sqlite3.connect(str(path))
    raw = conn.execute("SELECT value FROM memory WHERE key = 'k'").fetchone()[0]
    conn.close()
    assert json.loads(raw) == {"a": 1}


def test_in_memory_database_is_supported() -> None:
    store = MemoryStore(":memory:")
    store.set("k", "v")
    assert store.get("k") == "v"
    store.close()


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------
def test_delete_reports_whether_it_existed(store: MemoryStore) -> None:
    store.set("k", "v")
    assert store.delete("k") is True
    assert store.delete("k") is False
    assert store.get("k") is None


def test_clear_drops_one_namespace(store: MemoryStore) -> None:
    store.set("a", 1, namespace="keep")
    store.set("b", 2, namespace="drop")
    store.set("c", 3, namespace="drop")
    assert store.clear(namespace="drop") == 2
    assert store.namespaces() == ["keep"]


def test_clear_with_no_namespace_drops_everything(store: MemoryStore) -> None:
    store.set("a", 1, namespace="one")
    store.set("b", 2, namespace="two")
    assert store.clear() == 2
    assert store.namespaces() == []


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------
def test_ttl_hides_the_entry_once_it_lapses(store: MemoryStore) -> None:
    # A 50ms TTL races the assertion below on a loaded machine: the row can
    # lapse before the read that is supposed to still see it.
    store.set("temp", "v", ttl_seconds=0.5)
    assert store.get("temp") == "v"
    time.sleep(0.7)
    assert store.get("temp") is None


def test_expired_entry_is_deleted_on_read(store: MemoryStore) -> None:
    """A store read only through get() must still drain, not just hide rows."""
    store.set("temp", "v", ttl_seconds=0.05)
    time.sleep(0.08)
    assert store.get_record("temp") is None
    assert store.list(include_expired=True) == []


def test_purge_expired_counts_what_it_removed(store: MemoryStore) -> None:
    store.set("live", "v")
    store.set("gone", "v", ttl_seconds=0.05)
    time.sleep(0.08)
    assert store.purge_expired() == 1
    assert [r.key for r in store.list()] == ["live"]


def test_ttl_remaining_counts_down(store: MemoryStore) -> None:
    record = store.set("k", "v", ttl_seconds=60)
    assert 0 < (record.ttl_remaining or 0) <= 60
    assert store.set("j", "v").ttl_remaining is None


def test_non_positive_ttl_is_rejected(store: MemoryStore) -> None:
    with pytest.raises(MemoryStoreError, match="positive"):
        store.set("k", "v", ttl_seconds=0)


def test_exists_ignores_expired_entries(store: MemoryStore) -> None:
    store.set("k", "v", ttl_seconds=0.5)
    assert store.exists("k") is True
    time.sleep(0.7)
    assert store.exists("k") is False


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def test_list_is_newest_update_first(store: MemoryStore) -> None:
    store.set("old", 1)
    time.sleep(0.01)
    store.set("new", 2)
    assert [r.key for r in store.list()] == ["new", "old"]


def test_list_filters_by_prefix(store: MemoryStore) -> None:
    store.set("build:one", 1)
    store.set("build:two", 2)
    store.set("other", 3)
    assert sorted(r.key for r in store.list(prefix="build:")) == ["build:one", "build:two"]


def test_prefix_containing_a_wildcard_is_taken_literally(store: MemoryStore) -> None:
    store.set("100%done", 1)
    store.set("100xdone", 2)
    assert [r.key for r in store.list(prefix="100%")] == ["100%done"]


def test_list_filters_by_tag(store: MemoryStore) -> None:
    store.set("a", 1, tags=["ci", "build"])
    store.set("b", 2, tags=["build"])
    store.set("c", 3)
    assert sorted(r.key for r in store.list(tag="ci")) == ["a"]
    assert sorted(r.key for r in store.list(tag="build")) == ["a", "b"]


def test_list_across_all_namespaces(store: MemoryStore) -> None:
    store.set("a", 1, namespace="one")
    store.set("b", 2, namespace="two")
    assert len(store.list(namespace=None)) == 2
    assert len(store.list(namespace="one")) == 1


def test_list_respects_the_limit(store: MemoryStore) -> None:
    for i in range(5):
        store.set(f"k{i}", i)
    assert len(store.list(limit=2)) == 2


def test_count_matches_the_live_rows(store: MemoryStore) -> None:
    store.set("a", 1)
    store.set("b", 2, ttl_seconds=0.5)
    assert store.count() == 2
    time.sleep(0.7)
    assert store.count() == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["", "   ", None])
def test_empty_key_is_rejected(store: MemoryStore, key: object) -> None:
    with pytest.raises(MemoryStoreError, match="non-empty"):
        store.set(key, "v")  # type: ignore[arg-type]


def test_overlong_key_is_rejected(store: MemoryStore) -> None:
    with pytest.raises(MemoryStoreError, match="exceeds"):
        store.set("k" * (MAX_KEY_LENGTH + 1), "v")


def test_unserialisable_value_is_rejected(store: MemoryStore) -> None:
    with pytest.raises(MemoryStoreError, match="JSON"):
        store.set("k", object())


def test_oversized_value_is_rejected(store: MemoryStore) -> None:
    with pytest.raises(MemoryStoreError, match="store a path instead"):
        store.set("k", "x" * (MAX_VALUE_BYTES + 10))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "given,expected",
    [
        (None, []),
        ("a,b", ["a", "b"]),
        ("a, b , a", ["a", "b"]),
        (["x", "", "x", "y"], ["x", "y"]),
    ],
)
def test_normalise_tags(given: object, expected: list[str]) -> None:
    assert _normalise_tags(given) == expected  # type: ignore[arg-type]


def test_like_prefix_escapes_wildcards() -> None:
    assert _like_prefix("a_b") == "a\\_b%"
    assert _like_prefix("50%") == "50\\%%"


def test_default_db_path_follows_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_db_path() == tmp_path / ".iddo-harness" / "memory.db"


def test_record_to_dict_can_omit_the_value() -> None:
    record = MemoryRecord(namespace="n", key="k", value="secret", tags=["t"])
    assert "value" not in record.to_dict(include_value=False)
    assert record.to_dict()["value"] == "secret"


def test_close_is_idempotent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "m.db")
    store.close()
    store.close()

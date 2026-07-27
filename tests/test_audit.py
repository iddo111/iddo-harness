"""
Tests for agent/audit.py.

The point of the chained HMAC is that an attacker with write access to the log
cannot quietly remove or rewrite an entry. So the tests here mostly break the
file on purpose and assert that ``verify_chain`` names the line that broke.
"""
import gzip
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

import audit as audit_mod
from audit import AuditLog


@pytest.fixture
def log(tmp_path):
    return AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")


def _lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
def test_record_appends_one_json_line(log):
    log.record(actor="harness", action="agent_startup", resource="repo")
    assert len(_lines(log.path)) == 1


def test_record_has_the_documented_fields(log):
    rec = log.record(actor="task-1", action="policy_decision:auto", resource="ls -la")
    for field in ("ts", "actor", "action", "resource", "outcome", "meta", "prev", "hmac"):
        assert field in rec


def test_record_defaults_to_ok(log):
    assert log.record(actor="harness", action="startup")["outcome"] == "ok"


def test_invalid_outcome_becomes_error(log):
    assert log.record(actor="harness", action="x", outcome="banana")["outcome"] == "error"


def test_meta_is_preserved(log):
    rec = log.record(actor="t", action="a", meta={"rule": "auto-allowed: ls*", "n": 3})
    assert rec["meta"]["rule"] == "auto-allowed: ls*"
    assert _lines(log.path)[0]["meta"]["n"] == 3


def test_timestamps_are_iso8601(log):
    ts = log.record(actor="harness", action="startup")["ts"]
    assert ts.endswith("Z") or "+" in ts
    assert "T" in ts


def test_key_is_generated_on_first_use(log):
    assert not log.key_path.exists()
    log.record(actor="harness", action="startup")
    assert log.key_path.exists()
    assert len(log.key_path.read_bytes()) >= 16


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_key_is_not_world_readable(log):
    log.record(actor="harness", action="startup")
    assert log.key_path.stat().st_mode & 0o077 == 0


def test_existing_key_is_reused(tmp_path):
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"x" * 32)
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=key_path)
    assert log.key == b"x" * 32


def test_record_never_raises_on_an_unwritable_path(tmp_path):
    # A directory where the log file should be: every append fails, and the
    # caller must not notice.
    bad = tmp_path / "audit.jsonl"
    bad.mkdir()
    log = AuditLog(path=bad, key_path=tmp_path / "audit.key")
    assert log.record(actor="harness", action="startup")["action"] == "startup"


# ---------------------------------------------------------------------------
# Chaining
# ---------------------------------------------------------------------------
def test_first_record_has_an_empty_prev(log):
    assert log.record(actor="harness", action="startup")["prev"] == ""


def test_each_record_chains_onto_the_previous_hmac(log):
    first = log.record(actor="harness", action="one")
    second = log.record(actor="harness", action="two")
    assert second["prev"] == first["hmac"]


def test_verify_chain_accepts_an_intact_log(log):
    for i in range(5):
        log.record(actor="harness", action=f"event-{i}")
    ok, line, detail = log.verify_chain()
    assert (ok, line, detail) == (True, None, "")


def test_verify_chain_accepts_an_empty_log(log):
    assert log.verify_chain()[0] is True


def test_editing_a_record_breaks_the_chain(log):
    for i in range(3):
        log.record(actor="harness", action=f"event-{i}")

    records = _lines(log.path)
    records[1]["resource"] = "rm -rf /"
    log.path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in records) + "\n",
        encoding="utf-8",
    )

    ok, line, detail = log.verify_chain()
    assert ok is False
    assert line == 2
    assert "HMAC" in detail


def test_deleting_a_record_breaks_the_chain(log):
    for i in range(4):
        log.record(actor="harness", action=f"event-{i}")

    records = _lines(log.path)
    del records[1]
    log.path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in records) + "\n",
        encoding="utf-8",
    )

    ok, line, _ = log.verify_chain()
    assert ok is False
    assert line == 2


def test_reordering_records_breaks_the_chain(log):
    for i in range(3):
        log.record(actor="harness", action=f"event-{i}")
    records = _lines(log.path)
    records[0], records[1] = records[1], records[0]
    log.path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n", encoding="utf-8"
    )
    assert log.verify_chain()[0] is False


def test_appending_a_forged_record_breaks_the_chain(log):
    log.record(actor="harness", action="startup")
    forged = {
        "ts": "2026-07-25T00:00:00Z", "actor": "harness", "action": "policy_decision:auto",
        "resource": "rm -rf /", "outcome": "ok", "meta": {}, "prev": "", "hmac": "0" * 64,
    }
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(forged, sort_keys=True) + "\n")
    ok, line, _ = log.verify_chain()
    assert (ok, line) == (False, 2)


def test_truncated_json_line_is_reported(log):
    log.record(actor="harness", action="startup")
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026')
    ok, line, detail = log.verify_chain()
    assert ok is False
    assert line == 2
    assert "JSON" in detail


def test_verify_chain_with_the_wrong_key_fails(log, tmp_path):
    log.record(actor="harness", action="startup")
    ok, line, _ = audit_mod.verify_chain(log.path, b"y" * 32)
    assert (ok, line) == (False, 1)


def test_standalone_verify_of_a_gzipped_file(log, tmp_path):
    for i in range(3):
        log.record(actor="harness", action=f"event-{i}")
    gz = tmp_path / "audit-2026-07-24.jsonl.gz"
    with log.path.open("rb") as src, gzip.open(gz, "wb") as dst:
        dst.write(src.read())
    assert audit_mod.verify_chain(gz, log.key)[0] is True


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------
def test_yesterdays_log_is_rotated_and_gzipped(log):
    log.record(actor="harness", action="from-yesterday")
    yesterday = date.today() - timedelta(days=1)
    old = time.mktime(yesterday.timetuple())
    os.utime(log.path, (old, old))

    log.record(actor="harness", action="from-today")

    gz = log.path.with_name(f"audit-{yesterday.isoformat()}.jsonl.gz")
    assert gz.exists()
    with gzip.open(gz, "rt", encoding="utf-8") as fh:
        assert "from-yesterday" in fh.read()
    assert [r["action"] for r in _lines(log.path)] == ["from-today"]


def test_rotation_can_be_switched_off(tmp_path):
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key", rotate=False)
    log.record(actor="harness", action="one")
    old = time.mktime((date.today() - timedelta(days=2)).timetuple())
    os.utime(log.path, (old, old))
    log.record(actor="harness", action="two")
    assert len(_lines(log.path)) == 2
    assert not list(tmp_path.glob("audit-*.jsonl.gz"))


def test_rotated_files_are_listed(log):
    log.record(actor="harness", action="one")
    yesterday = date.today() - timedelta(days=1)
    old = time.mktime(yesterday.timetuple())
    os.utime(log.path, (old, old))
    log.record(actor="harness", action="two")
    assert any("audit-" in p.name for p in audit_mod.rotated_files(log.path.parent))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def test_tail_returns_the_last_n_oldest_first(log):
    for i in range(10):
        log.record(actor="harness", action=f"event-{i}")
    tail = log.tail(3)
    assert [r["action"] for r in tail] == ["event-7", "event-8", "event-9"]


def test_tail_of_a_missing_file_is_empty(tmp_path):
    assert AuditLog(path=tmp_path / "absent.jsonl", key_path=tmp_path / "k").tail(5) == []


def test_tail_skips_an_unparseable_line(log):
    log.record(actor="harness", action="good")
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
    assert [r["action"] for r in log.tail(10)] == ["good"]


def test_iter_records_reads_a_plain_file(log):
    log.record(actor="harness", action="one")
    assert [r["action"] for r in audit_mod.iter_records(log.path)] == ["one"]


# ---------------------------------------------------------------------------
# Config + module-level instance
# ---------------------------------------------------------------------------
def test_from_config_reads_the_security_block(tmp_path):
    class Cfg:
        security = {
            "audit": {
                "path": str(tmp_path / "custom.jsonl"),
                "key_path": str(tmp_path / "custom.key"),
                "rotate_daily": False,
            }
        }

    log = AuditLog.from_config(Cfg())
    assert log.path == tmp_path / "custom.jsonl"
    assert log.rotate_daily is False


def test_from_config_tolerates_a_config_without_a_security_block(tmp_path):
    class Cfg:
        pass

    assert AuditLog.from_config(Cfg()).path.name == "audit.jsonl"


def test_set_audit_log_redirects_the_module_level_record(log):
    audit_mod.set_audit_log(log)
    try:
        audit_mod.record(actor="harness", action="via-module")
        assert [r["action"] for r in log.tail(1)] == ["via-module"]
    finally:
        audit_mod.set_audit_log(None)


def test_audit_log_does_not_reuse_the_plain_logging_file(log):
    """The chain lives in its own file — a stray log line would break every HMAC."""
    assert log.path.name != "audit.log"


def test_an_all_commented_out_audit_block_is_not_a_crash():
    """`audit:` with every key commented out parses as None, not as absent."""
    from config import Config

    assert AuditLog.from_config(Config(version=1, owner="test", security={"audit": None})).path is not None

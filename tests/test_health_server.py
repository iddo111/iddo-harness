"""
Tests for the local health / audit / policy endpoint.

Most of these drive a real server over a real loopback socket. The endpoint's
whole job is to be reachable from this machine and from nowhere else, and to
answer honestly about subsystems that may themselves be broken — neither of
which a direct call to the payload builders would prove.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from audit import AuditLog
from health_server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_TAIL,
    VERSION,
    HealthServer,
    _to_prometheus,
    is_localhost,
)


# -- peer classification ----------------------------------------------------
def test_the_canonical_loopback_addresses_are_local():
    assert is_localhost("127.0.0.1")
    assert is_localhost("::1")
    assert is_localhost("::ffff:127.0.0.1")


def test_the_whole_127_block_is_local():
    """A client bound to 127.0.0.2 is equally local; refusing it would confuse."""
    assert is_localhost("127.0.0.2")
    assert is_localhost("127.255.255.254")


def test_routable_addresses_are_not_local():
    assert not is_localhost("10.0.0.5")
    assert not is_localhost("192.168.1.10")
    assert not is_localhost("8.8.8.8")
    assert not is_localhost("")


def test_an_address_merely_containing_127_is_not_local():
    """Prefix matching must not be substring matching."""
    assert not is_localhost("10.0.0.127")
    assert not is_localhost("212.127.0.1")


# -- configuration ----------------------------------------------------------
class Cfg:
    def __init__(self, health=None):
        self.health = health


def test_the_endpoint_is_off_unless_policy_switches_it_on():
    """It exposes policy and audit history, so opt-in is the only safe default."""
    assert HealthServer.enabled_in(Cfg()) is False
    assert HealthServer.enabled_in(Cfg({})) is False
    assert HealthServer.enabled_in(Cfg({"enabled": False})) is False
    assert HealthServer.enabled_in(Cfg({"enabled": True})) is True


def test_enabled_in_tolerates_a_malformed_health_block():
    assert HealthServer.enabled_in(Cfg("not a mapping")) is False
    assert HealthServer.enabled_in(object()) is False


def test_from_config_reads_host_and_port():
    server = HealthServer.from_config(Cfg({"host": "127.0.0.5", "port": 9999}))
    assert (server.host, server.port) == ("127.0.0.5", 9999)


def test_from_config_falls_back_to_the_defaults():
    server = HealthServer.from_config(Cfg({"enabled": True}))
    assert (server.host, server.port) == (DEFAULT_HOST, DEFAULT_PORT)


def test_from_config_survives_a_non_mapping_health_block():
    server = HealthServer.from_config(Cfg(["nonsense"]))
    assert server.port == DEFAULT_PORT


def test_load_config_normalises_an_empty_v3_block(tmp_path):
    """An empty `health:` parses as None; every consumer expects a mapping."""
    from config import load_config

    path = tmp_path / "policy.yaml"
    path.write_text("version: 1\nhealth:\nsandbox:\nsecurity:\napproval:\n", encoding="utf-8")
    cfg = load_config(str(path))
    assert (cfg.health, cfg.sandbox, cfg.security, cfg.approval) == ({}, {}, {}, {})
    assert HealthServer.from_config(cfg).port == DEFAULT_PORT


def test_the_default_port_is_documented():
    assert DEFAULT_PORT == 8478
    assert DEFAULT_HOST == "127.0.0.1"


# -- payload builders -------------------------------------------------------
def test_health_reports_ok_and_an_uptime():
    payload = HealthServer().health_payload()
    assert payload["status"] == "ok"
    assert payload["version"] == VERSION
    assert payload["uptime_sec"] >= 0


def test_a_status_provider_is_merged_into_health():
    server = HealthServer(status_provider=lambda: {"queue_depth": 4})
    assert server.health_payload()["queue_depth"] == 4


def test_a_degraded_subsystem_degrades_the_whole_status():
    server = HealthServer(status_provider=lambda: {"degraded": True, "reason": "no bridge"})
    payload = server.health_payload()
    assert payload["status"] == "degraded"
    assert payload["reason"] == "no bridge"


def test_a_crashing_status_provider_degrades_rather_than_raises():
    """Health that 500s when a subsystem is sick is health that tells you nothing."""

    def boom():
        raise RuntimeError("subsystem on fire")

    payload = HealthServer(status_provider=boom).health_payload()
    assert payload["status"] == "degraded"
    assert "on fire" in payload["error"]


def test_metrics_falls_back_to_a_builtin_minimum():
    payload = HealthServer().metrics_payload()
    assert payload["audit_records_recent"] == 0
    assert payload["uptime_sec"] >= 0


def test_an_injected_metrics_provider_supersedes_the_builtin(tmp_path):
    """Track A owns metrics when it is running; this endpoint only serves them."""
    server = HealthServer(metrics_provider=lambda: {"tasks_total": 7})
    assert server.metrics_payload() == {"tasks_total": 7}


def test_a_crashing_metrics_provider_reports_the_error(tmp_path):
    def boom():
        raise RuntimeError("counter exploded")

    assert "exploded" in HealthServer(metrics_provider=boom).metrics_payload()["error"]


def test_builtin_metrics_count_recent_audit_records(tmp_path):
    audit = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    for i in range(3):
        audit.record("harness", "shell", f"cmd-{i}")
    assert HealthServer(audit_log=audit).metrics_payload()["audit_records_recent"] == 3


def test_builtin_metrics_survive_an_unreadable_audit_log():
    class Broken:
        def tail(self, n):
            raise OSError("disk gone")

    assert HealthServer(audit_log=Broken()).metrics_payload()["audit_records_recent"] == 0


def test_audit_tail_is_empty_without_an_audit_log():
    assert HealthServer().audit_tail(10) == []


def test_audit_tail_returns_the_most_recent_records(tmp_path):
    audit = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    for i in range(5):
        audit.record("harness", "shell", f"cmd-{i}")
    records = HealthServer(audit_log=audit).audit_tail(2)
    assert [r["resource"] for r in records] == ["cmd-3", "cmd-4"]


def test_audit_tail_is_clamped_to_max_tail(tmp_path):
    """Unbounded n turns a health probe into a way to read the whole history."""
    seen = {}

    class Recorder:
        def tail(self, n):
            seen["n"] = n
            return []

    HealthServer(audit_log=Recorder()).audit_tail(10_000_000)
    assert seen["n"] == MAX_TAIL


def test_audit_tail_clamps_nonsense_lower_bounds(tmp_path):
    seen = {}

    class Recorder:
        def tail(self, n):
            seen["n"] = n
            return []

    HealthServer(audit_log=Recorder()).audit_tail(-5)
    assert seen["n"] == 1


def test_policy_text_reads_the_configured_path(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("auto_allow:\n  - ls\n", encoding="utf-8")
    assert "auto_allow" in HealthServer(policy_path=path).policy_text()


def test_policy_text_falls_back_to_the_repo_policy(tmp_path):
    """A missing configured path must not mask the policy actually in force."""
    server = HealthServer(policy_path=tmp_path / "absent.yaml")
    assert "block" in server.policy_text()


# -- Prometheus rendering ---------------------------------------------------
def test_prometheus_renders_flat_numbers():
    assert _to_prometheus({"tasks_total": 3}) == "iddo_harness_tasks_total 3\n"


def test_prometheus_labels_nested_counters():
    text = _to_prometheus({"tasks_by_kind": {"shell": 2, "read_file": 1}})
    assert 'iddo_harness_tasks_by_kind{name="read_file"} 1' in text
    assert 'iddo_harness_tasks_by_kind{name="shell"} 2' in text


def test_prometheus_coerces_booleans_to_numbers():
    assert _to_prometheus({"healthy": True}) == "iddo_harness_healthy 1\n"


def test_prometheus_skips_values_it_cannot_express():
    """A string metric has no Prometheus form; dropping it beats emitting junk."""
    text = _to_prometheus({"version": "3.0.0", "tasks_total": 1})
    assert "version" not in text
    assert "tasks_total" in text


def test_prometheus_output_is_sorted_and_newline_terminated():
    text = _to_prometheus({"b": 1, "a": 2})
    assert text.startswith("iddo_harness_a")
    assert text.endswith("\n")


# -- live server ------------------------------------------------------------
@pytest.fixture
def audit(tmp_path):
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    log.record("harness", "startup", "agent")
    log.record("task-1", "shell", "ls -la", outcome="ok")
    return log


@pytest.fixture
def server(tmp_path, audit):
    policy = tmp_path / "policy.yaml"
    policy.write_text("block:\n  - 'rm -rf /'\n", encoding="utf-8")
    srv = HealthServer(
        port=0,
        audit_log=audit,
        policy_path=policy,
        metrics_provider=lambda: {"tasks_total": 2, "tasks_by_kind": {"shell": 2}},
        status_provider=lambda: {"queue_depth": 0},
    )
    with srv:
        yield srv


def get(server, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}", timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")


def test_start_returns_a_real_bound_port(server):
    assert server.port > 0


def test_health_answers_over_http(server):
    status, ctype, body = get(server, "/health")
    assert status == 200
    assert "application/json" in ctype
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["queue_depth"] == 0


def test_the_root_path_is_health(server):
    assert json.loads(get(server, "/")[2])["version"] == VERSION


def test_a_trailing_slash_hits_the_same_route(server):
    assert json.loads(get(server, "/health/")[2])["status"] == "ok"


def test_metrics_answers_json_by_default(server):
    payload = json.loads(get(server, "/metrics")[2])
    assert payload["tasks_total"] == 2


def test_metrics_answers_prometheus_on_request(server):
    status, ctype, body = get(server, "/metrics?format=prom")
    assert status == 200
    assert "text/plain" in ctype
    assert "iddo_harness_tasks_total 2" in body
    assert 'iddo_harness_tasks_by_kind{name="shell"} 2' in body


def test_audit_tail_answers_over_http(server):
    payload = json.loads(get(server, "/audit/tail?n=10")[2])
    assert payload["count"] == 2
    assert payload["records"][0]["action"] == "startup"


def test_audit_tail_honours_n(server):
    payload = json.loads(get(server, "/audit/tail?n=1")[2])
    assert payload["count"] == 1
    assert payload["records"][0]["action"] == "shell"


def test_audit_tail_defaults_to_a_hundred(server):
    assert json.loads(get(server, "/audit/tail")[2])["count"] == 2


def test_a_non_numeric_n_is_a_400_not_a_crash(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(server, "/audit/tail?n=banana")
    assert excinfo.value.code == 400


def test_policy_serves_the_active_yaml(server):
    status, ctype, body = get(server, "/policy")
    assert status == 200
    assert "yaml" in ctype
    assert "rm -rf /" in body


def test_policy_is_404_when_there_is_nothing_to_serve(tmp_path, monkeypatch):
    monkeypatch.setattr(HealthServer, "policy_text", lambda self: "")
    with HealthServer(port=0) as srv:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            get(srv, "/policy")
        assert excinfo.value.code == 404


def test_an_unknown_route_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(server, "/nope")
    assert excinfo.value.code == 404


def test_a_non_local_peer_is_refused(server, monkeypatch):
    """The bind address is not the only defence — the handler re-checks the peer."""
    monkeypatch.setattr("health_server.is_localhost", lambda addr: False)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(server, "/health")
    assert excinfo.value.code == 403


def test_concurrent_probes_are_all_answered(server):
    """A scrape loop and a human curl must not block each other."""
    results = []

    def probe():
        results.append(get(server, "/health")[0])

    threads = [threading.Thread(target=probe) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert results == [200] * 8


# -- lifecycle --------------------------------------------------------------
def test_stop_releases_the_port():
    srv = HealthServer(port=0)
    srv.start()
    port = srv.port
    srv.stop()
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)


def test_stop_is_safe_before_start():
    HealthServer(port=0).stop()


def test_stop_is_idempotent():
    srv = HealthServer(port=0)
    srv.start()
    srv.stop()
    srv.stop()

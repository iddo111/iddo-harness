"""
Tests for agent/approval.py and agent/health_server.py.

Both are about the human side of the loop: how a confirmation request reaches
someone, and what the machine will tell a local operator about itself. The
timeout tests use tiny timeouts and back-dated pending files rather than sleeping.
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import approval
from approval import ApprovalManager
from audit import AuditLog
from health_server import HealthServer, is_localhost


@pytest.fixture
def cfg():
    from config import Config

    return Config(
        version=1, owner="test",
        auto_allow={}, require_confirm={}, block={},
        polling={"interval_seconds": 5, "max_concurrent_tasks": 1, "task_timeout_seconds": 60},
        confirm={"timeout_minutes": 30}, paths={},
        transport={"type": "github", "repo": "test/bridge"},
        approval={"mode": "local", "timeout_seconds": 300},
    )


@pytest.fixture
def audit_log(tmp_path):
    return AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")


@pytest.fixture
def manager(cfg, tmp_path, audit_log, monkeypatch):
    """A manager whose pending queue and bridge mirror both live in tmp_path."""
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending", audit_log=audit_log)
    monkeypatch.setattr(m, "_bridge_local", None)
    return m


class FakeTask:
    def __init__(self, task_id="t-1", command="pip install requests", kind="shell"):
        self.id = task_id
        self.kind = kind
        self.payload = {"command": command}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_mode_comes_from_the_policy_block(cfg, tmp_path):
    cfg.approval = {"mode": "notification", "timeout_seconds": 120}
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending")
    assert m.mode == "notification"
    assert m.timeout_seconds == 120


def test_default_timeout_is_five_minutes(cfg, tmp_path):
    cfg.approval = {}
    assert ApprovalManager(cfg, local_dir=tmp_path / "p").timeout_seconds == 300


def test_unknown_mode_falls_back_to_local(cfg, tmp_path, caplog):
    import logging

    cfg.approval = {"mode": "telepathy"}
    with caplog.at_level(logging.WARNING):
        m = ApprovalManager(cfg, local_dir=tmp_path / "p")
    assert m.mode == "local"
    assert "unknown approval mode" in caplog.text


def test_config_without_an_approval_block_uses_defaults(cfg, tmp_path):
    del cfg.approval
    m = ApprovalManager(cfg, local_dir=tmp_path / "p")
    assert (m.mode, m.timeout_seconds) == ("local", 300)


def test_timeout_minutes_agrees_with_timeout_seconds(cfg, tmp_path):
    cfg.approval = {"timeout_seconds": 600}
    assert ApprovalManager(cfg, local_dir=tmp_path / "p").timeout_minutes == 10


def test_normalise_mode_accepts_the_three_modes():
    for mode in ("local", "notification", "remote"):
        assert approval.normalise_mode(mode) == mode


def test_describe_reports_the_effective_configuration(manager):
    info = manager.describe()
    assert info["mode"] == "local"
    assert info["timeout_seconds"] == 300
    assert info["pending"] == 0


# ---------------------------------------------------------------------------
# Backward compatibility with ConfirmManager
# ---------------------------------------------------------------------------
def test_create_writes_a_pending_file(manager):
    pc = manager.create(FakeTask(), "requires confirmation: pip install*")
    assert pc.task_id == "t-1"
    assert (manager.local_dir / "pending-confirm-t-1.json").exists()


def test_check_response_is_none_before_an_answer(manager):
    manager.create(FakeTask(), "reason")
    assert manager.check_response("t-1") is None


def test_respond_approve_is_visible_to_check_response(manager):
    manager.create(FakeTask(), "reason")
    manager.respond("t-1", approve=True)
    assert manager.check_response("t-1") == "approved"


def test_respond_deny_is_visible_to_check_response(manager):
    manager.create(FakeTask(), "reason")
    manager.respond("t-1", approve=False)
    assert manager.check_response("t-1") == "denied"


def test_list_pending_hides_answered_requests(manager):
    manager.create(FakeTask("a"), "reason")
    manager.create(FakeTask("b"), "reason")
    manager.respond("a", approve=True)
    assert [pc.task_id for pc in manager.list_pending()] == ["b"]


def test_it_is_a_drop_in_for_the_executor(cfg, tmp_path, monkeypatch):
    """Executor takes it as confirm_manager and parks a CONFIRM task."""
    from executor import Executor
    from policy import PolicyEngine
    from poller import Task

    cfg.require_confirm = {"commands": ["pip install*"]}
    cfg.block = {"commands": []}
    cfg.auto_allow = {}
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending")
    monkeypatch.setattr(m, "_bridge_local", None)

    executor = Executor(PolicyEngine(cfg), m)
    result = executor.run(Task(id="t-9", kind="shell", payload={"command": "pip install requests"}))
    assert result.decision == "confirm_required"
    assert (m.local_dir / "pending-confirm-t-9.json").exists()


# ---------------------------------------------------------------------------
# Auditing
# ---------------------------------------------------------------------------
def test_the_request_is_audited(manager, audit_log):
    manager.create(FakeTask(), "requires confirmation: pip install*")
    actions = [r["action"] for r in audit_log.tail(10)]
    assert "approval_requested" in actions


def test_an_approval_records_who_decided(manager, audit_log):
    manager.create(FakeTask(), "reason")
    manager.respond("t-1", approve=True, actor="iddo")
    granted = [r for r in audit_log.tail(10) if r["action"] == "approval_granted"]
    assert granted and granted[0]["actor"] == "iddo"
    assert granted[0]["outcome"] == "ok"


def test_a_denial_is_audited_as_deny(manager, audit_log):
    manager.create(FakeTask(), "reason")
    manager.respond("t-1", approve=False)
    denied = [r for r in audit_log.tail(10) if r["action"] == "approval_denied"]
    assert denied and denied[0]["outcome"] == "deny"


def test_a_vault_reference_is_masked_in_the_audit_line(manager, audit_log):
    manager.create(FakeTask(command="curl -H 'K: {{secret:api}}'"), "reason")
    text = audit_log.path.read_text(encoding="utf-8")
    assert "{{secret:api}}" not in text
    assert "<vault:api>" in text


def test_auditing_failures_do_not_break_approval(manager):
    class BrokenSink:
        def record(self, **kw):
            raise RuntimeError("disk full")

    manager.audit_log = BrokenSink()
    assert manager.create(FakeTask(), "reason").task_id == "t-1"


def test_a_manager_without_an_audit_log_still_works(cfg, tmp_path, monkeypatch):
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending", audit_log=None)
    monkeypatch.setattr(m, "_bridge_local", None)
    m.create(FakeTask(), "reason")
    assert m.check_response("t-1") is None


# ---------------------------------------------------------------------------
# Timeout → auto-deny
# ---------------------------------------------------------------------------
def _backdate(manager, task_id, age_seconds):
    path = manager.local_dir / f"pending-confirm-{task_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["created_ts"] = time.time() - age_seconds
    path.write_text(json.dumps(data), encoding="utf-8")


def test_sweep_denies_a_request_older_than_the_timeout(manager):
    manager.create(FakeTask(), "reason")
    _backdate(manager, "t-1", 400)
    assert manager.sweep_timeouts() == ["t-1"]
    assert manager.check_response("t-1") == "denied"


def test_sweep_leaves_a_fresh_request_alone(manager):
    manager.create(FakeTask(), "reason")
    assert manager.sweep_timeouts() == []
    assert manager.check_response("t-1") is None


def test_sweep_uses_the_seconds_based_timeout(cfg, tmp_path, monkeypatch):
    """`confirm.timeout_minutes` of 30 must not override `approval.timeout_seconds`."""
    cfg.approval = {"timeout_seconds": 60}
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending")
    monkeypatch.setattr(m, "_bridge_local", None)
    m.create(FakeTask(), "reason")
    _backdate(m, "t-1", 120)
    assert m.sweep_timeouts() == ["t-1"]


def test_the_timeout_is_audited(manager, audit_log):
    manager.create(FakeTask(), "reason")
    _backdate(manager, "t-1", 400)
    manager.sweep_timeouts()
    actions = [r["action"] for r in audit_log.tail(20)]
    assert "approval_timeout" in actions


def test_wait_for_response_returns_an_existing_answer(manager):
    manager.create(FakeTask(), "reason")
    manager.respond("t-1", approve=True)
    assert manager.wait_for_response("t-1", timeout=0.1) == "approved"


def test_wait_for_response_auto_denies_after_the_deadline(manager):
    manager.create(FakeTask(), "reason")
    assert manager.wait_for_response("t-1", timeout=0, poll_interval=0.01) == "denied"
    assert manager.check_response("t-1") == "denied"


def test_sweep_skips_an_unparseable_pending_file(manager):
    manager.create(FakeTask(), "reason")
    (manager.local_dir / "pending-confirm-broken.json").write_text("{oops", encoding="utf-8")
    assert manager.sweep_timeouts() == []


# ---------------------------------------------------------------------------
# Notification and remote delivery
# ---------------------------------------------------------------------------
def test_notification_mode_calls_the_desktop_backend(cfg, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(approval, "notify_desktop", lambda title, body: calls.append((title, body)) or True)
    cfg.approval = {"mode": "notification"}
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending")
    monkeypatch.setattr(m, "_bridge_local", None)
    m.create(FakeTask(), "reason")
    assert len(calls) == 1
    assert "t-1" in calls[0][1]


def test_local_mode_does_not_notify(cfg, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(approval, "notify_desktop", lambda *a: calls.append(a) or True)
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending")
    monkeypatch.setattr(m, "_bridge_local", None)
    m.create(FakeTask(), "reason")
    assert calls == []


def test_the_notification_body_never_contains_a_secret(cfg, tmp_path, monkeypatch):
    bodies = []
    monkeypatch.setattr(approval, "notify_desktop", lambda title, body: bodies.append(body) or True)
    cfg.approval = {"mode": "notification"}
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending")
    monkeypatch.setattr(m, "_bridge_local", None)
    m.create(FakeTask(command="curl -H 'K: {{secret:api}}'"), "reason")
    assert "{{secret:api}}" not in bodies[0]


def test_a_failing_notifier_does_not_break_the_request(cfg, tmp_path, monkeypatch):
    def boom(*a):
        raise RuntimeError("no dbus")

    monkeypatch.setattr(approval, "notify_desktop", boom)
    cfg.approval = {"mode": "notification"}
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending")
    monkeypatch.setattr(m, "_bridge_local", None)
    with pytest.raises(RuntimeError):
        m.create(FakeTask(), "reason")
    # The pending file was written before delivery was attempted, which is the
    # ordering that matters: the request survives a broken notifier.
    assert (m.local_dir / "pending-confirm-t-1.json").exists()


def test_notify_desktop_returns_false_without_a_backend(monkeypatch):
    monkeypatch.setattr(approval.platform, "system", lambda: "Linux")
    monkeypatch.setattr(approval.shutil, "which", lambda name: None)
    assert approval.notify_desktop("title", "body") is False


def test_notify_desktop_invokes_notify_send_on_linux(monkeypatch):
    monkeypatch.setattr(approval.platform, "system", lambda: "Linux")
    monkeypatch.setattr(approval.shutil, "which", lambda name: "/usr/bin/notify-send")
    seen = {}
    monkeypatch.setattr(
        approval.subprocess, "run",
        lambda args, **kw: seen.setdefault("args", args),
    )
    assert approval.notify_desktop("Title", "Body") is True
    assert seen["args"][0] == "notify-send"
    assert seen["args"][-2:] == ["Title", "Body"]


def test_notify_desktop_returns_false_on_an_unknown_platform(monkeypatch):
    monkeypatch.setattr(approval.platform, "system", lambda: "Plan9")
    assert approval.notify_desktop("t", "b") is False


def test_remote_mode_sends_over_the_ws_bridge(cfg, tmp_path, monkeypatch):
    sent = []

    class FakeBridge:
        def broadcast(self, message):
            sent.append(message)

    monkeypatch.setattr(approval, "notify_desktop", lambda *a: False)
    cfg.approval = {"mode": "remote"}
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending", ws_bridge=FakeBridge())
    monkeypatch.setattr(m, "_bridge_local", None)
    m.create(FakeTask(), "reason")

    assert sent and sent[0]["type"] == "approval_request"
    assert sent[0]["task_id"] == "t-1"


def test_remote_mode_still_writes_the_pending_file(cfg, tmp_path, monkeypatch):
    """Delivery is additive: a websocket nobody answers must not lose the request."""
    monkeypatch.setattr(approval, "notify_desktop", lambda *a: False)
    cfg.approval = {"mode": "remote"}
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending", ws_bridge=None)
    monkeypatch.setattr(m, "_bridge_local", None)
    monkeypatch.setattr(ApprovalManager, "_resolve_bridge", staticmethod(lambda: None))
    m.create(FakeTask(), "reason")
    assert (m.local_dir / "pending-confirm-t-1.json").exists()


def test_a_broken_bridge_is_tolerated(cfg, tmp_path, monkeypatch):
    class BrokenBridge:
        def broadcast(self, message):
            raise ConnectionRefusedError("bridge is down")

    monkeypatch.setattr(approval, "notify_desktop", lambda *a: False)
    cfg.approval = {"mode": "remote"}
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending", ws_bridge=BrokenBridge())
    monkeypatch.setattr(m, "_bridge_local", None)
    assert m.create(FakeTask(), "reason").task_id == "t-1"


def test_remote_send_reports_false_without_a_bridge(cfg, tmp_path, monkeypatch):
    m = ApprovalManager(cfg, local_dir=tmp_path / "pending")
    monkeypatch.setattr(ApprovalManager, "_resolve_bridge", staticmethod(lambda: None))
    pc = type("PC", (), {"task_id": "t", "kind": "shell", "command": "ls", "reason": "r"})()
    assert m._send_remote(pc) is False


# ---------------------------------------------------------------------------
# Health / audit / policy endpoint
# ---------------------------------------------------------------------------
@pytest.fixture
def server(cfg, tmp_path, audit_log):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\nowner: test\n", encoding="utf-8")
    srv = HealthServer(
        cfg=cfg, host="127.0.0.1", port=0,
        audit_log=audit_log, policy_path=policy_path,
    )
    srv.start()
    yield srv
    srv.stop()


def _get(server, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}", timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_health_reports_ok(server):
    status, body = _get(server, "/health")
    payload = json.loads(body)
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["uptime_sec"] >= 0
    assert payload["version"]


def test_root_is_the_health_payload(server):
    assert json.loads(_get(server, "/")[1])["status"] == "ok"


def test_health_reports_degraded_from_the_status_provider(cfg, tmp_path):
    srv = HealthServer(cfg=cfg, port=0, status_provider=lambda: {"degraded": True, "queue_depth": 9})
    srv.start()
    try:
        payload = json.loads(_get(srv, "/health")[1])
        assert payload["status"] == "degraded"
        assert payload["queue_depth"] == 9
    finally:
        srv.stop()


def test_a_throwing_status_provider_reports_degraded(cfg):
    def boom():
        raise RuntimeError("subsystem down")

    srv = HealthServer(cfg=cfg, port=0, status_provider=boom)
    srv.start()
    try:
        assert json.loads(_get(srv, "/health")[1])["status"] == "degraded"
    finally:
        srv.stop()


def test_metrics_uses_the_injected_provider(cfg):
    srv = HealthServer(cfg=cfg, port=0, metrics_provider=lambda: {"tasks_total": 7})
    srv.start()
    try:
        assert json.loads(_get(srv, "/metrics")[1])["tasks_total"] == 7
    finally:
        srv.stop()


def test_metrics_can_be_rendered_for_prometheus(cfg):
    srv = HealthServer(cfg=cfg, port=0, metrics_provider=lambda: {"tasks_total": 7})
    srv.start()
    try:
        _status, body = _get(srv, "/metrics?format=prom")
        assert "iddo_harness_tasks_total 7" in body
    finally:
        srv.stop()


def test_audit_tail_returns_records(server, audit_log):
    audit_log.record(actor="harness", action="agent_startup")
    payload = json.loads(_get(server, "/audit/tail?n=5")[1])
    assert payload["count"] == 1
    assert payload["records"][0]["action"] == "agent_startup"


def test_audit_tail_rejects_a_non_numeric_n(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/audit/tail?n=lots")
    assert exc.value.code == 400


def test_policy_endpoint_serves_the_active_yaml(server):
    status, body = _get(server, "/policy")
    assert status == 200
    assert "owner: test" in body


def test_unknown_route_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/nope")
    assert exc.value.code == 404


def test_non_localhost_peer_is_forbidden(server, monkeypatch):
    """`/audit/tail` and `/policy` are exactly what an attacker wants next."""
    import health_server

    monkeypatch.setattr(health_server, "is_localhost", lambda addr: False)
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/audit/tail")
    assert exc.value.code == 403
    assert "localhost only" in exc.value.read().decode("utf-8")


def test_is_localhost_accepts_the_loopback_block():
    assert is_localhost("127.0.0.1")
    assert is_localhost("127.0.0.2")
    assert is_localhost("::1")


def test_is_localhost_rejects_a_lan_address():
    assert not is_localhost("192.168.1.10")
    assert not is_localhost("10.0.0.5")


def test_endpoint_is_off_by_default(cfg):
    cfg.health = {}
    assert HealthServer.enabled_in(cfg) is False


def test_endpoint_switches_on_from_policy(cfg):
    cfg.health = {"enabled": True, "port": 9999}
    assert HealthServer.enabled_in(cfg) is True
    assert HealthServer.from_config(cfg).port == 9999

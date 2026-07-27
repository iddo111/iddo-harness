"""
Tests for the handshake (agent/handshake.py).

The security-relevant assertion in this file is
``test_policy_summary_never_leaks_the_patterns``: the rule list is a map of
what the owner is protecting, and it must not travel to whoever holds the
bridge token.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from handshake import (
    AMP_VERSION,
    BRICK_IDENTITY,
    HARNESS_NAME,
    HARNESS_VERSION,
    SUPPORTED_PROTOCOLS,
    V1_KINDS,
    Capability,
    HandshakeReport,
    _module_available,
    _policy_summary,
    _protocol_key,
    build_report,
    detect_capabilities,
    negotiate,
)
from executor_v2 import V2_KINDS
from executor_v3 import V3_KINDS


def fake_policy(**overrides: Any) -> SimpleNamespace:
    cfg = SimpleNamespace(
        auto_allow={"commands": ["echo*", "ls*"], "paths": {"read": ["/tmp/**", "/srv/**"]}},
        require_confirm={"commands": ["rm*"], "paths": {"write": ["/etc/**"]}},
        block={"commands": ["*secret*"], "paths": {"absolute_no_touch": ["**/.env", "**/id_rsa"]}},
        confirm={"timeout_minutes": 30},
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return SimpleNamespace(cfg=cfg)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def test_report_identifies_the_harness() -> None:
    body = build_report(v3_kinds=V3_KINDS).to_dict()
    assert body["harness"] == HARNESS_NAME
    assert body["version"] == HARNESS_VERSION
    assert body["identity"] == BRICK_IDENTITY
    assert body["amp_version"] == AMP_VERSION
    assert body["protocols"] == list(SUPPORTED_PROTOCOLS)


def test_report_groups_kinds_by_generation() -> None:
    body = build_report(v3_kinds=V3_KINDS).to_dict()
    assert set(body["kinds"]["v1"]) == set(V1_KINDS)
    assert set(body["kinds"]["v2"]) == set(V2_KINDS)
    assert set(body["kinds"]["v3"]) == set(V3_KINDS)


def test_v2_kinds_are_discovered_when_not_supplied() -> None:
    """A handshake that disagreed with the router would be worse than none."""
    assert set(build_report().kinds["v2"]) == set(V2_KINDS)


def test_all_kinds_is_the_sorted_union() -> None:
    report = build_report(v3_kinds=V3_KINDS)
    assert report.all_kinds == sorted(set(V1_KINDS) | set(V2_KINDS) | set(V3_KINDS))


def test_report_describes_the_host() -> None:
    host = build_report().to_dict()["host"]
    assert set(host) == {"platform", "release", "machine", "python", "implementation"}
    assert host["python"]


def test_limits_are_passed_through() -> None:
    assert build_report(limits={"max_nodes": 100}).to_dict()["limits"] == {"max_nodes": 100}


def test_uptime_is_reported() -> None:
    assert build_report().to_dict()["uptime_sec"] >= 0


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
def test_capabilities_cover_the_optional_dependencies() -> None:
    names = {c.name for c in detect_capabilities()}
    assert {"watchdog", "psutil", "croniter", "websockets", "PyYAML", "httpx", "sqlite3"} <= names


def test_sqlite3_is_always_available() -> None:
    caps = {c.name: c for c in detect_capabilities()}
    assert caps["sqlite3"].available is True


def test_every_capability_explains_the_fallback() -> None:
    assert all(c.detail for c in detect_capabilities())


def test_module_availability_probe() -> None:
    assert _module_available("json") is True
    assert _module_available("definitely_not_a_module_xyz") is False


def test_capability_renders() -> None:
    assert Capability(name="x", available=True, detail="d").to_dict() == {
        "name": "x", "available": True, "detail": "d"
    }


# ---------------------------------------------------------------------------
# Policy summary
# ---------------------------------------------------------------------------
def test_policy_summary_counts_the_rules() -> None:
    summary = _policy_summary(fake_policy())
    assert summary["available"] is True
    assert summary["default_decision"] == "confirm"
    assert summary["auto_allow_commands"] == 2
    assert summary["auto_allow_read_paths"] == 2
    assert summary["require_confirm_commands"] == 1
    assert summary["require_confirm_write_paths"] == 1
    assert summary["block_commands"] == 1
    assert summary["block_paths"] == 2
    assert summary["confirm_timeout_minutes"] == 30


def test_policy_summary_never_leaks_the_patterns() -> None:
    """Publishing the rule list would tell a token holder what to go after."""
    blob = repr(_policy_summary(fake_policy()))
    for pattern in ("echo*", "/tmp/**", "*secret*", ".env", "id_rsa", "/etc/**"):
        assert pattern not in blob


def test_policy_summary_handles_a_missing_policy() -> None:
    assert _policy_summary(None) == {"available": False}
    assert _policy_summary(SimpleNamespace()) == {"available": False}


def test_policy_summary_tolerates_missing_sections() -> None:
    policy = SimpleNamespace(cfg=SimpleNamespace())
    summary = _policy_summary(policy)
    assert summary["available"] is True
    assert summary["block_commands"] == 0


def test_report_embeds_the_policy_summary() -> None:
    assert build_report(policy=fake_policy()).to_dict()["policy"]["available"] is True


# ---------------------------------------------------------------------------
# Negotiation
# ---------------------------------------------------------------------------
def test_an_empty_request_is_compatible() -> None:
    body = negotiate(build_report(v3_kinds=V3_KINDS), {})
    assert body["negotiation"]["compatible"] is True
    assert body["negotiation"]["negotiated_protocol"] == "3.0"


def test_none_request_is_treated_as_empty() -> None:
    assert negotiate(build_report(), None)["negotiation"]["compatible"] is True


def test_supported_kinds_negotiate_cleanly() -> None:
    body = negotiate(
        build_report(v3_kinds=V3_KINDS), {"required_kinds": ["shell", "grep", "workflow"]}
    )
    assert body["negotiation"]["unsupported_kinds"] == []
    assert body["negotiation"]["compatible"] is True


def test_unsupported_kinds_are_named_not_just_counted() -> None:
    """One round trip should tell a producer exactly what is missing."""
    body = negotiate(build_report(v3_kinds=V3_KINDS), {"required_kinds": ["shell", "teleport"]})
    assert body["negotiation"]["unsupported_kinds"] == ["teleport"]
    assert body["negotiation"]["compatible"] is False


def test_the_highest_shared_protocol_wins() -> None:
    body = negotiate(build_report(), {"protocols": ["1.0", "2.0"]})
    assert body["negotiation"]["negotiated_protocol"] == "2.0"


def test_a_singular_protocol_field_is_accepted() -> None:
    body = negotiate(build_report(), {"protocol": "1.0"})
    assert body["negotiation"]["negotiated_protocol"] == "1.0"


def test_no_shared_protocol_is_incompatible() -> None:
    body = negotiate(build_report(), {"protocols": ["9.0"]})
    assert body["negotiation"]["negotiated_protocol"] is None
    assert body["negotiation"]["compatible"] is False


def test_a_missing_capability_makes_the_plan_incompatible() -> None:
    body = negotiate(build_report(), {"required_capabilities": ["sqlite3", "warp-drive"]})
    assert body["negotiation"]["missing_capabilities"] == ["warp-drive"]
    assert body["negotiation"]["compatible"] is False


def test_a_present_capability_is_not_reported_missing() -> None:
    body = negotiate(build_report(), {"required_capabilities": ["sqlite3"]})
    assert body["negotiation"]["missing_capabilities"] == []


def test_the_client_name_is_echoed_back() -> None:
    body = negotiate(build_report(), {"client": "orchestrator-1"})
    assert body["negotiation"]["client"] == "orchestrator-1"


def test_negotiation_still_carries_the_whole_report() -> None:
    body = negotiate(build_report(v3_kinds=V3_KINDS), {"required_kinds": ["nope"]})
    assert body["harness"] == HARNESS_NAME
    assert "capabilities" in body


# ---------------------------------------------------------------------------
# Protocol ordering
# ---------------------------------------------------------------------------
def test_protocols_sort_numerically_not_lexically() -> None:
    assert _protocol_key("10.0") > _protocol_key("9.0")
    assert _protocol_key("3.0") > _protocol_key("2.0")


def test_a_junk_protocol_component_does_not_raise() -> None:
    assert _protocol_key("x.y") == (0, 0)


def test_report_defaults_are_usable() -> None:
    assert HandshakeReport().all_kinds == []

"""
Handshake — ``kind: handshake``, the packet that answers "what can you do?".

Until now a producer had to *assume* what the harness on the far end supports.
That assumption is wrong more often than it looks: v1, v2 and v3 harnesses all
answer the same bridge, ``watchdog``/``psutil``/``croniter`` may or may not be
installed, and policy differs per machine. A producer that guesses wrong finds
out by getting ``unknown kind`` back one poll interval later, or worse, by
having a workflow die halfway through.

So the handshake is the first packet a producer should send: it reports
protocol versions, every kind this build actually handles, which optional
features are live, and a *summary* of policy — counts and defaults, never the
patterns themselves, because the rule list is a map of what the owner is
protecting.

It also negotiates. Send ``required_kinds`` and the response tells you whether
the plan is runnable here before you send step one.
"""
from __future__ import annotations

import logging
import platform
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger("harness.handshake")

HARNESS_NAME = "iddo-harness"
HARNESS_VERSION = "3.0.0"
BRICK_IDENTITY = "brick:iddo-harness"

#: Protocol generations this build understands. A producer picks the highest
#: it also knows; ``negotiate`` does that for it.
SUPPORTED_PROTOCOLS: tuple[str, ...] = ("1.0", "2.0", "3.0")
AMP_VERSION = "1.0"

#: The four original kinds. Listed explicitly rather than imported because
#: ``agent/executor.py`` dispatches them from an if-chain, and a handshake that
#: silently disagreed with the router would be worse than no handshake.
V1_KINDS: frozenset[str] = frozenset({"shell", "read_file", "write_file", "list_dir"})

_START_TIME = time.time()


@dataclass
class Capability:
    """One optional feature and whether it is actually usable here."""

    name: str
    available: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "available": self.available, "detail": self.detail}


@dataclass
class HandshakeReport:
    """Everything a producer needs to plan against this harness."""

    harness: str = HARNESS_NAME
    version: str = HARNESS_VERSION
    identity: str = BRICK_IDENTITY
    protocols: list[str] = field(default_factory=lambda: list(SUPPORTED_PROTOCOLS))
    amp_version: str = AMP_VERSION
    kinds: dict[str, list[str]] = field(default_factory=dict)
    capabilities: list[Capability] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)
    host: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    uptime_sec: float = 0.0

    @property
    def all_kinds(self) -> list[str]:
        """Every kind this harness handles, across all generations."""
        return sorted({k for group in self.kinds.values() for k in group})

    def to_dict(self) -> dict[str, Any]:
        """Render for a task result body."""
        return {
            "harness": self.harness,
            "version": self.version,
            "identity": self.identity,
            "protocols": self.protocols,
            "amp_version": self.amp_version,
            "kinds": self.kinds,
            "all_kinds": self.all_kinds,
            "capabilities": [c.to_dict() for c in self.capabilities],
            "limits": self.limits,
            "host": self.host,
            "policy": self.policy,
            "uptime_sec": self.uptime_sec,
        }


def _module_available(name: str) -> bool:
    """Whether an optional dependency can be imported right now."""
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def detect_capabilities() -> list[Capability]:
    """Probe the optional features whose absence changes behaviour."""
    probes = (
        ("watchdog", "watchdog", "native filesystem events; falls back to mtime polling"),
        ("psutil", "psutil", "rich process listing; falls back to ps/tasklist"),
        ("croniter", "croniter", "cron parsing; falls back to the built-in parser"),
        ("websockets", "websockets", "low-latency transport; git bridge always works"),
        ("yaml", "PyYAML", "policy and config parsing"),
        ("httpx", "httpx", "HTTP LLM backends; mock providers need nothing"),
    )
    found: list[Capability] = []
    for module, label, detail in probes:
        available = _module_available(module)
        found.append(Capability(name=label, available=available, detail=detail))
    found.append(
        Capability(
            name="sqlite3",
            available=_module_available("sqlite3"),
            detail="memory store; required for memory_* kinds",
        )
    )
    return found


def _policy_summary(policy: Any) -> dict[str, Any]:
    """Counts and defaults from the policy engine — never the patterns.

    A producer needs to know *that* confirmation is the default and roughly how
    tight the rules are. Which paths the owner blocks is their business, and
    publishing it to whoever holds the bridge token is a needless disclosure.
    """
    cfg = getattr(policy, "cfg", None)
    if cfg is None:
        return {"available": False}

    def count(section: str, key: str) -> int:
        value = getattr(cfg, section, {}) or {}
        entry = value.get(key, [])
        if isinstance(entry, dict):
            return sum(len(v or []) for v in entry.values())
        return len(entry or [])

    return {
        "available": True,
        "default_decision": "confirm",
        "auto_allow_commands": count("auto_allow", "commands"),
        "auto_allow_read_paths": count("auto_allow", "paths"),
        "require_confirm_commands": count("require_confirm", "commands"),
        "require_confirm_write_paths": count("require_confirm", "paths"),
        "block_commands": count("block", "commands"),
        "block_paths": count("block", "paths"),
        "confirm_timeout_minutes": (getattr(cfg, "confirm", {}) or {}).get("timeout_minutes"),
    }


def build_report(
    *,
    policy: Any = None,
    v2_kinds: Iterable[str] | None = None,
    v3_kinds: Iterable[str] | None = None,
    limits: dict[str, Any] | None = None,
) -> HandshakeReport:
    """Assemble the capability report for this process."""
    if v2_kinds is None:
        try:
            from executor_v2 import V2_KINDS
        except ImportError:  # pragma: no cover - packaged import
            from agent.executor_v2 import V2_KINDS  # type: ignore[no-redef]
        v2_kinds = V2_KINDS

    kinds = {
        "v1": sorted(V1_KINDS),
        "v2": sorted(v2_kinds),
        "v3": sorted(v3_kinds or ()),
    }

    return HandshakeReport(
        kinds=kinds,
        capabilities=detect_capabilities(),
        limits=dict(limits or {}),
        host={
            "platform": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "implementation": sys.implementation.name,
        },
        policy=_policy_summary(policy),
        uptime_sec=round(time.time() - _START_TIME, 3),
    )


def negotiate(report: HandshakeReport, request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Answer a producer's compatibility question against ``report``.

    ``required_kinds`` is the useful field: the response says whether every one
    of them is handled here, and names the ones that are not. A producer that
    checks this before submitting a workflow finds out in one round trip
    instead of at node seven.
    """
    request = request or {}
    body = report.to_dict()

    required = [str(k) for k in (request.get("required_kinds") or [])]
    available = set(report.all_kinds)
    missing = [k for k in required if k not in available]

    raw_protocols = request.get("protocols")
    if raw_protocols is None and request.get("protocol"):
        raw_protocols = [request["protocol"]]
    requested_protocols = [str(p) for p in (raw_protocols or [])]
    if requested_protocols:
        shared = [p for p in report.protocols if p in set(requested_protocols)]
    else:
        shared = list(report.protocols)
    negotiated = max(shared, key=_protocol_key) if shared else None

    required_caps = [str(c) for c in (request.get("required_capabilities") or [])]
    live_caps = {c.name for c in report.capabilities if c.available}
    missing_caps = [c for c in required_caps if c not in live_caps]

    body["negotiation"] = {
        "requested_kinds": required,
        "unsupported_kinds": missing,
        "requested_capabilities": required_caps,
        "missing_capabilities": missing_caps,
        "requested_protocols": requested_protocols,
        "negotiated_protocol": negotiated,
        "compatible": not missing and not missing_caps and negotiated is not None,
    }
    if request.get("client"):
        body["negotiation"]["client"] = str(request["client"])
    return body


def _protocol_key(version: str) -> tuple[int, ...]:
    """Sort ``"3.0"`` above ``"2.0"`` numerically, not lexically."""
    parts = []
    for chunk in str(version).split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)

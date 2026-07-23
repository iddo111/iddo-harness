"""
llm_router.py — Routes harness LLM requests to a healthy backend by role.

Reads the `llm_backends:` section of policy.yaml, e.g.:

    llm_backends:
      coder:
        primary:
          base_url: http://eran1.local:8000/v1
          model: glm-4.6
        secondary:
          base_url: http://eran3.local:8000/v1
          model: glm-4.6
        health_check_interval: 60
      reasoner:
        primary:
          base_url: http://eran3.local:8000/v1
          model: deepseek-v3.1

Each role maps to a `primary` backend and an optional `secondary` (fallback).
A background thread periodically pings GET /v1/models on every configured
backend; `route(role)` consults the last-known health status and returns
the primary if healthy, else the secondary, else raises.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from llm_client import OpenAICompatibleClient

log = logging.getLogger("harness.llm_router")

DEFAULT_HEALTH_CHECK_INTERVAL = 60  # seconds


class NoHealthyBackendError(Exception):
    """Raised by route() when neither primary nor secondary is healthy."""


@dataclass
class BackendSpec:
    name: str  # "primary" | "secondary" | any label
    base_url: str
    model: str
    api_key: str = ""


@dataclass
class RoleConfig:
    role: str
    primary: BackendSpec
    secondary: BackendSpec | None = None
    health_check_interval: int = DEFAULT_HEALTH_CHECK_INTERVAL


@dataclass
class _HealthState:
    healthy: bool = True  # optimistic default until first check completes
    last_checked: float = 0.0
    last_error: str = ""


def _backend_spec_from_dict(name: str, d: dict) -> BackendSpec:
    return BackendSpec(
        name=name,
        base_url=d["base_url"],
        model=d["model"],
        api_key=d.get("api_key", ""),
    )


# Mirrors agent/config.py's DEFAULT_CONFIG_PATHS so this module can read
# llm_backends: directly out of policy.yaml without agent/config.py needing
# to know llm_backends exists (config.py is explicitly off-limits here).
_DEFAULT_POLICY_PATHS = [
    Path.home() / ".iddo-harness" / "policy.yaml",
    Path(__file__).parent.parent / "policy.yaml",
]


def load_llm_backends_from_yaml(path: str | None = None) -> dict:
    """Reads just the `llm_backends:` section straight out of policy.yaml,
    independent of agent/config.py's Config dataclass. Returns {} if no
    policy.yaml is found or it has no llm_backends section."""
    import yaml  # local import: keep this module usable without PyYAML if unneeded

    candidates = [Path(path)] if path else _DEFAULT_POLICY_PATHS
    for p in candidates:
        if p.exists():
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data.get("llm_backends", {})
    return {}


def parse_llm_backends(llm_backends_cfg: dict) -> dict[str, RoleConfig]:
    """Parses the raw `llm_backends:` dict (already yaml.safe_load'd) into
    role -> RoleConfig. Kept as a standalone function so it's trivially
    testable without touching disk/config.py."""
    roles: dict[str, RoleConfig] = {}
    for role, spec in (llm_backends_cfg or {}).items():
        if "primary" not in spec:
            raise ValueError(f"llm_backends.{role} is missing a 'primary' backend")
        primary = _backend_spec_from_dict("primary", spec["primary"])
        secondary = None
        if spec.get("secondary"):
            secondary = _backend_spec_from_dict("secondary", spec["secondary"])
        roles[role] = RoleConfig(
            role=role,
            primary=primary,
            secondary=secondary,
            health_check_interval=spec.get("health_check_interval", DEFAULT_HEALTH_CHECK_INTERVAL),
        )
    return roles


class LlmRouter:
    """
    Owns one OpenAICompatibleClient per (role, backend-slot) and a
    background health-check thread.

    Usage:
        router = LlmRouter(cfg)          # cfg has .llm_backends dict, or pass raw dict
        router.start()
        client = router.route("coder")   # -> OpenAICompatibleClient
        ...
        router.stop()
    """

    def __init__(self, llm_backends_cfg: dict, auto_start: bool = False):
        self.roles: dict[str, RoleConfig] = parse_llm_backends(llm_backends_cfg)
        self._clients: dict[tuple[str, str], OpenAICompatibleClient] = {}
        self._health: dict[tuple[str, str], _HealthState] = {}
        for role, rc in self.roles.items():
            self._clients[(role, "primary")] = OpenAICompatibleClient(
                base_url=rc.primary.base_url, model=rc.primary.model, api_key=rc.primary.api_key
            )
            self._health[(role, "primary")] = _HealthState()
            if rc.secondary:
                self._clients[(role, "secondary")] = OpenAICompatibleClient(
                    base_url=rc.secondary.base_url, model=rc.secondary.model, api_key=rc.secondary.api_key
                )
                self._health[(role, "secondary")] = _HealthState()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        if auto_start:
            self.start()

    # -----------------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg, auto_start: bool = False) -> "LlmRouter":
        """
        Convenience constructor that tolerates three shapes of `cfg`:

          1. An agent.config.Config instance that already exposes an
             `llm_backends` attribute (future-proofing, in case
             agent/config.py's owner adds one).
          2. A plain dict with an `llm_backends` key.
          3. An agent.config.Config instance WITHOUT an `llm_backends`
             attribute (the current reality, since we don't touch
             config.py) — in which case we re-read `llm_backends:` directly
             from whatever policy.yaml path the Config was loaded from, or
             the default location, ourselves.
        """
        llm_backends_cfg = getattr(cfg, "llm_backends", None)
        if llm_backends_cfg is None and isinstance(cfg, dict):
            llm_backends_cfg = cfg.get("llm_backends")
        if llm_backends_cfg is None:
            llm_backends_cfg = load_llm_backends_from_yaml()
        return cls(llm_backends_cfg or {}, auto_start=auto_start)

    # -----------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._health_loop, daemon=True, name="llm-router-health")
        self._thread.start()
        log.info("LlmRouter health-check thread started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("LlmRouter health-check thread stopped")

    # -----------------------------------------------------------------------
    def _health_loop(self):
        while not self._stop_event.is_set():
            self.check_all_once()
            # Sleep in small increments so stop() is responsive.
            min_interval = min((rc.health_check_interval for rc in self.roles.values()), default=DEFAULT_HEALTH_CHECK_INTERVAL)
            waited = 0.0
            while waited < min_interval and not self._stop_event.is_set():
                time.sleep(min(1.0, min_interval - waited))
                waited += 1.0

    def check_all_once(self):
        """Runs one health-check pass over every configured backend slot."""
        for key, client in self._clients.items():
            role, slot = key
            ok = client.health_check()
            state = self._health[key]
            state.healthy = ok
            state.last_checked = time.time()
            state.last_error = "" if ok else "GET /v1/models failed or timed out"
            log.debug(f"health-check role={role} slot={slot} healthy={ok}")

    # -----------------------------------------------------------------------
    def route(self, role: str) -> OpenAICompatibleClient:
        """
        Returns a healthy client for `role`. Prefers primary; falls back to
        secondary if primary is marked unhealthy. Raises NoHealthyBackendError
        if neither is healthy (or role is unknown).
        """
        if role not in self.roles:
            raise KeyError(f"No llm_backends entry for role '{role}'. Configured roles: {list(self.roles)}")

        primary_key = (role, "primary")
        primary_state = self._health.get(primary_key)
        if primary_state is None or primary_state.healthy:
            return self._clients[primary_key]

        rc = self.roles[role]
        if rc.secondary:
            secondary_key = (role, "secondary")
            secondary_state = self._health.get(secondary_key)
            if secondary_state is None or secondary_state.healthy:
                log.warning(f"role={role}: primary unhealthy, falling back to secondary")
                return self._clients[secondary_key]

        raise NoHealthyBackendError(
            f"No healthy backend for role '{role}' (primary_error={primary_state.last_error if primary_state else 'unknown'})"
        )

    # -----------------------------------------------------------------------
    def status(self) -> list[dict]:
        """Returns a list of {role, slot, base_url, model, healthy, last_checked} —
        used by `iddo-harness models`."""
        rows = []
        for (role, slot), client in self._clients.items():
            state = self._health[(role, slot)]
            rows.append({
                "role": role,
                "slot": slot,
                "base_url": client.base_url,
                "model": client.model,
                "healthy": state.healthy,
                "last_checked": state.last_checked,
                "last_error": state.last_error,
            })
        return sorted(rows, key=lambda r: (r["role"], r["slot"]))

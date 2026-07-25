"""
Configuration loader.

Two independent files, two loaders:

* ``policy.yaml`` -> :func:`load_config` -> :class:`Config`
  Security policy plus the legacy v1/v2 runtime bits. Unchanged.
* ``config.yaml`` -> :func:`load_runtime_config` -> :class:`RuntimeConfig`
  v3 runtime knobs: pacing, concurrency, WebSocket bridge, retries, metrics.

They are kept apart on purpose: policy is reviewed and rarely edited, runtime
is tuned freely. ``RuntimeConfig`` has defaults for every field, so a harness
with no ``config.yaml`` at all behaves exactly like v2.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


DEFAULT_CONFIG_PATHS = [
    Path.home() / ".iddo-harness" / "policy.yaml",
    Path(__file__).parent.parent / "policy.yaml",
]

DEFAULT_RUNTIME_CONFIG_PATHS = [
    Path.home() / ".iddo-harness" / "config.yaml",
    Path(__file__).parent.parent / "config.yaml",
]

WS_TOKEN_PATH = Path.home() / ".iddo-harness" / "ws-token"


@dataclass
class Config:
    version: int
    owner: str
    auto_allow: dict = field(default_factory=dict)
    require_confirm: dict = field(default_factory=dict)
    block: dict = field(default_factory=dict)
    polling: dict = field(default_factory=dict)
    paths: dict = field(default_factory=dict)
    transport: dict = field(default_factory=dict)
    confirm: dict = field(default_factory=dict)


def _default_owner() -> str:
    """Best-effort current-user lookup that never raises.

    os.getlogin() requires a controlling tty and raises OSError in many
    sandboxed/CI/service environments, so fall back through env vars.
    """
    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def load_config(path: str | None = None) -> Config:
    if path:
        candidate_paths = [Path(path)]
    else:
        candidate_paths = DEFAULT_CONFIG_PATHS

    for p in candidate_paths:
        if p.exists():
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return Config(
                version=data.get("version", 1),
                owner=data.get("owner", _default_owner()),
                auto_allow=data.get("auto_allow", {}),
                require_confirm=data.get("require_confirm", {}),
                block=data.get("block", {}),
                polling=data.get("polling", {"interval_seconds": 5, "max_concurrent_tasks": 3, "task_timeout_seconds": 600}),
                paths=data.get("paths", {}),
                transport=data.get("transport", {}),
                confirm=data.get("confirm", {"timeout_minutes": 30}),
            )

    raise FileNotFoundError(f"No policy.yaml found in {candidate_paths}")


# --------------------------------------------------------------------------- #
# v3 runtime configuration
# --------------------------------------------------------------------------- #


class ConfigError(ValueError):
    """Raised when config.yaml (or an ENV override) holds an unusable value."""


@dataclass
class WsConfig:
    """WebSocket bridge settings. Disabled unless explicitly turned on."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8477
    token: str | None = None
    token_path: Path = field(default_factory=lambda: WS_TOKEN_PATH)


@dataclass
class RetryConfig:
    """Fallback retry policy for tasks that do not carry their own."""

    default_max_attempts: int = 1
    default_backoff_seconds: list[float] = field(default_factory=lambda: [1.0, 5.0, 15.0])


@dataclass
class MetricsConfig:
    """Metrics collection switch."""

    enabled: bool = True


@dataclass
class RuntimeConfig:
    """Everything the v3 runtime needs to pace and parallelise itself."""

    poll_interval_seconds: float = 5.0
    max_concurrent_tasks: int = 3
    chunk_flush_ms: int = 500
    chunk_max_bytes: int = 16384
    ws: WsConfig = field(default_factory=WsConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    source_path: Path | None = None


def _as_bool(raw: Any, where: str) -> bool:
    """Coerce YAML/ENV truthiness, rejecting anything ambiguous."""
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{where}: expected a boolean, got {raw!r}")


def _as_int(raw: Any, where: str, *, minimum: int | None = None) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: expected an integer, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {value}")
    return value


def _as_float(raw: Any, where: str, *, minimum: float | None = None) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: expected a number, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {value}")
    return value


# ENV var -> (dotted config path, coercion callable).
ENV_OVERRIDES: dict[str, tuple[str, Callable[[Any, str], Any]]] = {
    "IDDO_POLL_INTERVAL_SECONDS": ("poll_interval_seconds", lambda r, w: _as_float(r, w, minimum=0.0)),
    "IDDO_MAX_CONCURRENT_TASKS": ("max_concurrent_tasks", lambda r, w: _as_int(r, w, minimum=1)),
    "IDDO_CHUNK_FLUSH_MS": ("chunk_flush_ms", lambda r, w: _as_int(r, w, minimum=1)),
    "IDDO_CHUNK_MAX_BYTES": ("chunk_max_bytes", lambda r, w: _as_int(r, w, minimum=1)),
    "IDDO_WS_ENABLED": ("ws.enabled", _as_bool),
    "IDDO_WS_HOST": ("ws.host", lambda r, w: str(r)),
    "IDDO_WS_PORT": ("ws.port", lambda r, w: _as_int(r, w, minimum=1)),
    "IDDO_WS_TOKEN": ("ws.token", lambda r, w: str(r)),
    "IDDO_RETRY_MAX_ATTEMPTS": ("retry.default_max_attempts", lambda r, w: _as_int(r, w, minimum=1)),
    "IDDO_METRICS_ENABLED": ("metrics.enabled", _as_bool),
}


def _apply_env_overrides(cfg: RuntimeConfig, env: dict[str, str] | None = None) -> RuntimeConfig:
    """Overlay ENV vars onto an already-parsed RuntimeConfig, in place."""
    env = os.environ if env is None else env
    for var, (dotted, coerce) in ENV_OVERRIDES.items():
        if var not in env:
            continue
        value = coerce(env[var], var)
        target: Any = cfg
        *parents, leaf = dotted.split(".")
        for part in parents:
            target = getattr(target, part)
        setattr(target, leaf, value)
    return cfg


def load_runtime_config(
    path: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> RuntimeConfig:
    """Load ``config.yaml``, validate it, then overlay ENV overrides.

    A missing file is not an error — every field has a default, so an
    un-migrated v2 install gets v2 behaviour. An explicit ``path`` that does
    not exist *is* an error, because the caller clearly meant it.
    """
    if path is not None:
        candidates = [Path(path)]
        if not candidates[0].exists():
            raise FileNotFoundError(f"runtime config not found: {path}")
    else:
        candidates = DEFAULT_RUNTIME_CONFIG_PATHS

    data: dict[str, Any] = {}
    source: Path | None = None
    for candidate in candidates:
        if candidate.exists():
            with open(candidate, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            source = candidate
            break

    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a mapping")

    cfg = RuntimeConfig(source_path=source)

    if "poll_interval_seconds" in data:
        cfg.poll_interval_seconds = _as_float(
            data["poll_interval_seconds"], "poll_interval_seconds", minimum=0.0
        )
    if "max_concurrent_tasks" in data:
        cfg.max_concurrent_tasks = _as_int(
            data["max_concurrent_tasks"], "max_concurrent_tasks", minimum=1
        )
    if "chunk_flush_ms" in data:
        cfg.chunk_flush_ms = _as_int(data["chunk_flush_ms"], "chunk_flush_ms", minimum=1)
    if "chunk_max_bytes" in data:
        cfg.chunk_max_bytes = _as_int(data["chunk_max_bytes"], "chunk_max_bytes", minimum=1)

    ws = data.get("ws") or {}
    if not isinstance(ws, dict):
        raise ConfigError("ws: expected a mapping")
    if "enabled" in ws:
        cfg.ws.enabled = _as_bool(ws["enabled"], "ws.enabled")
    if "host" in ws:
        cfg.ws.host = str(ws["host"])
    if "port" in ws:
        cfg.ws.port = _as_int(ws["port"], "ws.port", minimum=1)
    if ws.get("token"):
        cfg.ws.token = str(ws["token"])
    if ws.get("token_path"):
        cfg.ws.token_path = Path(str(ws["token_path"])).expanduser()

    retry = data.get("retry") or {}
    if not isinstance(retry, dict):
        raise ConfigError("retry: expected a mapping")
    if "default_max_attempts" in retry:
        cfg.retry.default_max_attempts = _as_int(
            retry["default_max_attempts"], "retry.default_max_attempts", minimum=1
        )
    if "default_backoff_seconds" in retry:
        raw_backoff = retry["default_backoff_seconds"]
        if not isinstance(raw_backoff, (list, tuple)):
            raise ConfigError("retry.default_backoff_seconds: expected a list")
        cfg.retry.default_backoff_seconds = [
            _as_float(item, "retry.default_backoff_seconds[]", minimum=0.0)
            for item in raw_backoff
        ]

    metrics = data.get("metrics") or {}
    if not isinstance(metrics, dict):
        raise ConfigError("metrics: expected a mapping")
    if "enabled" in metrics:
        cfg.metrics.enabled = _as_bool(metrics["enabled"], "metrics.enabled")

    return _apply_env_overrides(cfg, env)


def ensure_ws_token(cfg: RuntimeConfig) -> str:
    """Return the WebSocket bearer token, minting and persisting one if needed.

    The token file is created 0600 so a shared machine cannot read it. An
    explicit ``ws.token`` in config or ENV wins and is never written to disk.
    """
    if cfg.ws.token:
        return cfg.ws.token

    path = cfg.ws.token_path
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            cfg.ws.token = token
            return token

    import secrets

    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows / exotic filesystems: best effort.
    cfg.ws.token = token
    return token

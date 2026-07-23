"""
Configuration loader.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATHS = [
    Path.home() / ".iddo-harness" / "policy.yaml",
    Path(__file__).parent.parent / "policy.yaml",
]


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

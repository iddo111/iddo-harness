"""Install the Iddo Harness MCP entry into Claude Desktop configuration."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


SERVER_NAME = "iddo-harness"


def default_config_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    appdata = env.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set; cannot locate Claude Desktop config")
    return Path(appdata) / "Claude" / "claude_desktop_config.json"


def server_entry(*, python: str | None = None, policy_path: str | None = None,
                 runtime_path: str | None = None,
                 source_root: str | None = None) -> dict[str, Any]:
    root = Path(source_root).resolve() if source_root else Path(__file__).resolve().parents[1]
    policy = Path(policy_path).resolve() if policy_path else root / "policy.yaml"
    runtime = Path(runtime_path).resolve() if runtime_path else root / "config.yaml"
    args = [
        "-m", "agent.mcp_server", "--transport", "stdio",
        "--config", str(policy),
        "--runtime-config", str(runtime),
    ]
    return {
        "command": python or sys.executable,
        "args": args,
        # Claude Desktop does not guarantee that MCP servers start in the
        # repository directory.  Make the requested ``python -m`` contract
        # independent of the host process working directory.
        "env": {"PYTHONPATH": str(root)},
    }


def merged_config(existing: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    out = dict(existing)
    servers = dict(out.get("mcpServers") or {})
    servers[SERVER_NAME] = entry
    out["mcpServers"] = servers
    return out


def install(*, path: Path | None = None, dry_run: bool = False,
            python: str | None = None, policy_path: str | None = None,
            runtime_path: str | None = None) -> tuple[Path, str]:
    path = path or default_config_path()
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"refusing to overwrite invalid JSON at {path}: {exc}") from exc
        if not isinstance(existing, dict):
            raise RuntimeError(f"refusing to overwrite non-object JSON at {path}")
    else:
        existing = {}
    rendered = json.dumps(
        merged_config(existing, server_entry(python=python, policy_path=policy_path,
                                             runtime_path=runtime_path)),
        ensure_ascii=False, indent=2,
    ) + "\n"
    if dry_run:
        return path, rendered
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass
    return path, rendered

"""Authenticated agent identity carried across transports and child tasks."""
from __future__ import annotations

import re
from typing import Any


AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
LEGACY_AGENT_ID = "legacy"


def validate_agent_id(agent_id: str) -> str:
    raw = str(agent_id or "")
    if raw != raw.strip():
        raise ValueError("agent_id must not contain leading or trailing whitespace")
    value = raw.lower()
    if not AGENT_ID_RE.fullmatch(value):
        raise ValueError("agent_id must match [a-z][a-z0-9_-]{0,63}")
    return value


def bind_authenticated_identity(
    task: Any,
    agent_id: str,
    *,
    transport: str,
    client_id: str = "",
) -> Any:
    """Bind transport-authenticated identity to a task.

    These attributes are intentionally separate from the producer-controlled
    AMP body. A caller may *claim* any ``agent_id`` in JSON; only a transport
    that completed authentication may call this function and establish the
    identity used by policy and audit.
    """
    task.authenticated_agent_id = validate_agent_id(agent_id)
    task.transport = str(transport or "unknown")
    task.client_id = str(client_id or "")
    return task


def authenticated_agent_id(task: Any) -> str:
    value = getattr(task, "authenticated_agent_id", "")
    if not value:
        return LEGACY_AGENT_ID
    try:
        return validate_agent_id(value)
    except ValueError:
        return LEGACY_AGENT_ID


def copy_authenticated_identity(parent: Any, child: Any) -> Any:
    """Propagate an already-authenticated identity to an in-process child."""
    value = getattr(parent, "authenticated_agent_id", "")
    if value:
        bind_authenticated_identity(
            child,
            value,
            transport=getattr(parent, "transport", "internal"),
            client_id=getattr(parent, "client_id", ""),
        )
    return child

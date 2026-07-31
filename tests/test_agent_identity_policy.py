from dataclasses import dataclass, field

import pytest

from agent_identity import authenticated_agent_id, bind_authenticated_identity, validate_agent_id
from config import load_config
from executor import Executor
from policy import Decision, PolicyEngine


@dataclass
class Cfg:
    auto_allow: dict = field(default_factory=lambda: {"commands": ["dir*"]})
    require_confirm: dict = field(default_factory=lambda: {"commands": ["patch *"]})
    block: dict = field(default_factory=lambda: {"commands": ["format*"]})
    agents: dict = field(default_factory=lambda: {
        "profiles": {
            "perplexity": {
                "allowed_kinds": ["read_file", "patch_file"],
                "override_global_confirm": True,
                "auto_allow": {
                    "commands": ["patch *"],
                    "paths": {"write": ["D:\\CLAUDE\\**"]},
                },
            }
        }
    })


@dataclass
class Task:
    id: str
    kind: str
    payload: dict = field(default_factory=dict)
    authenticated_agent_id: str = ""
    transport: str = "git"
    client_id: str = ""


def test_claimed_payload_agent_id_is_not_trusted():
    task = Task("t", "read_file", {"agent_id": "perplexity"})
    assert authenticated_agent_id(task) == "legacy"


def test_transport_can_bind_a_valid_identity():
    task = bind_authenticated_identity(Task("t", "read_file"), "perplexity", transport="mcp", client_id="p1")
    assert authenticated_agent_id(task) == "perplexity"
    assert task.transport == "mcp"
    assert task.client_id == "p1"


def test_agent_kind_allowlist_is_enforced_at_executor_entry():
    executor = Executor(PolicyEngine(Cfg()))
    task = bind_authenticated_identity(Task("t", "shell", {"command": "dir"}), "perplexity", transport="mcp")
    result = executor.run(task)
    assert result.ok is False
    assert result.decision == "block"
    assert "not allowed" in result.error


def test_agent_override_can_auto_allow_patch_inside_scoped_root():
    policy = PolicyEngine(Cfg())
    with policy.bind_task(bind_authenticated_identity(Task("t", "patch_file"), "perplexity", transport="mcp")):
        decision, reason = policy.decide("patch D:\\CLAUDE\\x.py", ["D:\\CLAUDE\\x.py"])
    assert decision is Decision.AUTO
    assert "agent auto-allowed" in reason


def test_agent_override_does_not_escape_write_roots():
    policy = PolicyEngine(Cfg())
    with policy.bind_task(bind_authenticated_identity(Task("t", "patch_file"), "perplexity", transport="mcp")):
        decision, reason = policy.decide("patch C:\\temp\\x.py", ["C:\\temp\\x.py"])
    assert decision is Decision.CONFIRM
    assert "outside autonomous roots" in reason


def test_global_block_still_wins_over_agent_power():
    policy = PolicyEngine(Cfg())
    with policy.bind_task(bind_authenticated_identity(Task("t", "read_file"), "perplexity", transport="mcp")):
        decision, _ = policy.decide("format C:")
    assert decision is Decision.BLOCK


def test_confirm_resume_rechecks_agent_kind_allowlist():
    executor = Executor(PolicyEngine(Cfg()))
    task = bind_authenticated_identity(Task("t", "shell", {"command": "dir"}), "perplexity", transport="mcp")
    result = executor.resume_after_confirm(task, approved=True)
    assert result.decision == "block"


def test_repository_policy_loads_perplexity_profile():
    cfg = load_config("policy.yaml")
    assert "perplexity" in cfg.agents["profiles"]
    assert cfg.agents["profiles"]["perplexity"]["allowed_kinds"] == ["*"]


@pytest.mark.parametrize("value", [
    "", "1agent", "-agent", "_agent", "agent name", "agent/name",
    "agent.name", "agent:root", "פרפלקסיטי", "agent\nroot", "a" * 65, " agent ",
])
def test_invalid_authenticated_agent_ids_are_rejected(value):
    with pytest.raises(ValueError):
        validate_agent_id(value)


@pytest.mark.parametrize(("value", "expected"), [
    ("perplexity", "perplexity"),
    ("claude", "claude"),
    ("codex-1", "codex-1"),
    ("glm_2", "glm_2"),
    ("Perplexity", "perplexity"),
])
def test_valid_authenticated_agent_ids_are_canonicalized(value, expected):
    assert validate_agent_id(value) == expected

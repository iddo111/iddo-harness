"""
Tests for agent/confirm.py and its integration points:

  (a) Decision.CONFIRM causes the executor to write a pending file instead
      of just refusing.
  (b) An approved-<id>.json file causes poller.scan_pending() to surface the
      task for resume_after_confirm(), which then actually executes it.
  (c) Pending confirmations older than the configured timeout are
      auto-denied by sweep_timeouts() / scan_pending().
"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from confirm import ConfirmManager, PendingConfirmation
from policy import Decision, PolicyEngine
from executor import Executor
from poller import GithubPoller, Task


@dataclass
class FakeCfg:
    """Minimal stand-in for config.Config, just enough for the modules under test."""
    owner: str = "test-owner"
    auto_allow: dict = field(default_factory=lambda: {"commands": ["ls*"]})
    require_confirm: dict = field(default_factory=lambda: {"commands": ["pip install*"]})
    block: dict = field(default_factory=lambda: {"commands": ["rm -rf /"]})
    polling: dict = field(default_factory=lambda: {"interval_seconds": 5})
    paths: dict = field(default_factory=dict)
    transport: dict = field(
        default_factory=lambda: {"repo": "test-owner/test-bridge", "task_dir": "tasks/", "result_dir": "results/"}
    )
    confirm: dict = field(default_factory=lambda: {"timeout_minutes": 30})


@pytest.fixture
def cfg():
    return FakeCfg()


@pytest.fixture
def confirm_manager(cfg, tmp_path):
    return ConfirmManager(cfg, local_dir=tmp_path / "pending")


@pytest.fixture
def policy(cfg):
    return PolicyEngine(cfg)


@pytest.fixture
def executor(policy, confirm_manager):
    return Executor(policy, confirm_manager)


def make_task(task_id="task-1", kind="shell", payload=None):
    return Task(id=task_id, kind=kind, payload=payload or {})


# ---------------------------------------------------------------------------
# (a) CONFIRM decision writes a pending file
# ---------------------------------------------------------------------------
def test_confirm_decision_writes_pending_file(executor, confirm_manager):
    task = make_task(payload={"command": "pip install requests"})
    result = executor.run(task)

    assert result.ok is False
    assert result.decision == "confirm_required"

    pending_path = confirm_manager.local_dir / f"pending-confirm-{task.id}.json"
    assert pending_path.exists()

    data = json.loads(pending_path.read_text(encoding="utf-8"))
    assert data["task_id"] == task.id
    assert data["command"] == "pip install requests"
    assert "Approve task" in data["message"]
    assert f"iddo-harness confirm {task.id} --approve" in data["message"]


def test_confirm_manager_create_includes_one_liner(confirm_manager):
    task = make_task(task_id="abc123", payload={"command": "git push origin main"})
    pc = confirm_manager.create(task, reason="requires confirmation: git push*")
    assert isinstance(pc, PendingConfirmation)
    assert "abc123" in pc.message
    assert "git push origin main" in pc.message


# ---------------------------------------------------------------------------
# (b) approved file triggers execution via scan_pending + resume_after_confirm
# ---------------------------------------------------------------------------
def test_approved_file_triggers_execution(cfg, tmp_path, confirm_manager, executor):
    poller = GithubPoller(cfg)
    # Avoid any real git/gh network calls during scan_pending's _sync().
    poller._sync = lambda: None

    task = make_task(task_id="task-echo", payload={"command": "echo hello-from-confirm"})
    executor.run(task)  # parks it as pending (ls-only auto_allow means this needs confirm)

    pending_path = confirm_manager.local_dir / f"pending-confirm-{task.id}.json"
    assert pending_path.exists()

    # Simulate the CLI's `confirm --approve`
    confirm_manager.respond(task.id, approve=True)

    resolved = poller.scan_pending(confirm_manager)
    assert len(resolved) == 1
    resumed_task, approved = resolved[0]
    assert approved is True
    assert resumed_task.id == task.id

    result = executor.resume_after_confirm(resumed_task, approved)
    assert result.ok is True
    assert "hello-from-confirm" in result.stdout

    # pending marker should be cleaned up after being picked up
    assert not pending_path.exists()


def test_denied_file_drops_task(cfg, confirm_manager, executor):
    poller = GithubPoller(cfg)
    poller._sync = lambda: None

    task = make_task(task_id="task-denyme", payload={"command": "pip install malicious-pkg"})
    executor.run(task)

    confirm_manager.respond(task.id, approve=False)

    resolved = poller.scan_pending(confirm_manager)
    assert len(resolved) == 1
    resumed_task, approved = resolved[0]
    assert approved is False

    result = executor.resume_after_confirm(resumed_task, approved)
    assert result.ok is False
    assert result.decision == "denied"


# ---------------------------------------------------------------------------
# (c) 30-minute timeout auto-denies
# ---------------------------------------------------------------------------
def test_timeout_auto_denies_stale_pending(confirm_manager):
    task = make_task(task_id="task-stale", payload={"command": "pip install stale-pkg"})
    pc = confirm_manager.create(task, reason="requires confirmation: pip install*")

    # Backdate the pending file's created_ts beyond the 30-minute timeout.
    pending_path = confirm_manager.local_dir / f"pending-confirm-{task.id}.json"
    data = json.loads(pending_path.read_text(encoding="utf-8"))
    data["created_ts"] = time.time() - (31 * 60)
    pending_path.write_text(json.dumps(data), encoding="utf-8")

    denied = confirm_manager.sweep_timeouts()
    assert task.id in denied

    denied_path = confirm_manager.local_dir / f"denied-{task.id}.json"
    assert denied_path.exists()
    assert confirm_manager.check_response(task.id) == "denied"


def test_fresh_pending_not_timed_out(confirm_manager):
    task = make_task(task_id="task-fresh", payload={"command": "pip install fresh-pkg"})
    confirm_manager.create(task, reason="requires confirmation: pip install*")

    denied = confirm_manager.sweep_timeouts()
    assert task.id not in denied
    assert confirm_manager.check_response(task.id) is None


def test_custom_timeout_minutes_respected(cfg, tmp_path):
    cfg.confirm = {"timeout_minutes": 1}
    cm = ConfirmManager(cfg, local_dir=tmp_path / "pending2")

    task = make_task(task_id="task-quick-timeout", payload={"command": "pip install x"})
    cm.create(task, reason="requires confirmation")

    pending_path = cm.local_dir / f"pending-confirm-{task.id}.json"
    data = json.loads(pending_path.read_text(encoding="utf-8"))
    data["created_ts"] = time.time() - 90  # 1.5 minutes ago, timeout is 1 minute
    pending_path.write_text(json.dumps(data), encoding="utf-8")

    denied = cm.sweep_timeouts()
    assert task.id in denied

"""
Smoke tests for agent/cli.py using click's CliRunner.

These tests avoid any real network / git / gh calls: `submit` and `status`
are exercised with `--no-push` and a temp bridge dir respectively, and a
throwaway policy.yaml + HOME are used so nothing touches the real
~/.iddo-harness.
"""
import json
import os
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

import cli as cli_module  # agent/cli.py, added to sys.path by conftest.py


POLICY_YAML = textwrap.dedent(
    """
    version: 1
    owner: test-owner
    auto_allow:
      commands:
        - "ls*"
    require_confirm:
      commands:
        - "pip install*"
    block:
      commands:
        - "rm -rf /"
    polling:
      interval_seconds: 5
      max_concurrent_tasks: 3
      task_timeout_seconds: 600
    confirm:
      timeout_minutes: 30
    transport:
      type: github
      repo: test-owner/test-bridge
      task_dir: tasks/
      result_dir: results/
    """
)


@pytest.fixture
def policy_file(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text(POLICY_YAML, encoding="utf-8")
    return str(p)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect ~ (and the bridge repo tmp dir) so tests never touch real
    ~/.iddo-harness or a shared /tmp bridge checkout from other test runs.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_tmp = tmp_path / "tmp"
    fake_tmp.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setattr(cli_module, "LOCK_PATH", fake_home / ".iddo-harness" / "agent.lock")
    monkeypatch.setattr(cli_module, "AUDIT_LOG_PATH", fake_home / ".iddo-harness" / "audit.log")
    monkeypatch.setattr(cli_module.tempfile, "gettempdir", lambda: str(fake_tmp))
    return fake_home


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
def test_policy_check_auto(runner, policy_file, isolated_home):
    result = runner.invoke(cli_module.cli, ["--config", policy_file, "policy", "check", "ls -la"])
    assert result.exit_code == 0, result.output
    assert "Decision: auto" in result.output


def test_policy_check_block(runner, policy_file, isolated_home):
    result = runner.invoke(cli_module.cli, ["--config", policy_file, "policy", "check", "rm -rf /"])
    assert result.exit_code == 0, result.output
    assert "Decision: block" in result.output


def test_policy_check_confirm(runner, policy_file, isolated_home):
    result = runner.invoke(cli_module.cli, ["--config", policy_file, "policy", "check", "pip install requests"])
    assert result.exit_code == 0, result.output
    assert "Decision: confirm" in result.output


# ---------------------------------------------------------------------------
def test_submit_shell_writes_task_file(runner, policy_file, isolated_home):
    result = runner.invoke(
        cli_module.cli,
        ["--config", policy_file, "submit", "shell", "--command", "ls -la", "--no-push"],
    )
    assert result.exit_code == 0, result.output
    assert "Submitted task" in result.output

    # Locate the bridge dir the CLI wrote to, and confirm a task json landed there.
    import config as config_module
    cfg = config_module.load_config(policy_file)
    local = cli_module._bridge_local_dir(cfg)
    task_dir = local / cfg.transport.get("task_dir", "tasks/")
    files = list(task_dir.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    # Either AMP-wrapped or legacy shape should carry the shell command.
    body = data.get("payload", {}).get("body", data) if "payload" in data else data.get("payload", data)
    assert "ls -la" in json.dumps(data)


def test_submit_requires_command_for_shell(runner, policy_file, isolated_home):
    result = runner.invoke(cli_module.cli, ["--config", policy_file, "submit", "shell", "--no-push"])
    assert result.exit_code != 0
    assert "requires --command" in result.output


def test_submit_write_file_requires_path(runner, policy_file, isolated_home):
    result = runner.invoke(
        cli_module.cli,
        ["--config", policy_file, "submit", "write_file", "--content", "hello", "--no-push"],
    )
    assert result.exit_code != 0
    assert "requires --path" in result.output


# ---------------------------------------------------------------------------
def test_status_shows_not_running(runner, policy_file, isolated_home):
    result = runner.invoke(cli_module.cli, ["--config", policy_file, "status"])
    assert result.exit_code == 0, result.output
    assert "Agent running: no" in result.output
    assert "Pending confirmations: 0" in result.output


def test_status_shows_running_when_lock_present(runner, policy_file, isolated_home):
    cli_module.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    cli_module.LOCK_PATH.write_text("12345", encoding="utf-8")

    result = runner.invoke(cli_module.cli, ["--config", policy_file, "status"])
    assert result.exit_code == 0, result.output
    assert "Agent running: yes" in result.output

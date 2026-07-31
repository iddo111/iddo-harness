"""
Tests for agent/sandbox.py.

firejail is not installed in CI, and that is itself one of the cases worth
testing: a missing backend must warn once and then run the command anyway. Where
a present backend is needed, ``shutil.which`` is monkeypatched rather than
firejail being required.
"""
import shlex

import pytest

import sandbox


@pytest.fixture(autouse=True)
def fresh_warnings():
    sandbox.reset_warnings()
    yield
    sandbox.reset_warnings()


@pytest.fixture
def with_firejail(monkeypatch):
    """Pretend firejail is installed at /usr/bin/firejail."""
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(
        sandbox.shutil, "which",
        lambda name: "/usr/bin/firejail" if name == sandbox.FIREJAIL else None,
    )


@pytest.fixture
def without_firejail(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)


def _cfg(**sandbox_block):
    class Cfg:
        sandbox = sandbox_block

    return Cfg()


# ---------------------------------------------------------------------------
# Level resolution
# ---------------------------------------------------------------------------
def test_normalise_level_accepts_the_three_levels():
    for level in ("none", "light", "strict"):
        assert sandbox.normalise_level(level) == level


def test_normalise_level_is_case_insensitive():
    assert sandbox.normalise_level("STRICT") == "strict"


def test_unknown_level_falls_back_to_none():
    assert sandbox.normalise_level("paranoid") == sandbox.LEVEL_NONE


def test_missing_level_falls_back_to_none():
    assert sandbox.normalise_level(None) == sandbox.LEVEL_NONE


def test_level_for_kind_uses_the_default():
    assert sandbox.level_for_kind(_cfg(enabled=True, default="light"), "shell") == "light"


def test_per_kind_overrides_the_default():
    cfg = _cfg(enabled=True, default="light", per_kind={"shell_stream": "strict"})
    assert sandbox.level_for_kind(cfg, "shell_stream") == "strict"
    assert sandbox.level_for_kind(cfg, "shell") == "light"


def test_disabled_sandbox_forces_none():
    cfg = _cfg(enabled=False, default="strict", per_kind={"shell": "strict"})
    assert sandbox.level_for_kind(cfg, "shell") == sandbox.LEVEL_NONE


def test_config_without_a_sandbox_block_is_none():
    class Cfg:
        pass

    assert sandbox.level_for_kind(Cfg(), "shell") == sandbox.LEVEL_NONE


def test_v2_default_policy_keeps_behaviour_unchanged():
    """`default: none` is what makes this backward compatible."""
    assert sandbox.level_for_kind(_cfg(enabled=True, default="none"), "shell") == "none"


# ---------------------------------------------------------------------------
# Wrapping
# ---------------------------------------------------------------------------
def test_level_none_returns_the_command_untouched(with_firejail):
    assert sandbox.wrap_popen_args("ls -la", "none") == "ls -la"


def test_empty_command_is_returned_untouched(with_firejail):
    assert sandbox.wrap_popen_args("", "strict") == ""


def test_light_wraps_with_private_tmp(with_firejail):
    wrapped = sandbox.wrap_popen_args("ls -la", "light")
    args = shlex.split(wrapped)
    assert args[0] == sandbox.FIREJAIL
    assert "--private-tmp" in args
    assert "--net=none" not in args


def test_strict_disables_the_network(with_firejail):
    args = shlex.split(sandbox.wrap_popen_args("curl example.com", "strict"))
    assert "--net=none" in args


def test_strict_is_read_only_outside_cwd(with_firejail, tmp_path):
    args = shlex.split(sandbox.wrap_popen_args("touch x", "strict", cwd=tmp_path))
    assert "--read-only=/" in args
    assert f"--read-write={tmp_path.resolve()}" in args


def test_strict_without_cwd_grants_no_writable_island(with_firejail):
    args = shlex.split(sandbox.wrap_popen_args("touch x", "strict"))
    assert "--read-only=/" in args
    assert not any(a.startswith("--read-write=") for a in args)


def test_the_users_command_stays_one_argument(with_firejail):
    """Pipes must run inside the jail, not outside it."""
    wrapped = sandbox.wrap_popen_args("cat /etc/hostname | wc -l", "light")
    args = shlex.split(wrapped)
    assert args[-1] == "cat /etc/hostname | wc -l"
    assert args[-3:-1] == ["/bin/sh", "-c"]


def test_wrapping_survives_quotes_in_the_command(with_firejail):
    cmd = """python -c "print('hi')" """
    assert shlex.split(sandbox.wrap_popen_args(cmd, "light"))[-1] == cmd


def test_missing_firejail_runs_unsandboxed(without_firejail):
    assert sandbox.wrap_popen_args("ls -la", "strict") == "ls -la"


def test_missing_firejail_warns_once(without_firejail, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        sandbox.wrap_popen_args("ls", "strict")
        sandbox.wrap_popen_args("ls", "strict")
    assert caplog.text.count("firejail is not installed") == 1


def test_windows_without_pywin32_runs_unsandboxed(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    monkeypatch.setattr(sandbox, "_pywin32_available", lambda: False)
    with caplog.at_level(logging.WARNING):
        assert sandbox.wrap_popen_args("dir", "strict") == "dir"
    assert "no Windows backend" in caplog.text


def test_windows_with_pywin32_does_not_rewrite_the_command(monkeypatch):
    """Job Objects are applied to the handle, so the command line is unchanged."""
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    monkeypatch.setattr(sandbox, "_pywin32_available", lambda: True)
    assert sandbox.wrap_popen_args("dir", "strict") == "dir"


# ---------------------------------------------------------------------------
# Availability reporting
# ---------------------------------------------------------------------------
def test_backend_is_firejail_when_installed(with_firejail):
    assert sandbox.backend() == sandbox.FIREJAIL


def test_backend_is_none_when_nothing_is_installed(without_firejail):
    assert sandbox.backend() is None


def test_level_none_is_always_available(without_firejail):
    assert sandbox.available("none") is True


def test_real_levels_need_a_backend(without_firejail):
    assert sandbox.available("light") is False
    assert sandbox.available("strict") is False


def test_describe_reports_whether_it_is_enforcing(with_firejail):
    info = sandbox.describe()
    assert info["backend"] == sandbox.FIREJAIL
    assert info["enforcing"] is True
    assert set(info["levels"]) == {"none", "light", "strict"}


def test_describe_reports_not_enforcing_without_a_backend(without_firejail):
    assert sandbox.describe()["enforcing"] is False


def test_confine_process_is_a_no_op_on_posix(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")

    class Proc:
        pid = 1234

    assert sandbox.confine_process(Proc(), "strict") is False


# ---------------------------------------------------------------------------
# Executor integration
# ---------------------------------------------------------------------------
def _shell_cfg(**sandbox_block):
    from config import Config

    return Config(
        version=1, owner="test",
        auto_allow={"commands": ["echo*"]}, require_confirm={}, block={"commands": []},
        polling={"interval_seconds": 5, "max_concurrent_tasks": 1, "task_timeout_seconds": 60},
        confirm={"timeout_minutes": 30}, paths={},
        transport={"type": "github", "repo": "x/y"},
        sandbox=sandbox_block,
    )


def test_executor_records_the_level_in_result_metadata(monkeypatch, with_firejail):
    """A sandboxed run says so in its result, so the consumer can tell."""
    from executor import Executor
    from policy import PolicyEngine
    from poller import Task

    captured = {}
    real_run = sandbox.wrap_popen_args

    def spy(cmd, level=sandbox.LEVEL_NONE, cwd=None):
        captured["level"] = level
        # Strip the wrapper so the test still executes a real echo locally.
        real_run(cmd, level, cwd)
        return cmd

    monkeypatch.setattr(sandbox, "wrap_popen_args", spy)
    cfg = _shell_cfg(enabled=True, default="light")
    executor = Executor(PolicyEngine(cfg))
    result = executor.run(Task(id="t1", kind="shell", payload={"command": "echo hi"}))

    assert captured["level"] == "light"
    assert result.metadata.get("sandbox") == "light"


def test_executor_omits_sandbox_metadata_when_level_is_none():
    from executor import Executor
    from policy import PolicyEngine
    from poller import Task

    executor = Executor(PolicyEngine(_shell_cfg(enabled=True, default="none")))
    result = executor.run(Task(id="t1", kind="shell", payload={"command": "echo hi"}))
    assert "sandbox" not in result.metadata
    assert result.ok

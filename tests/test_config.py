"""
Tests for the v3 runtime config loader (``config.yaml`` -> RuntimeConfig).

Two things matter here beyond "does YAML parse": a harness with no
``config.yaml`` must keep behaving exactly like v2, and a malformed value must
fail loudly at startup rather than surfacing as a mystery later.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from file_security import private_file_permissions_ok

from config import (
    ConfigError,
    MetricsConfig,
    RetryConfig,
    RuntimeConfig,
    WsConfig,
    ensure_ws_token,
    load_config,
    load_runtime_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# -- defaults ---------------------------------------------------------------
def test_defaults_match_the_documented_v2_behaviour():
    cfg = RuntimeConfig()
    assert cfg.poll_interval_seconds == 5
    assert cfg.max_concurrent_tasks == 3
    assert cfg.chunk_flush_ms == 500
    assert cfg.chunk_max_bytes == 16384


def test_ws_is_off_by_default():
    """The WS bridge opens a port, so it must never come up unasked."""
    assert WsConfig().enabled is False
    assert WsConfig().host == "127.0.0.1"
    assert WsConfig().port == 8477


def test_retry_and_metrics_defaults():
    assert RetryConfig().default_max_attempts == 1
    assert MetricsConfig().enabled is True


def test_missing_default_config_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr("config.DEFAULT_RUNTIME_CONFIG_PATHS", [tmp_path / "nope.yaml"])
    cfg = load_runtime_config(env={})
    assert cfg.source_path is None
    assert cfg.max_concurrent_tasks == 3


def test_explicitly_named_missing_config_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_runtime_config(tmp_path / "absent.yaml")


def test_empty_file_yields_defaults(tmp_path):
    cfg = load_runtime_config(write_config(tmp_path, ""), env={})
    assert cfg.max_concurrent_tasks == 3
    assert cfg.ws.enabled is False


# -- the repo's own config.yaml --------------------------------------------
def test_repo_config_yaml_loads_and_matches_the_brief():
    cfg = load_runtime_config(REPO_ROOT / "config.yaml", env={})
    assert cfg.poll_interval_seconds == 5
    assert cfg.max_concurrent_tasks == 3
    assert cfg.chunk_flush_ms == 500
    assert cfg.chunk_max_bytes == 16384
    assert cfg.ws.enabled is False
    assert cfg.ws.host == "127.0.0.1"
    assert cfg.ws.port == 8477
    assert cfg.retry.default_max_attempts == 1
    assert cfg.metrics.enabled is True


def test_source_path_records_where_the_config_came_from(tmp_path):
    path = write_config(tmp_path, "max_concurrent_tasks: 7\n")
    assert load_runtime_config(path, env={}).source_path == path


# -- parsing ----------------------------------------------------------------
def test_scalars_are_read_from_the_file(tmp_path):
    path = write_config(
        tmp_path,
        "poll_interval_seconds: 0.5\n"
        "max_concurrent_tasks: 8\n"
        "chunk_flush_ms: 250\n"
        "chunk_max_bytes: 4096\n",
    )
    cfg = load_runtime_config(path, env={})
    assert cfg.poll_interval_seconds == 0.5
    assert cfg.max_concurrent_tasks == 8
    assert cfg.chunk_flush_ms == 250
    assert cfg.chunk_max_bytes == 4096


def test_nested_sections_are_read(tmp_path):
    path = write_config(
        tmp_path,
        "ws:\n  enabled: true\n  host: 0.0.0.0\n  port: 9999\n"
        "retry:\n  default_max_attempts: 4\n  default_backoff_seconds: [1, 2.5, 10]\n"
        "metrics:\n  enabled: false\n",
    )
    cfg = load_runtime_config(path, env={})
    assert cfg.ws.enabled is True
    assert cfg.ws.host == "0.0.0.0"
    assert cfg.ws.port == 9999
    assert cfg.retry.default_max_attempts == 4
    assert cfg.retry.default_backoff_seconds == [1.0, 2.5, 10.0]
    assert cfg.metrics.enabled is False


@pytest.mark.parametrize("raw", ["true", "True", "yes", "on", "1"])
def test_truthy_spellings_all_enable(tmp_path, raw):
    cfg = load_runtime_config(write_config(tmp_path, f"ws:\n  enabled: '{raw}'\n"), env={})
    assert cfg.ws.enabled is True


@pytest.mark.parametrize("raw", ["false", "False", "no", "off", "0"])
def test_falsey_spellings_all_disable(tmp_path, raw):
    cfg = load_runtime_config(write_config(tmp_path, f"metrics:\n  enabled: '{raw}'\n"), env={})
    assert cfg.metrics.enabled is False


def test_token_path_is_user_expanded(tmp_path):
    path = write_config(tmp_path, "ws:\n  token_path: ~/somewhere/ws-token\n")
    cfg = load_runtime_config(path, env={})
    assert "~" not in str(cfg.ws.token_path)
    assert cfg.ws.token_path.name == "ws-token"


# -- validation -------------------------------------------------------------
def test_non_mapping_top_level_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_runtime_config(write_config(tmp_path, "- just\n- a list\n"), env={})


@pytest.mark.parametrize("section", ["ws", "retry", "metrics"])
def test_non_mapping_section_is_rejected(tmp_path, section):
    with pytest.raises(ConfigError):
        load_runtime_config(write_config(tmp_path, f"{section}: 12\n"), env={})


def test_unparseable_number_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_runtime_config(write_config(tmp_path, "max_concurrent_tasks: banana\n"), env={})


def test_concurrency_below_one_is_rejected(tmp_path):
    """A zero-worker pool would silently execute nothing at all."""
    with pytest.raises(ConfigError):
        load_runtime_config(write_config(tmp_path, "max_concurrent_tasks: 0\n"), env={})


def test_non_boolean_flag_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_runtime_config(write_config(tmp_path, "ws:\n  enabled: perhaps\n"), env={})


def test_backoff_must_be_a_list(tmp_path):
    with pytest.raises(ConfigError):
        load_runtime_config(
            write_config(tmp_path, "retry:\n  default_backoff_seconds: 5\n"), env={}
        )


def test_error_message_names_the_offending_key(tmp_path):
    with pytest.raises(ConfigError, match="chunk_max_bytes"):
        load_runtime_config(write_config(tmp_path, "chunk_max_bytes: nope\n"), env={})


# -- ENV overrides ----------------------------------------------------------
def test_env_overrides_the_file(tmp_path):
    path = write_config(tmp_path, "max_concurrent_tasks: 2\n")
    cfg = load_runtime_config(path, env={"IDDO_MAX_CONCURRENT_TASKS": "9"})
    assert cfg.max_concurrent_tasks == 9


def test_env_reaches_nested_fields(tmp_path):
    cfg = load_runtime_config(
        write_config(tmp_path, ""),
        env={"IDDO_WS_ENABLED": "true", "IDDO_WS_PORT": "1234", "IDDO_METRICS_ENABLED": "false"},
    )
    assert cfg.ws.enabled is True
    assert cfg.ws.port == 1234
    assert cfg.metrics.enabled is False


def test_unrelated_env_vars_are_ignored(tmp_path):
    cfg = load_runtime_config(write_config(tmp_path, ""), env={"PATH": "/nowhere"})
    assert cfg.max_concurrent_tasks == 3


def test_invalid_env_value_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_runtime_config(write_config(tmp_path, ""), env={"IDDO_WS_PORT": "not-a-port"})


def test_env_can_supply_the_ws_token(tmp_path):
    cfg = load_runtime_config(write_config(tmp_path, ""), env={"IDDO_WS_TOKEN": "sekrit"})
    assert cfg.ws.token == "sekrit"


# -- ws token ---------------------------------------------------------------
def test_ensure_ws_token_mints_and_persists(tmp_path):
    cfg = RuntimeConfig(ws=WsConfig(token_path=tmp_path / "sub" / "ws-token"))
    token = ensure_ws_token(cfg)
    assert token
    assert cfg.ws.token_path.read_text(encoding="utf-8").strip() == token


def test_ensure_ws_token_is_stable_across_calls(tmp_path):
    cfg = RuntimeConfig(ws=WsConfig(token_path=tmp_path / "ws-token"))
    first = ensure_ws_token(cfg)
    second = ensure_ws_token(RuntimeConfig(ws=WsConfig(token_path=tmp_path / "ws-token")))
    assert first == second


def test_minted_token_file_is_owner_only(tmp_path):
    cfg = RuntimeConfig(ws=WsConfig(token_path=tmp_path / "ws-token"))
    ensure_ws_token(cfg)
    assert private_file_permissions_ok(cfg.ws.token_path)


def test_explicit_token_is_never_written_to_disk(tmp_path):
    """A token from ENV or config belongs to the operator, not to our token file."""
    path = tmp_path / "ws-token"
    cfg = RuntimeConfig(ws=WsConfig(token="from-env", token_path=path))
    assert ensure_ws_token(cfg) == "from-env"
    assert not path.exists()


def test_blank_token_file_is_replaced(tmp_path):
    path = tmp_path / "ws-token"
    path.write_text("   \n", encoding="utf-8")
    cfg = RuntimeConfig(ws=WsConfig(token_path=path))
    assert ensure_ws_token(cfg).strip()


def test_minted_tokens_are_unguessable(tmp_path):
    a = ensure_ws_token(RuntimeConfig(ws=WsConfig(token_path=tmp_path / "a")))
    b = ensure_ws_token(RuntimeConfig(ws=WsConfig(token_path=tmp_path / "b")))
    assert a != b
    assert len(a) >= 32


# -- backward compatibility -------------------------------------------------
def test_policy_loader_is_untouched_by_the_v3_loader():
    """``load_config`` is the v1/v2 policy path and must keep its own shape."""
    cfg = load_config(REPO_ROOT / "policy.yaml")
    assert cfg.transport
    assert hasattr(cfg, "auto_allow")
    assert not hasattr(cfg, "max_concurrent_tasks")

"""
Tests for agent/secrets_vault.py and the executor's use of it.

Two kinds of assertion here. The first kind is ordinary vault behaviour. The
second kind — the reason this module exists — is that a resolved value must not
turn up in a log line, a published result, an audit record, or a metrics label.
Those tests execute a real command that echoes the secret and then grep the
outputs.
"""
import json
import logging
from pathlib import Path

import pytest

import secrets_vault
from secrets_vault import (
    MissingSecretError,
    SecretVault,
    VaultError,
    VaultUnavailable,
    mask_text,
)

pytest.importorskip("cryptography", reason="the vault needs the cryptography package")

import signing  # noqa: E402  - imported after the cryptography guard


@pytest.fixture(autouse=True)
def clean_registry():
    """The value registry is process-wide; keep tests from leaking into each other."""
    secrets_vault.clear_registry()
    yield
    secrets_vault.clear_registry()


@pytest.fixture
def vault(tmp_path):
    priv, _ = signing.generate_keypair(tmp_path)
    return SecretVault(store_path=tmp_path / "secrets.enc", key_path=priv)


# ---------------------------------------------------------------------------
# Placeholders and masking
# ---------------------------------------------------------------------------
def test_referenced_names_finds_each_name_once():
    text = "curl -H 'K: {{secret:api}}' -H 'B: {{secret:api}}' {{secret:url}}"
    assert secrets_vault.referenced_names(text) == ["api", "url"]


def test_has_placeholder():
    assert secrets_vault.has_placeholder("x {{secret:a}}") is True
    assert secrets_vault.has_placeholder("plain text") is False


def test_mask_text_replaces_the_placeholder_with_the_name_only():
    assert mask_text("curl -H 'K: {{secret:openai_key}}'") == "curl -H 'K: <vault:openai_key>'"


def test_mask_text_leaves_ordinary_text_alone():
    assert mask_text("ls -la /tmp") == "ls -la /tmp"


def test_mask_text_redacts_a_registered_value():
    secrets_vault.register_value("sk-abcdef123456")
    assert "sk-abcdef123456" not in mask_text("Bearer sk-abcdef123456")
    assert secrets_vault.REDACTED in mask_text("Bearer sk-abcdef123456")


def test_short_values_are_not_registered():
    """Blanket-replacing a 2-char string would shred unrelated output."""
    secrets_vault.register_value("ab")
    assert secrets_vault.redact("ab cd ab") == "ab cd ab"


def test_redact_handles_overlapping_values():
    assert secrets_vault.redact("prefix-token", ["prefix-token", "prefix"]).count(
        secrets_vault.REDACTED
    ) == 1


def test_placeholder_names_cannot_smuggle_shell_metacharacters():
    assert secrets_vault.referenced_names("{{secret:a;rm -rf /}}") == []


# ---------------------------------------------------------------------------
# Vault storage
# ---------------------------------------------------------------------------
def test_set_then_get_round_trips(vault):
    vault.set("openai_key", "sk-secret-value")
    assert vault.get("openai_key") == "sk-secret-value"


def test_store_file_is_encrypted_on_disk(vault):
    vault.set("openai_key", "sk-secret-value")
    blob = vault.store_path.read_bytes()
    assert b"sk-secret-value" not in blob
    assert b"openai_key" not in blob


def test_store_file_is_not_world_readable(vault):
    vault.set("k", "v")
    assert vault.store_path.stat().st_mode & 0o077 == 0


def test_names_lists_without_values(vault):
    vault.set("b", "value-b")
    vault.set("a", "value-a")
    assert vault.names() == ["a", "b"]
    assert "value-a" not in json.dumps(vault.names())


def test_set_replaces_an_existing_value(vault):
    vault.set("k", "old")
    vault.set("k", "new")
    assert vault.get("k") == "new"


def test_delete_removes_a_secret(vault):
    vault.set("k", "v")
    assert vault.delete("k") is True
    assert vault.names() == []


def test_delete_of_an_unknown_name_is_false(vault):
    assert vault.delete("nope") is False


def test_get_of_an_unknown_name_raises(vault):
    with pytest.raises(MissingSecretError):
        vault.get("nope")


def test_invalid_name_is_rejected(vault):
    with pytest.raises(VaultError):
        vault.set("bad name!", "v")


def test_vault_is_unavailable_without_a_root_key(tmp_path):
    v = SecretVault(store_path=tmp_path / "secrets.enc", key_path=tmp_path / "absent.key")
    assert v.available is False
    with pytest.raises(VaultUnavailable):
        v.set("k", "v")


def test_a_second_vault_reads_what_the_first_wrote(tmp_path, vault):
    vault.set("k", "v")
    other = SecretVault(store_path=vault.store_path, key_path=vault.key_path)
    assert other.get("k") == "v"


def test_a_different_root_key_cannot_decrypt_the_store(tmp_path, vault):
    vault.set("k", "v")
    other_dir = tmp_path / "other"
    other_priv, _ = signing.generate_keypair(other_dir)
    stranger = SecretVault(store_path=vault.store_path, key_path=other_priv)
    with pytest.raises(VaultError):
        stranger.names()


def test_from_config_reads_the_security_block(tmp_path):
    class Cfg:
        security = {"secrets": {"store_path": str(tmp_path / "s.enc"), "key_path": str(tmp_path / "k")}}

    v = SecretVault.from_config(Cfg())
    assert v.store_path == tmp_path / "s.enc"


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def test_resolve_substitutes_the_value(vault):
    vault.set("api", "sk-123456")
    resolved, used = vault.resolve("curl -H 'X-Key: {{secret:api}}'")
    assert resolved == "curl -H 'X-Key: sk-123456'"
    assert used == {"api": "sk-123456"}


def test_resolve_without_placeholders_is_a_no_op(vault):
    assert vault.resolve("ls -la") == ("ls -la", {})


def test_resolve_raises_for_an_unknown_name(vault):
    with pytest.raises(MissingSecretError):
        vault.resolve("echo {{secret:absent}}")


def test_resolve_structure_walks_headers_and_bodies(vault):
    vault.set("api", "sk-123456")
    payload = {"headers": {"X-Key": "{{secret:api}}"}, "json": {"list": ["{{secret:api}}", 1]}}
    resolved, used = vault.resolve_structure(payload)
    assert resolved["headers"]["X-Key"] == "sk-123456"
    assert resolved["json"]["list"] == ["sk-123456", 1]
    assert used == {"api": "sk-123456"}


def test_resolve_with_passes_plain_text_through_without_a_vault():
    assert secrets_vault.resolve_with(None, "ls -la") == ("ls -la", {})


def test_resolve_with_refuses_to_ship_an_unresolved_placeholder():
    """Better a loud failure than a literal {{secret:x}} sent to a remote API."""
    with pytest.raises(VaultUnavailable):
        secrets_vault.resolve_with(None, "curl -H 'K: {{secret:api}}'")


def test_get_registers_the_value_for_later_redaction(vault):
    vault.set("api", "sk-abcdef123456")
    vault.get("api")
    assert secrets_vault.REDACTED in secrets_vault.redact("leaked sk-abcdef123456")


def test_vault_audits_the_name_but_never_the_value(tmp_path, vault):
    from audit import AuditLog

    audit_log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    vault._audit_sink = audit_log
    vault.set("api", "sk-abcdef123456")
    vault.get("api")

    text = audit_log.path.read_text(encoding="utf-8")
    assert "secret:api" in text
    assert "sk-abcdef123456" not in text
    assert [r["action"] for r in audit_log.tail(10)] == ["secret_set", "secret_get"]


# ---------------------------------------------------------------------------
# End-to-end: no leak through the executor
# ---------------------------------------------------------------------------
SECRET_VALUE = "sk-do-not-leak-abcdef"


@pytest.fixture
def executor_with_vault(tmp_path, vault):
    """A real Executor wired to a vault holding one secret."""
    from config import Config
    from executor import Executor
    from policy import PolicyEngine
    from audit import AuditLog

    vault.set("api", SECRET_VALUE)
    cfg = Config(
        version=1, owner="test",
        auto_allow={"commands": ["echo*", "python*"]},
        require_confirm={}, block={"commands": []},
        polling={"interval_seconds": 5, "max_concurrent_tasks": 1, "task_timeout_seconds": 60},
        confirm={"timeout_minutes": 30},
        paths={}, transport={"type": "github", "repo": "x/y"},
    )
    audit_log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    policy = PolicyEngine(cfg, audit_log=audit_log)
    return Executor(policy, vault=vault, audit_log=audit_log), audit_log, cfg


def _task(command: str):
    from poller import Task

    return Task(id="t-secret", kind="shell", payload={"command": command, "timeout_sec": 30})


def test_executor_substitutes_the_secret_before_running(executor_with_vault):
    executor, _, _ = executor_with_vault
    result = executor.run(_task("echo {{secret:api}}"))
    assert result.ok, result.stderr
    # The value reached the command, but the result must not carry it back.
    assert SECRET_VALUE not in result.stdout
    assert secrets_vault.REDACTED in result.stdout


def test_secret_does_not_leak_into_the_published_result(executor_with_vault):
    executor, _, _ = executor_with_vault
    result = executor.run(_task("echo {{secret:api}}"))
    assert SECRET_VALUE not in json.dumps(result.__dict__, default=str)


def test_secret_does_not_leak_into_the_audit_log(executor_with_vault):
    executor, audit_log, _ = executor_with_vault
    executor.run(_task("echo {{secret:api}}"))
    assert SECRET_VALUE not in audit_log.path.read_text(encoding="utf-8")


def test_secret_does_not_leak_into_the_python_log(executor_with_vault, caplog):
    executor, _, _ = executor_with_vault
    with caplog.at_level(logging.DEBUG):
        executor.run(_task("echo {{secret:api}}"))
    assert SECRET_VALUE not in caplog.text
    assert "<vault:api>" in caplog.text or "{{secret:api}}" not in caplog.text


def test_secret_does_not_leak_through_a_reported_envelope(executor_with_vault, tmp_path, monkeypatch):
    """The result that lands in the bridge repo (and in git) must be clean."""
    from reporter import Reporter

    executor, _, cfg = executor_with_vault
    task = _task("echo {{secret:api}}")
    result = executor.run(task)

    reporter = Reporter(cfg)
    # Point the bridge checkout at a temp dir. The git calls inside _write fail
    # harmlessly there (not a repo), and the file we care about is still written.
    monkeypatch.setattr(reporter, "_local", tmp_path / "bridge")
    reporter.send(task, result)

    written = (tmp_path / "bridge" / reporter.result_dir / f"{task.id}.json").read_text(
        encoding="utf-8"
    )
    assert SECRET_VALUE not in written
    assert "{{secret:api}}" not in written


def test_secret_does_not_leak_into_metrics_style_reporting(executor_with_vault):
    """Whatever a metrics view exposes about a task, the command is masked."""
    executor, audit_log, _ = executor_with_vault
    executor.run(_task("echo {{secret:api}}"))
    exposed = json.dumps(audit_log.tail(50))
    assert SECRET_VALUE not in exposed
    assert "{{secret:api}}" not in exposed


def test_missing_secret_fails_the_task_without_running_it(executor_with_vault):
    executor, _, _ = executor_with_vault
    result = executor.run(_task("echo {{secret:absent}}"))
    assert result.ok is False
    assert "absent" in (result.stderr or "") + (result.error or "")


def test_policy_does_not_block_a_vault_reference(executor_with_vault):
    """`*secret*` is a block pattern — the masked form must still be allowed."""
    from policy import Decision

    executor, _, _ = executor_with_vault
    decision, _reason = executor.policy.decide("echo {{secret:api}}")
    assert decision is not Decision.BLOCK


def test_policy_still_blocks_an_inline_credential(executor_with_vault):
    from policy import Decision

    executor, _, _ = executor_with_vault
    cfg = executor.policy.cfg
    cfg.block = {"commands": ["*secret*"]}
    decision, _reason = executor.policy.decide("echo my-secret-value")
    assert decision is Decision.BLOCK


def test_an_all_commented_out_secrets_block_is_not_a_crash():
    """`secrets:` with every key commented out parses as None, not as absent."""
    from config import Config

    vault = SecretVault.from_config(Config(version=1, owner="test", security={"secrets": None}))
    assert vault.store_path is not None


def test_the_shipped_policy_builds_a_vault():
    """policy.yaml ships `secrets:` with only comments under it."""
    from config import load_config

    repo_root = Path(__file__).resolve().parent.parent
    assert SecretVault.from_config(load_config(str(repo_root / "policy.yaml"))) is not None

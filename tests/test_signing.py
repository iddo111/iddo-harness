"""
Tests for agent/signing.py, agent/verify.py and installer/gen_keys.py.

The property that matters is negative: a document that has been altered by even
one byte must fail verification. Most tests here therefore sign something,
change it, and assert the verdict flips.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import signing
import verify as verify_cli

pytest.importorskip("cryptography", reason="signing needs the cryptography package")

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def keys(tmp_path):
    """A throwaway keypair in a temp dir."""
    priv, pub = signing.generate_keypair(tmp_path)
    return priv, pub


@pytest.fixture
def doc():
    return {
        "v": 1,
        "id": "task-42",
        "payload": {"body": {"ok": True, "stdout": "שלום"}, "type": "harness_result"},
        "ts": "2026-07-25T10:00:00Z",
    }


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------
def test_canonical_json_is_compact_and_sorted():
    text = signing.canonical_json({"b": 1, "a": 2})
    assert text == '{"a":2,"b":1}'


def test_canonical_json_ignores_key_order(doc):
    reordered = {k: doc[k] for k in reversed(list(doc))}
    assert signing.canonical_json(doc) == signing.canonical_json(reordered)


def test_canonical_json_excludes_the_signature_field(doc):
    signed = dict(doc, signature="ZmFrZQ==")
    assert signing.canonical_json(signed) == signing.canonical_json(doc)


def test_canonical_json_keeps_non_ascii_raw():
    assert "שלום" in signing.canonical_json({"msg": "שלום"})


def test_canonical_bytes_is_utf8_of_the_text(doc):
    assert signing.canonical_bytes(doc) == signing.canonical_json(doc).encode("utf-8")


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------
def test_generate_keypair_writes_both_keys(tmp_path):
    priv, pub = signing.generate_keypair(tmp_path)
    assert priv.exists() and pub.exists()
    assert b"PRIVATE KEY" in priv.read_bytes()
    assert b"PUBLIC KEY" in pub.read_bytes()


def test_generate_keypair_refuses_to_overwrite(keys, tmp_path):
    with pytest.raises(signing.SigningError):
        signing.generate_keypair(tmp_path)


def test_generate_keypair_force_replaces_the_key(keys, tmp_path):
    before = keys[0].read_bytes()
    signing.generate_keypair(tmp_path, force=True)
    assert keys[0].read_bytes() != before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_private_key_is_not_world_readable(keys):
    assert keys[0].stat().st_mode & 0o077 == 0


def test_load_private_key_missing_raises(tmp_path):
    with pytest.raises(signing.SigningError):
        signing.load_private_key(tmp_path / "nope.key")


def test_public_key_from_private_matches_the_published_key(keys, doc):
    priv, pub = keys
    signed = signing.sign_document(doc, key_path=priv)
    assert signing.verify_document(signed, public_key=signing.public_key_from_private(priv))
    assert signing.verify_document(signed, key_path=pub)


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------
def test_sign_then_verify_round_trips(keys, doc):
    priv, pub = keys
    signed = signing.sign_document(doc, key_path=priv)
    assert signing.verify_document(signed, key_path=pub) is True


def test_signing_does_not_mutate_the_original(keys, doc):
    signing.sign_document(doc, key_path=keys[0])
    assert signing.SIGNATURE_FIELD not in doc


def test_tampered_body_fails_verification(keys, doc):
    """The headline requirement: a tampered envelope must not verify."""
    priv, pub = keys
    signed = signing.sign_document(doc, key_path=priv)
    signed["payload"]["body"]["ok"] = False
    assert signing.verify_document(signed, key_path=pub) is False


def test_tampered_id_fails_verification(keys, doc):
    priv, pub = keys
    signed = signing.sign_document(doc, key_path=priv)
    signed["id"] = "task-43"
    assert signing.verify_document(signed, key_path=pub) is False


def test_added_field_fails_verification(keys, doc):
    priv, pub = keys
    signed = signing.sign_document(doc, key_path=priv)
    signed["injected"] = "extra"
    assert signing.verify_document(signed, key_path=pub) is False


def test_removed_field_fails_verification(keys, doc):
    priv, pub = keys
    signed = signing.sign_document(doc, key_path=priv)
    del signed["ts"]
    assert signing.verify_document(signed, key_path=pub) is False


def test_unsigned_document_fails_verification(keys, doc):
    assert signing.verify_document(doc, key_path=keys[1]) is False


def test_garbage_signature_fails_without_raising(keys, doc):
    signed = dict(doc, signature="not base64 !!")
    assert signing.verify_document(signed, key_path=keys[1]) is False


def test_signature_from_another_key_fails(tmp_path, doc):
    a = tmp_path / "a"
    b = tmp_path / "b"
    priv_a, _ = signing.generate_keypair(a)
    _, pub_b = signing.generate_keypair(b)
    signed = signing.sign_document(doc, key_path=priv_a)
    assert signing.verify_document(signed, key_path=pub_b) is False


def test_reserialised_document_still_verifies(keys, doc):
    """Signatures survive a JSON round-trip — that is what canonicalisation buys."""
    priv, pub = keys
    signed = signing.sign_document(doc, key_path=priv)
    reloaded = json.loads(json.dumps(signed, indent=2, ensure_ascii=False))
    assert signing.verify_document(reloaded, key_path=pub) is True


# ---------------------------------------------------------------------------
# Signer (reporter-facing)
# ---------------------------------------------------------------------------
def test_signer_signs_when_a_key_exists(keys, doc):
    signer = signing.Signer(key_path=keys[0])
    assert signer.available
    assert signing.verify_document(signer.sign(doc), key_path=keys[1])


def test_signer_passes_through_without_a_key(tmp_path, doc):
    signer = signing.Signer(key_path=tmp_path / "absent.key")
    assert signer.available is False
    assert signer.sign(doc) == doc


def test_signer_passes_through_when_disabled(keys, doc):
    signer = signing.Signer(key_path=keys[0], enabled=False)
    assert signer.sign(doc) == doc
    assert signing.SIGNATURE_FIELD not in signer.sign(doc)


def test_signer_from_config_reads_the_policy_block(keys):
    class Cfg:
        security = {"signing": {"enabled": False, "private_key_path": str(keys[0])}}

    signer = signing.Signer.from_config(Cfg())
    assert signer.enabled is False
    assert signer.key_path == keys[0]


def test_signer_records_key_use_in_the_audit_sink(keys, doc):
    class Sink:
        def __init__(self):
            self.records = []

        def record(self, **kw):
            self.records.append(kw)

    sink = Sink()
    signing.Signer(key_path=keys[0], audit_sink=sink).sign(doc, context="result:task-42")
    assert [r["action"] for r in sink.records] == ["sign"]
    assert sink.records[0]["resource"] == "result:task-42"


# ---------------------------------------------------------------------------
# python -m agent.verify
# ---------------------------------------------------------------------------
def test_verify_cli_accepts_a_good_document(keys, doc, tmp_path, capsys):
    path = tmp_path / "result.json"
    path.write_text(json.dumps(signing.sign_document(doc, key_path=keys[0])), encoding="utf-8")
    code = verify_cli.main([str(path), "--pubkey", str(keys[1])])
    assert code == verify_cli.EXIT_OK
    assert "OK" in capsys.readouterr().out


def test_verify_cli_rejects_a_tampered_document(keys, doc, tmp_path, capsys):
    signed = signing.sign_document(doc, key_path=keys[0])
    signed["payload"]["body"]["stdout"] = "rm -rf /"
    path = tmp_path / "result.json"
    path.write_text(json.dumps(signed), encoding="utf-8")
    code = verify_cli.main([str(path), "--pubkey", str(keys[1])])
    assert code == verify_cli.EXIT_FAIL
    assert "FAIL" in capsys.readouterr().out


def test_verify_cli_errors_on_a_missing_file(tmp_path, keys):
    code = verify_cli.main([str(tmp_path / "nothing.json"), "--pubkey", str(keys[1])])
    assert code == verify_cli.EXIT_ERROR


def test_verify_cli_errors_on_unparseable_json(tmp_path, keys):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert verify_cli.main([str(path), "--pubkey", str(keys[1])]) == verify_cli.EXIT_ERROR


# ---------------------------------------------------------------------------
# installer.gen_keys
# ---------------------------------------------------------------------------
def test_gen_keys_module_creates_a_usable_keypair(tmp_path, doc):
    proc = subprocess.run(
        [sys.executable, "-m", "installer.gen_keys", "--dir", str(tmp_path), "--no-publish"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    priv = tmp_path / signing.PRIVATE_KEY_NAME
    pub = tmp_path / signing.PUBLIC_KEY_NAME
    assert priv.exists() and pub.exists()
    assert signing.verify_document(signing.sign_document(doc, key_path=priv), key_path=pub)


def test_gen_keys_is_idempotent_without_force(tmp_path):
    args = [sys.executable, "-m", "installer.gen_keys", "--dir", str(tmp_path), "--no-publish"]
    assert subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True).returncode == 0
    second = subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert second.returncode != 0
    assert "already exists" in (second.stdout + second.stderr)


def test_an_all_commented_out_signing_block_is_not_a_crash():
    """`signing:` with every key commented out parses as None, not as absent."""
    from config import Config

    assert signing.Signer.from_config(Config(version=1, owner="test", security={"signing": None})) is not None

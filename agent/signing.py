"""
Cryptographic result signing — Ed25519 over canonical JSON.

Every result the harness writes into the bridge repo is a public artifact: the
repo is private, but anyone with push access (a second agent, a leaked PAT, a
stale CI token) could forge a ``results/<id>.json`` and the consumer would
happily act on it. Signing closes that: the consumer verifies the signature
against a published public key (``docs/agent.pub``) and rejects anything the
harness did not actually produce.

Shape
-----
The signature is a single top-level field on the result document::

    {
      "v": 1, "id": "...", "payload": {...},
      "signature": "<base64 Ed25519 over the canonical form>"
    }

``signature`` is *excluded* from the bytes being signed (otherwise the value
would have to contain itself). ``agent/amp.py`` ignores unknown top-level
fields (see its ``TODO(amp)`` note on ``additionalProperties``), so a signed
envelope still parses as valid AMP v1.0 — and an *unsigned* envelope still
parses too, which is what keeps v1/v2 consumers working.

Canonical form
--------------
``json.dumps(doc, separators=(",", ":"), sort_keys=True, ensure_ascii=False)``
encoded UTF-8. ``sort_keys`` makes the byte stream independent of dict
insertion order, the tight separators remove whitespace ambiguity, and
``ensure_ascii=False`` means Hebrew stays Hebrew instead of turning into
``\\uXXXX`` escapes — a verifier in another language must therefore compare
raw UTF-8, not ASCII-escaped JSON.

Keys
----
``~/.iddo-harness/agent.key``   private, PKCS#8 PEM, mode 0600
``~/.iddo-harness/agent.pub``   public, SubjectPublicKeyInfo PEM

Generate them with ``python -m installer.gen_keys``. Verify a document with
``python -m agent.verify <file.json>``.

Signing is *best-effort by design*: an install that has never run
``gen_keys`` has no private key, and :class:`Signer` then passes documents
through untouched (with one warning) rather than refusing to report results.
Silence on the wire is worse than an unsigned result.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

log = logging.getLogger("harness.signing")

DEFAULT_KEY_DIR = Path.home() / ".iddo-harness"
PRIVATE_KEY_NAME = "agent.key"
PUBLIC_KEY_NAME = "agent.pub"
DEFAULT_PRIVATE_KEY_PATH = DEFAULT_KEY_DIR / PRIVATE_KEY_NAME
DEFAULT_PUBLIC_KEY_PATH = DEFAULT_KEY_DIR / PUBLIC_KEY_NAME

#: Top-level field carrying the base64 signature. Excluded from signed bytes.
SIGNATURE_FIELD = "signature"

#: Algorithm label used in log/audit lines. Not written into the document —
#: the field stays a bare base64 string per the wire contract above.
ALGORITHM = "ed25519"


class SigningError(Exception):
    """Raised on key-loading, signing, or verification failures."""


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------
def canonical_json(doc: dict[str, Any]) -> str:
    """Return the canonical JSON text that gets signed.

    Drops :data:`SIGNATURE_FIELD` so that signing and verifying operate on the
    identical byte stream.
    """
    payload = {k: v for k, v in doc.items() if k != SIGNATURE_FIELD}
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def canonical_bytes(doc: dict[str, Any]) -> bytes:
    """UTF-8 encoding of :func:`canonical_json` — the exact bytes signed."""
    return canonical_json(doc).encode("utf-8")


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------
def _crypto():
    """Import the Ed25519 primitives lazily.

    Keeps ``cryptography`` off the import path of a bare install: modules that
    merely *reference* signing (reporter, audit) stay importable, and the
    absence only surfaces when someone actually asks for a signature.
    """
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as e:  # pragma: no cover - depends on install
        raise SigningError(
            "the 'cryptography' package is required for signing "
            "(pip install 'cryptography>=42') "
        ) from e
    return serialization, ed25519


def generate_keypair(
    key_dir: Path | str | None = None, *, force: bool = False
) -> tuple[Path, Path]:
    """Create a fresh Ed25519 keypair and return ``(private_path, public_path)``.

    Refuses to overwrite an existing private key unless ``force`` — regenerating
    silently would invalidate every signature already published in the bridge
    repo's history.
    """
    serialization, ed25519 = _crypto()
    directory = Path(key_dir) if key_dir else DEFAULT_KEY_DIR
    directory.mkdir(parents=True, exist_ok=True)
    priv_path = directory / PRIVATE_KEY_NAME
    pub_path = directory / PUBLIC_KEY_NAME

    if priv_path.exists() and not force:
        raise SigningError(
            f"{priv_path} already exists — pass force=True to replace it "
            f"(this invalidates every signature published so far)"
        )

    private_key = ed25519.Ed25519PrivateKey.generate()
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    priv_path.write_bytes(priv_pem)
    _harden(priv_path)
    pub_path.write_bytes(pub_pem)
    log.info(f"generated Ed25519 keypair: {priv_path} / {pub_path}")
    return priv_path, pub_path


def _harden(path: Path) -> None:
    """Best-effort 0600 on the private key. No-op where chmod is meaningless."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as e:  # pragma: no cover - Windows / exotic filesystems
        log.warning(f"could not restrict permissions on {path}: {e}")


def load_private_key(path: Path | str | None = None):
    """Load the Ed25519 private key from PEM."""
    serialization, _ = _crypto()
    p = Path(path) if path else DEFAULT_PRIVATE_KEY_PATH
    if not p.exists():
        raise SigningError(f"no private key at {p} — run `python -m installer.gen_keys`")
    key = serialization.load_pem_private_key(p.read_bytes(), password=None)
    return key


def load_public_key(path: Path | str | None = None):
    """Load the Ed25519 public key from PEM."""
    serialization, _ = _crypto()
    p = Path(path) if path else DEFAULT_PUBLIC_KEY_PATH
    if not p.exists():
        raise SigningError(f"no public key at {p}")
    return serialization.load_pem_public_key(p.read_bytes())


def public_key_from_private(path: Path | str | None = None):
    """Derive the public key from the private key on disk.

    Useful for verifying locally without a separate ``agent.pub``.
    """
    return load_private_key(path).public_key()


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------
def sign_document(
    doc: dict[str, Any], *, private_key: Any | None = None, key_path: Path | str | None = None
) -> dict[str, Any]:
    """Return a copy of ``doc`` carrying a fresh :data:`SIGNATURE_FIELD`."""
    key = private_key or load_private_key(key_path)
    signature = key.sign(canonical_bytes(doc))
    signed = dict(doc)
    signed[SIGNATURE_FIELD] = base64.b64encode(signature).decode("ascii")
    return signed


def verify_document(
    doc: dict[str, Any], *, public_key: Any | None = None, key_path: Path | str | None = None
) -> bool:
    """True when ``doc``'s signature matches its canonical form.

    Returns ``False`` — never raises — for a missing, malformed, or simply
    wrong signature, so a verifier can treat every failure mode the same way.
    Key *loading* problems still raise :class:`SigningError`, because those are
    an operator error rather than a verdict on the document.
    """
    raw = doc.get(SIGNATURE_FIELD)
    if not isinstance(raw, str) or not raw:
        log.warning("document carries no signature")
        return False
    try:
        signature = base64.b64decode(raw, validate=True)
    except Exception:
        log.warning("signature is not valid base64")
        return False

    key = public_key or load_public_key(key_path)
    try:
        key.verify(signature, canonical_bytes(doc))
        return True
    except Exception:
        # cryptography raises InvalidSignature; catching broadly keeps this a
        # pure predicate even if the backend changes its exception type.
        return False


# ---------------------------------------------------------------------------
# Reporter-facing helper
# ---------------------------------------------------------------------------
class Signer:
    """Signs outbound documents, degrading to a pass-through when it cannot.

    The reporters call :meth:`sign` on every result and every chunk. Three
    things must not break the harness: no ``cryptography`` installed, no key
    generated yet, and signing explicitly disabled in policy.yaml. All three
    return the document unchanged after a single warning.
    """

    def __init__(
        self,
        key_path: Path | str | None = None,
        enabled: bool = True,
        audit_sink: Any | None = None,
    ) -> None:
        self.key_path = Path(key_path) if key_path else DEFAULT_PRIVATE_KEY_PATH
        self.enabled = enabled
        self._audit_sink = audit_sink
        self._key: Any | None = None
        self._warned = False

    # -----------------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: Any, audit_sink: Any | None = None) -> "Signer":
        """Build a Signer from the ``security.signing`` block of policy.yaml."""
        security = getattr(cfg, "security", None) or {}
        signing = (security.get("signing") or {}) if isinstance(security, dict) else {}
        return cls(
            key_path=signing.get("private_key_path"),
            enabled=bool(signing.get("enabled", True)),
            audit_sink=audit_sink,
        )

    # -----------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """True when signing is switched on and a private key exists."""
        return self.enabled and self.key_path.exists()

    def _load(self) -> Any | None:
        if self._key is None:
            try:
                self._key = load_private_key(self.key_path)
            except SigningError as e:
                self._warn(str(e))
                return None
        return self._key

    def _warn(self, message: str) -> None:
        if not self._warned:
            log.warning(f"results will be published unsigned: {message}")
            self._warned = True

    # -----------------------------------------------------------------------
    def sign(self, doc: dict[str, Any], context: str = "") -> dict[str, Any]:
        """Sign ``doc`` if possible, else return it unchanged."""
        if not self.enabled:
            return doc
        if not self.key_path.exists():
            self._warn(f"no private key at {self.key_path}")
            return doc
        key = self._load()
        if key is None:
            return doc
        try:
            signed = sign_document(doc, private_key=key)
        except Exception as e:  # pragma: no cover - defensive
            self._warn(f"signing failed: {e}")
            return doc
        self._audit(context)
        return signed

    def _audit(self, context: str) -> None:
        """Record the key use, when an audit sink is wired up."""
        if self._audit_sink is None:
            return
        try:
            self._audit_sink.record(
                actor="harness",
                action="sign",
                resource=context or "result",
                outcome="ok",
                meta={"alg": ALGORITHM, "key": str(self.key_path)},
            )
        except Exception:  # pragma: no cover - auditing must never block a push
            log.exception("audit sink failed while recording a signature")

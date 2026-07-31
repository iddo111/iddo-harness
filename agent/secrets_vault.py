"""
Encrypted secrets vault and ``{{secret:name}}`` substitution.

Task packets travel through a git repo and land in ``results/`` forever, so a
command that needs an API key must never *contain* one. Instead it carries a
reference::

    {"kind": "shell", "payload": {
       "command": "curl -H 'X-API-Key: {{secret:openai_key}}' https://api.openai.com/v1/models"
    }}

The executor resolves the placeholder from a locally-encrypted vault the instant
before it spawns the process. The resolved string exists only in the argument
list of that one call: it is never written to the task packet, never logged,
never committed, and any occurrence of the value in the command's own output is
redacted before the result is published.

Storage
-------
``~/.iddo-harness/secrets.enc`` — a single Fernet token over a JSON object.
The Fernet key is derived (HKDF-SHA256) from the Ed25519 private key at
``~/.iddo-harness/agent.key``, so the vault is bound to the same root secret as
result signing: one key to protect, and copying ``secrets.enc`` to another
machine gains the attacker nothing.

Populate it with ``iddo-harness secret set <name>`` — the value is read from a
prompt (``getpass``) or stdin, never from ``argv``, because ``argv`` is visible
to every other process on the box and lands in shell history.

Interaction with the block-list
-------------------------------
``policy.yaml`` blocks any command matching ``*secret*`` / ``*token*`` /
``*password*``, which would reject every vault reference. :func:`mask_text`
rewrites ``{{secret:name}}`` to ``<vault:name>`` before matching, so a *vault
reference* passes while a literal, inline credential is still blocked — which
is exactly the intended pressure: use the vault, don't paste the key.

Module name
-----------
**Deviation from the brief:** the brief names this ``agent/secrets.py``. The
agent is imported with a flat path (``from policy import ...``, with
``agent/`` itself on ``sys.path``), so a module called ``secrets`` would shadow
the standard library's ``secrets`` for the whole process. Named
``secrets_vault`` to avoid that.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable

try:
    from file_security import restrict_private_file
except ImportError:  # pragma: no cover - packaged imports
    from agent.file_security import restrict_private_file

log = logging.getLogger("harness.secrets")

DEFAULT_DIR = Path.home() / ".iddo-harness"
DEFAULT_STORE_PATH = DEFAULT_DIR / "secrets.enc"
DEFAULT_KEY_PATH = DEFAULT_DIR / "agent.key"

#: ``{{secret:name}}`` — names are deliberately restrictive so a placeholder
#: can never smuggle shell metacharacters into the resolved command.
PLACEHOLDER_RE = re.compile(r"\{\{secret:([A-Za-z0-9_.\-]{1,64})\}\}")

#: What a masked placeholder becomes in logs and audit lines.
MASK_TEMPLATE = "<vault:{name}>"

#: What a leaked *value* becomes when found in output.
REDACTED = "***REDACTED***"

_HKDF_INFO = b"iddo-harness/secrets/v1"
_MIN_REDACT_LEN = 4


class VaultError(Exception):
    """Base class for vault failures."""


class VaultUnavailable(VaultError):
    """The vault cannot be opened (no root key, or ``cryptography`` missing)."""


class MissingSecretError(VaultError):
    """A task referenced a ``{{secret:name}}`` that is not in the vault."""


# ---------------------------------------------------------------------------
# Process-wide value registry, for defence-in-depth redaction
# ---------------------------------------------------------------------------
_known_values: set[str] = set()
_known_lock = threading.Lock()


def register_value(value: str) -> None:
    """Remember a resolved secret so any log path can redact it.

    The executor already redacts the values *it* resolved; this registry covers
    the paths that never learn which secret was involved — a stack trace, a
    subprocess echoing its own argv, a metrics label. Values shorter than
    :data:`_MIN_REDACT_LEN` are ignored: blanket-replacing a 2-character string
    would shred unrelated output for no security gain.
    """
    if value and len(value) >= _MIN_REDACT_LEN:
        with _known_lock:
            _known_values.add(value)


def clear_registry() -> None:
    """Forget every registered value (used by tests)."""
    with _known_lock:
        _known_values.clear()


def redact(text: str, values: Iterable[str] = ()) -> str:
    """Replace ``values`` — plus every registered value — with :data:`REDACTED`."""
    if not text:
        return text
    with _known_lock:
        candidates = set(_known_values)
    candidates.update(v for v in values if v and len(v) >= _MIN_REDACT_LEN)
    for value in sorted(candidates, key=len, reverse=True):
        if value in text:
            text = text.replace(value, REDACTED)
    return text


def mask_text(text: str) -> str:
    """Make ``text`` safe to log: mask placeholders, redact known values."""
    if not text:
        return text
    masked = PLACEHOLDER_RE.sub(lambda m: MASK_TEMPLATE.format(name=m.group(1)), text)
    return redact(masked)


def referenced_names(text: str) -> list[str]:
    """Return the secret names ``text`` refers to, in order of first appearance."""
    seen: list[str] = []
    for match in PLACEHOLDER_RE.finditer(text or ""):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def has_placeholder(text: str) -> bool:
    """True when ``text`` contains at least one ``{{secret:...}}`` reference."""
    return bool(PLACEHOLDER_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------
class SecretVault:
    """Fernet-encrypted name/value store keyed off the agent's Ed25519 key."""

    def __init__(
        self,
        store_path: Path | str | None = None,
        key_path: Path | str | None = None,
        audit_sink: Any | None = None,
    ) -> None:
        self.store_path = Path(store_path) if store_path else DEFAULT_STORE_PATH
        self.key_path = Path(key_path) if key_path else DEFAULT_KEY_PATH
        self._audit_sink = audit_sink
        self._fernet: Any | None = None
        self._lock = threading.Lock()

    # -----------------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: Any, audit_sink: Any | None = None) -> "SecretVault":
        """Build from the ``security.secrets`` block of policy.yaml."""
        security = getattr(cfg, "security", None) or {}
        # `or {}` rather than a default: a block whose keys are all commented
        # out parses as None, not as a missing key.
        secrets_cfg = (security.get("secrets") or {}) if isinstance(security, dict) else {}
        return cls(
            store_path=secrets_cfg.get("store_path"),
            key_path=secrets_cfg.get("key_path"),
            audit_sink=audit_sink,
        )

    # -----------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """True when the root key exists, i.e. the vault can be opened at all."""
        return self.key_path.exists()

    def _cipher(self) -> Any:
        """Return the Fernet cipher, deriving its key from agent.key on first use."""
        if self._fernet is not None:
            return self._fernet
        try:
            from cryptography.fernet import Fernet  # noqa: PLC0415
            from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: PLC0415
            from cryptography.hazmat.primitives.serialization import (  # noqa: PLC0415
                Encoding, NoEncryption, PrivateFormat, load_pem_private_key,
            )
        except ImportError as e:  # pragma: no cover - depends on install
            raise VaultUnavailable(
                "the 'cryptography' package is required for the secrets vault "
                "(pip install 'cryptography>=42')"
            ) from e

        if not self.key_path.exists():
            raise VaultUnavailable(
                f"no root key at {self.key_path} — run `python -m installer.gen_keys` first"
            )

        private_key = load_pem_private_key(self.key_path.read_bytes(), password=None)
        try:
            root = private_key.private_bytes_raw()
        except AttributeError:  # pragma: no cover - non-Ed25519 key on disk
            root = private_key.private_bytes(
                encoding=Encoding.Raw, format=PrivateFormat.Raw,
                encryption_algorithm=NoEncryption(),
            )
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(root)
        import base64  # noqa: PLC0415

        self._fernet = Fernet(base64.urlsafe_b64encode(derived))
        return self._fernet

    # -----------------------------------------------------------------------
    def _load(self) -> dict[str, str]:
        """Decrypt and return the whole store, or ``{}`` when there is none yet."""
        if not self.store_path.exists():
            return {}
        blob = self.store_path.read_bytes().strip()
        if not blob:
            return {}
        try:
            plain = self._cipher().decrypt(blob)
        except VaultUnavailable:
            raise
        except Exception as e:
            raise VaultError(
                f"cannot decrypt {self.store_path} — was agent.key regenerated? ({e})"
            ) from e
        data = json.loads(plain.decode("utf-8"))
        return {str(k): str(v) for k, v in data.items()}

    def _save(self, data: dict[str, str]) -> None:
        """Encrypt and write the whole store atomically, 0600."""
        blob = self._cipher().encrypt(
            json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
        tmp.write_bytes(blob)
        restrict_private_file(tmp)
        os.replace(tmp, self.store_path)
        restrict_private_file(self.store_path)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------
    def set(self, name: str, value: str) -> None:
        """Store (or replace) one secret."""
        self._check_name(name)
        with self._lock:
            data = self._load()
            data[name] = value
            self._save(data)
        self._audit("secret_set", name, "ok")
        log.info(f"secret {name!r} stored in {self.store_path}")

    def get(self, name: str) -> str:
        """Return one secret's value, auditing the access."""
        self._check_name(name)
        with self._lock:
            data = self._load()
        if name not in data:
            self._audit("secret_get", name, "deny", {"reason": "not found"})
            raise MissingSecretError(f"no secret named {name!r} in {self.store_path}")
        self._audit("secret_get", name, "ok")
        register_value(data[name])
        return data[name]

    def delete(self, name: str) -> bool:
        """Remove one secret. Returns False when it was not there."""
        self._check_name(name)
        with self._lock:
            data = self._load()
            if name not in data:
                return False
            del data[name]
            self._save(data)
        self._audit("secret_delete", name, "ok")
        return True

    def names(self) -> list[str]:
        """Return the stored secret names (never the values), sorted."""
        with self._lock:
            return sorted(self._load())

    # -----------------------------------------------------------------------
    def resolve(self, text: str) -> tuple[str, dict[str, str]]:
        """Substitute every ``{{secret:name}}`` in ``text``.

        Returns ``(resolved_text, {name: value})``. The mapping lets the caller
        redact those exact values out of the command's output. Raises
        :class:`MissingSecretError` if a referenced name is absent — running the
        command with a literal ``{{secret:x}}`` in it would send garbage to a
        remote API and could log the placeholder into a third-party system.
        """
        names = referenced_names(text)
        if not names:
            return text, {}
        used = {name: self.get(name) for name in names}
        resolved = PLACEHOLDER_RE.sub(lambda m: used[m.group(1)], text)
        return resolved, used

    def resolve_structure(self, value: Any) -> tuple[Any, dict[str, str]]:
        """Recursively resolve placeholders inside strings, dicts, and lists.

        Used for ``http_local`` headers and bodies, where the credential sits in
        a nested field rather than in a command line.
        """
        used: dict[str, str] = {}
        if isinstance(value, str):
            resolved, u = self.resolve(value)
            used.update(u)
            return resolved, used
        if isinstance(value, dict):
            out_d = {}
            for k, v in value.items():
                rv, u = self.resolve_structure(v)
                used.update(u)
                out_d[k] = rv
            return out_d, used
        if isinstance(value, list):
            out_l = []
            for v in value:
                rv, u = self.resolve_structure(v)
                used.update(u)
                out_l.append(rv)
            return out_l, used
        return value, used

    # -----------------------------------------------------------------------
    @staticmethod
    def _check_name(name: str) -> None:
        """Reject names that could not appear in a placeholder anyway."""
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", name or ""):
            raise VaultError(
                f"invalid secret name {name!r} — use 1-64 chars of [A-Za-z0-9_.-]"
            )

    def _audit(self, action: str, name: str, outcome: str, meta: dict[str, Any] | None = None) -> None:
        """Record a vault operation. The *name* is audited; the value never is."""
        if self._audit_sink is None:
            return
        try:
            self._audit_sink.record(
                actor="harness", action=action, resource=f"secret:{name}",
                outcome=outcome, meta=meta or {},
            )
        except Exception:  # pragma: no cover - auditing must not block the vault
            log.exception("audit sink failed while recording a vault operation")


# ---------------------------------------------------------------------------
def resolve_with(vault: SecretVault | None, text: str) -> tuple[str, dict[str, str]]:
    """Resolve placeholders when a vault is configured, else pass through.

    Lets callers stay ignorant of whether the vault exists on this machine:
    a task with no placeholders behaves identically either way, and a task that
    *does* reference a secret fails loudly rather than silently shipping
    ``{{secret:x}}`` to a remote endpoint.
    """
    if not has_placeholder(text):
        return text, {}
    if vault is None or not vault.available:
        raise VaultUnavailable(
            "task references {{secret:...}} but no vault is available — "
            "run `python -m installer.gen_keys` then `iddo-harness secret set <name>`"
        )
    return vault.resolve(text)

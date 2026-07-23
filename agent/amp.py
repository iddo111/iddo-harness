"""
AMP (Almaware Protocol) v1.0 envelope module.

Implements the core AMP envelope shape (spec §2) for the Iddo Harness
`harness` channel, scoped to the two payload types the harness actually
speaks:

  - ``harness_task``   (direction: "inbound"  — a command sent TO the harness)
  - ``harness_result`` (direction: "outbound" — a result sent FROM the harness)

This module intentionally implements a *subset* of the full AMP v1.0
schema — only what Iddo Harness needs as a conformant brick on the
``harness`` channel. Fields/behaviors from the full spec that are not yet
wired up are marked with ``# TODO(amp):`` per the AMP v1.0 §7 rule that a
brick MUST NOT claim conformance it hasn't earned.

Reference: Almaware Protocol v1.0, §2 (envelope), §6.1 (identity
normalization), §2.3.2 (reply routing).
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Constants — AMP v1.0 core vocabulary as used by Iddo Harness
# ---------------------------------------------------------------------------

AMP_MAJOR_VERSION = 1

VALID_DIRECTIONS = {"inbound", "outbound"}

# Iddo Harness only speaks two payload types today (docs/amp_alignment.md).
# TODO(amp): full AMP v1.0 also defines "text", "command", "sensor", "event",
# "state" payload types (§2.4). Harness does not need them yet — add as
# new bricks/channels require.
VALID_PAYLOAD_TYPES = {"harness_task", "harness_result"}

# §6.1 / §6.1a — core + harness-relevant identity canonical prefixes.
# Format is always "<prefix>:<address>", enforced by _CANONICAL_RE below.
# TODO(amp): full registry lives at registry/channels.json per §6.1a; this
# module only whitelists the prefixes Iddo Harness is documented to use
# (docs/amp_alignment.md, Handout v2) plus the AMP-core ones.
VALID_IDENTITY_PREFIXES = {"user", "wa", "mqtt", "brick", "session"}

# ISO-8601 UTC, millisecond precision, trailing Z — §2.3 `ts` field.
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

# identity.canonical / reply.to_address — "<prefix>:<address>", prefix
# restricted to VALID_IDENTITY_PREFIXES for Iddo Harness (spec's own pattern
# is the more permissive `^[a-z0-9_]+:.+$`, §2.5).
_CANONICAL_RE = re.compile(r"^([a-z0-9_]+):(.+)$")

# id — ULID or UUIDv4 lowercase, per §2.5. Iddo Harness also accepts the
# legacy free-form task ids already in use by the bridge repo (e.g.
# "20260723-001-scan-dclaude") for backward compatibility with non-AMP
# task packets; that acceptance happens in poller.py, not here — this
# module enforces the strict AMP `id` pattern for genuine AMP envelopes.
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class AmpValidationError(ValueError):
    """Raised when a dict does not validate as a well-formed AMP envelope."""


# ---------------------------------------------------------------------------
# Sub-objects
# ---------------------------------------------------------------------------

@dataclass
class Source:
    brick: str
    instance: str

    def to_dict(self) -> dict:
        return {"brick": self.brick, "instance": self.instance}


@dataclass
class Identity:
    self_: bool  # "self" is a Python keyword; exposed as identity.self on the wire
    canonical: str
    display_name: str | None = None
    provisional: bool = False
    raw: dict | None = None

    def to_dict(self) -> dict:
        out: dict = {"self": self.self_, "canonical": self.canonical}
        if self.display_name is not None:
            out["display_name"] = self.display_name
        if self.provisional:
            out["provisional"] = self.provisional
        if self.raw is not None:
            out["raw"] = self.raw
        return out


@dataclass
class Payload:
    type: str
    body: dict

    def to_dict(self) -> dict:
        return {"type": self.type, "body": self.body}


@dataclass
class Reply:
    to_channel: str
    to_address: str
    reply_to_id: str | None = None

    def to_dict(self) -> dict:
        out = {"to_channel": self.to_channel, "to_address": self.to_address}
        if self.reply_to_id is not None:
            out["reply_to_id"] = self.reply_to_id
        return out


@dataclass
class RateLimit:
    """
    Optional rate-limit annotation. AMP v1.0 §6.3 defines rate limiting as a
    protocol-level concept keyed on (channel, to_address), enforced by a
    shared limiter — NOT independently by each producer.

    TODO(amp): Iddo Harness does not yet enforce §6.3 rate limits (no
    shared SQLite counter, no ALMAWARE_MIN_INTERVAL_S / MAX_PER_HOUR
    checks). This dataclass only lets an envelope *carry* rate-limit
    metadata (e.g. from an upstream brick) so it round-trips losslessly;
    it is not consulted by poller/executor/reporter yet.
    """
    max_per_hour: int | None = None
    min_interval_s: int | None = None

    def to_dict(self) -> dict:
        out = {}
        if self.max_per_hour is not None:
            out["max_per_hour"] = self.max_per_hour
        if self.min_interval_s is not None:
            out["min_interval_s"] = self.min_interval_s
        return out


@dataclass
class Audit:
    """
    Optional audit annotation carried on the envelope itself.

    TODO(amp): AMP v1.0 expects a brick-side JSONL audit log (per the
    Handout's "AMP conformance" checklist and docs/amp_alignment.md's
    conformance checklist item "audit log ב-JSONL מלא"). Iddo Harness
    already writes a plaintext audit.log via agent/policy.py
    (`PolicyEngine.audit`) and agent/main.py's logging handler, but not
    JSONL, and this field is not yet populated by poller/reporter. This
    dataclass exists so an envelope can carry/round-trip audit metadata
    (e.g. `decision`, `reason`) without losing it, ahead of full JSONL
    audit-log wiring.
    """
    decision: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        out = {}
        if self.decision is not None:
            out["decision"] = self.decision
        if self.reason is not None:
            out["reason"] = self.reason
        return out


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------

@dataclass
class AmpEnvelope:
    v: int
    id: str
    ts: str
    direction: str
    source: Source
    channel: str
    identity: Identity
    payload: Payload
    reply: Reply
    rate_limit: RateLimit | None = None
    audit: Audit | None = None

    # -----------------------------------------------------------------
    def to_dict(self) -> dict:
        out = {
            "v": self.v,
            "id": self.id,
            "ts": self.ts,
            "direction": self.direction,
            "source": self.source.to_dict(),
            "channel": self.channel,
            "identity": self.identity.to_dict(),
            "payload": self.payload.to_dict(),
            "reply": self.reply.to_dict(),
        }
        if self.rate_limit is not None:
            out["rate_limit"] = self.rate_limit.to_dict()
        if self.audit is not None:
            out["audit"] = self.audit.to_dict()
        return out

    # Convenience passthroughs used by callers that only care about the
    # inner task/result body, regardless of direction.
    @property
    def body(self) -> dict:
        return self.payload.body


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d:
        raise AmpValidationError(f"{ctx}: missing required field '{key}'")
    return d[key]


def _require_type(value: Any, types, ctx: str, field_name: str):
    if not isinstance(value, types):
        want = types.__name__ if not isinstance(types, tuple) else " | ".join(t.__name__ for t in types)
        raise AmpValidationError(
            f"{ctx}: field '{field_name}' must be of type {want}, got {type(value).__name__}"
        )


def _validate_canonical(value: Any, ctx: str, field_name: str) -> str:
    _require_type(value, str, ctx, field_name)
    m = _CANONICAL_RE.match(value)
    if not m:
        raise AmpValidationError(
            f"{ctx}: field '{field_name}' = {value!r} is not a valid identity "
            f"string; expected format '<prefix>:<address>'"
        )
    prefix = m.group(1)
    if prefix not in VALID_IDENTITY_PREFIXES:
        raise AmpValidationError(
            f"{ctx}: field '{field_name}' has unknown identity prefix '{prefix}:'; "
            f"expected one of {sorted(VALID_IDENTITY_PREFIXES)}"
        )
    return value


def _parse_source(data: Any, ctx: str) -> Source:
    _require_type(data, dict, ctx, "source")
    brick = _require(data, "brick", f"{ctx}.source")
    instance = _require(data, "instance", f"{ctx}.source")
    _require_type(brick, str, f"{ctx}.source", "brick")
    _require_type(instance, str, f"{ctx}.source", "instance")
    return Source(brick=brick, instance=instance)


def _parse_identity(data: Any, ctx: str) -> Identity:
    _require_type(data, dict, ctx, "identity")
    self_val = _require(data, "self", f"{ctx}.identity")
    _require_type(self_val, bool, f"{ctx}.identity", "self")
    canonical = _require(data, "canonical", f"{ctx}.identity")
    _validate_canonical(canonical, f"{ctx}.identity", "canonical")
    display_name = data.get("display_name")
    if display_name is not None:
        _require_type(display_name, str, f"{ctx}.identity", "display_name")
    provisional = data.get("provisional", False)
    _require_type(provisional, bool, f"{ctx}.identity", "provisional")
    raw = data.get("raw")
    if raw is not None:
        _require_type(raw, dict, f"{ctx}.identity", "raw")
    return Identity(
        self_=self_val,
        canonical=canonical,
        display_name=display_name,
        provisional=provisional,
        raw=raw,
    )


def _parse_payload(data: Any, ctx: str) -> Payload:
    _require_type(data, dict, ctx, "payload")
    ptype = _require(data, "type", f"{ctx}.payload")
    _require_type(ptype, str, f"{ctx}.payload", "type")
    if ptype not in VALID_PAYLOAD_TYPES:
        raise AmpValidationError(
            f"{ctx}.payload: unknown payload.type '{ptype}'; "
            f"Iddo Harness only accepts {sorted(VALID_PAYLOAD_TYPES)}"
        )
    body = _require(data, "body", f"{ctx}.payload")
    _require_type(body, dict, f"{ctx}.payload", "body")

    if ptype == "harness_task":
        _require(body, "kind", f"{ctx}.payload.body")
        _require_type(body["kind"], str, f"{ctx}.payload.body", "kind")
    elif ptype == "harness_result":
        _require(body, "task_id", f"{ctx}.payload.body")
        _require(body, "ok", f"{ctx}.payload.body")
        _require_type(body["ok"], bool, f"{ctx}.payload.body", "ok")

    return Payload(type=ptype, body=body)


def _parse_reply(data: Any, ctx: str) -> Reply:
    _require_type(data, dict, ctx, "reply")
    to_channel = _require(data, "to_channel", f"{ctx}.reply")
    _require_type(to_channel, str, f"{ctx}.reply", "to_channel")
    to_address = _require(data, "to_address", f"{ctx}.reply")
    _validate_canonical(to_address, f"{ctx}.reply", "to_address")
    reply_to_id = data.get("reply_to_id")
    if reply_to_id is not None:
        _require_type(reply_to_id, str, f"{ctx}.reply", "reply_to_id")
    return Reply(to_channel=to_channel, to_address=to_address, reply_to_id=reply_to_id)


def _parse_rate_limit(data: Any, ctx: str) -> RateLimit | None:
    if data is None:
        return None
    _require_type(data, dict, ctx, "rate_limit")
    max_per_hour = data.get("max_per_hour")
    min_interval_s = data.get("min_interval_s")
    if max_per_hour is not None:
        _require_type(max_per_hour, int, f"{ctx}.rate_limit", "max_per_hour")
    if min_interval_s is not None:
        _require_type(min_interval_s, int, f"{ctx}.rate_limit", "min_interval_s")
    return RateLimit(max_per_hour=max_per_hour, min_interval_s=min_interval_s)


def _parse_audit(data: Any, ctx: str) -> Audit | None:
    if data is None:
        return None
    _require_type(data, dict, ctx, "audit")
    decision = data.get("decision")
    reason = data.get("reason")
    if decision is not None:
        _require_type(decision, str, f"{ctx}.audit", "decision")
    if reason is not None:
        _require_type(reason, str, f"{ctx}.audit", "reason")
    return Audit(decision=decision, reason=reason)


def _validate_id(value: Any, ctx: str):
    _require_type(value, str, ctx, "id")
    if not (_ULID_RE.match(value) or _UUID4_RE.match(value.lower())):
        raise AmpValidationError(
            f"{ctx}: field 'id' = {value!r} is not a valid ULID or UUIDv4"
        )


def _validate_ts(value: Any, ctx: str):
    _require_type(value, str, ctx, "ts")
    if not _TS_RE.match(value):
        raise AmpValidationError(
            f"{ctx}: field 'ts' = {value!r} must be ISO-8601 UTC with millisecond "
            f"precision and trailing 'Z', e.g. '2026-07-23T07:45:00.000Z'"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_envelope(data: dict, *, id_strict: bool = True) -> AmpEnvelope:
    """
    Strictly validate ``data`` as an AMP v1.0 envelope and return an
    :class:`AmpEnvelope`.

    Raises :class:`AmpValidationError` with a clear, field-scoped message on
    any violation. This function validates the subset of AMP v1.0 §2 that
    Iddo Harness needs (see module docstring) — it is deliberately stricter
    than the full spec in some places (closed `payload.type` union, closed
    `identity` prefix set) and does not yet implement some optional §2.3
    fields (`minor`, `external_id`, `thread`, `attachments`, `ext`, `raw`).

    :param id_strict: if False, skip the ULID/UUIDv4 pattern check on `id`
        (used by callers that need to accept legacy free-form task ids while
        still validating everything else about the envelope).
    """
    ctx = "envelope"
    if not isinstance(data, dict):
        raise AmpValidationError(f"{ctx}: expected a JSON object, got {type(data).__name__}")

    v = _require(data, "v", ctx)
    _require_type(v, int, ctx, "v")
    if v != AMP_MAJOR_VERSION:
        raise AmpValidationError(
            f"{ctx}: unrecognized major version v={v!r}; this module only "
            f"understands v={AMP_MAJOR_VERSION} (AMP v1.0 §2.6 requires "
            f"consumers reject unrecognized major versions)"
        )

    env_id = _require(data, "id", ctx)
    if id_strict:
        _validate_id(env_id, ctx)
    else:
        _require_type(env_id, str, ctx, "id")

    ts = _require(data, "ts", ctx)
    _validate_ts(ts, ctx)

    direction = _require(data, "direction", ctx)
    _require_type(direction, str, ctx, "direction")
    if direction not in VALID_DIRECTIONS:
        raise AmpValidationError(
            f"{ctx}: field 'direction' = {direction!r} must be one of {sorted(VALID_DIRECTIONS)}"
        )

    source = _parse_source(_require(data, "source", ctx), ctx)

    channel = _require(data, "channel", ctx)
    _require_type(channel, str, ctx, "channel")
    if not channel:
        raise AmpValidationError(f"{ctx}: field 'channel' must be non-empty")

    identity = _parse_identity(_require(data, "identity", ctx), ctx)
    payload = _parse_payload(_require(data, "payload", ctx), ctx)

    # reply is conditionally required per §2.3.2: MUST be present when
    # direction == outbound, channel != "control", and payload.type is
    # recipient-directed. Iddo Harness's two payload types (harness_task,
    # harness_result) are always recipient-directed on its one channel
    # ("harness"), so this module treats `reply` as always-required,
    # matching docs/amp_alignment.md's example envelopes.
    reply = _parse_reply(_require(data, "reply", ctx), ctx)

    # Cross-field check: direction/payload.type coherence for Iddo Harness.
    if direction == "inbound" and payload.type != "harness_task":
        raise AmpValidationError(
            f"{ctx}: direction='inbound' requires payload.type='harness_task', "
            f"got {payload.type!r}"
        )
    if direction == "outbound" and payload.type != "harness_result":
        raise AmpValidationError(
            f"{ctx}: direction='outbound' requires payload.type='harness_result', "
            f"got {payload.type!r}"
        )

    rate_limit = _parse_rate_limit(data.get("rate_limit"), ctx)
    audit = _parse_audit(data.get("audit"), ctx)

    # TODO(amp): reject unknown top-level fields to match the spec's
    # `additionalProperties: false` (§2.5). Currently unknown extra keys
    # are silently ignored rather than rejected, to stay lenient while the
    # bridge repo's producers (Perplexity/Claude/GPT/Gemini) stabilize on
    # the exact shape.

    return AmpEnvelope(
        v=v,
        id=env_id,
        ts=ts,
        direction=direction,
        source=source,
        channel=channel,
        identity=identity,
        payload=payload,
        reply=reply,
        rate_limit=rate_limit,
        audit=audit,
    )


def build_envelope(
    *,
    direction: str,
    source_brick: str,
    source_instance: str,
    channel: str,
    identity_canonical: str,
    payload_type: str,
    payload_body: dict,
    to_channel: str,
    to_address: str,
    identity_self: bool = True,
    identity_display_name: str | None = None,
    env_id: str | None = None,
    ts: str | None = None,
    reply_to_id: str | None = None,
    rate_limit: RateLimit | None = None,
    audit: Audit | None = None,
) -> AmpEnvelope:
    """
    Build a new outbound-or-inbound :class:`AmpEnvelope` programmatically.

    Used by ``reporter.py`` to build ``harness_result`` envelopes. Also
    usable by any future producer that wants to emit a ``harness_task``
    envelope without hand-assembling a dict.

    ``env_id`` defaults to a fresh UUIDv4 (lowercase); ``ts`` defaults to
    "now" in AMP's required millisecond-precision UTC format.
    """
    if direction not in VALID_DIRECTIONS:
        raise AmpValidationError(f"build_envelope: direction must be one of {sorted(VALID_DIRECTIONS)}")
    if payload_type not in VALID_PAYLOAD_TYPES:
        raise AmpValidationError(f"build_envelope: payload_type must be one of {sorted(VALID_PAYLOAD_TYPES)}")

    env_id = env_id or str(uuid.uuid4())
    ts = ts or _now_ts()

    envelope_dict = {
        "v": AMP_MAJOR_VERSION,
        "id": env_id,
        "ts": ts,
        "direction": direction,
        "source": {"brick": source_brick, "instance": source_instance},
        "channel": channel,
        "identity": {
            "self": identity_self,
            "canonical": identity_canonical,
            **({"display_name": identity_display_name} if identity_display_name else {}),
        },
        "payload": {"type": payload_type, "body": payload_body},
        "reply": {
            "to_channel": to_channel,
            "to_address": to_address,
            **({"reply_to_id": reply_to_id} if reply_to_id else {}),
        },
    }
    if rate_limit is not None:
        envelope_dict["rate_limit"] = rate_limit.to_dict()
    if audit is not None:
        envelope_dict["audit"] = audit.to_dict()

    # Round-trip through parse_envelope so build_envelope can never produce
    # an envelope that parse_envelope itself would reject. UUIDv4 ids are
    # always id_strict-valid, so no relaxation needed here.
    return parse_envelope(envelope_dict)


def serialize(env: AmpEnvelope) -> dict:
    """Return the plain-dict wire representation of an AmpEnvelope."""
    return env.to_dict()


def to_json(env: AmpEnvelope, *, indent: int | None = None) -> str:
    """JSON-serialize an envelope. Mirrors AMP's own JSON-round-trip requirement."""
    return json.dumps(env.to_dict(), ensure_ascii=False, indent=indent)


def _now_ts() -> str:
    """Current UTC time formatted per AMP §2.3 `ts` pattern."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Legacy (non-AMP) task compatibility
# ---------------------------------------------------------------------------

def is_amp_shaped(data: dict) -> bool:
    """
    Heuristic used by poller.py to decide whether an incoming task-packet
    JSON dict looks like an AMP envelope (has the AMP top-level shape) vs.
    a legacy free-form task packet (top-level `kind`, per
    docs/task_packet_spec.md).

    Per the task spec: legacy packets have a top-level `kind` field and no
    `v`/`payload` envelope wrapper. AMP envelopes have `v` + `payload`.
    """
    if not isinstance(data, dict):
        return False
    return "v" in data and "payload" in data and "kind" not in data

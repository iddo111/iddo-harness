"""
Tests for agent/amp.py — the AMP (Almaware Protocol) v1.0 envelope module.

Covers: valid envelope round-trip, missing required fields, bad identity
format, wrong direction (payload.type/direction mismatch), and unknown
payload type. See docs/amp_envelope_examples.md for the example envelopes
these fixtures are modeled on.
"""
import copy
import json

import pytest

import amp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def inbound_task_envelope() -> dict:
    """A valid inbound harness_task envelope (shell command)."""
    return {
        "v": 1,
        "id": "01J8Z3K9RXTG3V6P6M8AH1QF7C",
        "ts": "2026-07-23T07:45:00.000Z",
        "direction": "inbound",
        "source": {"brick": "chatgpt-codex", "instance": "session-abc123"},
        "channel": "harness",
        "identity": {
            "self": True,
            "canonical": "user:iddo111",
            "display_name": "Iddo",
        },
        "payload": {
            "type": "harness_task",
            "body": {
                "kind": "shell",
                "command": "echo hello",
                "paths": [],
                "timeout_sec": 60,
            },
        },
        "reply": {"to_channel": "harness", "to_address": "session:abc123"},
    }


@pytest.fixture
def outbound_result_envelope() -> dict:
    """A valid outbound harness_result envelope."""
    return {
        "v": 1,
        "id": "8f14e45f-ceea-4e46-9c92-1234567890ab",
        "ts": "2026-07-23T07:46:00.000Z",
        "direction": "outbound",
        "source": {"brick": "iddo-harness", "instance": "agent-01"},
        "channel": "harness",
        "identity": {"self": False, "canonical": "brick:iddo-harness"},
        "payload": {
            "type": "harness_result",
            "body": {
                "task_id": "01J8Z3K9RXTG3V6P6M8AH1QF7C",
                "ok": True,
                "decision": "auto",
                "stdout": "hello\n",
                "exit_code": 0,
            },
        },
        "reply": {
            "to_channel": "harness",
            "to_address": "session:abc123",
            "reply_to_id": "01J8Z3K9RXTG3V6P6M8AH1QF7C",
        },
    }


# ---------------------------------------------------------------------------
# 1. Valid envelope round-trip
# ---------------------------------------------------------------------------

class TestValidRoundTrip:
    def test_inbound_task_parses(self, inbound_task_envelope):
        env = amp.parse_envelope(inbound_task_envelope)
        assert env.v == 1
        assert env.direction == "inbound"
        assert env.payload.type == "harness_task"
        assert env.payload.body["command"] == "echo hello"
        assert env.identity.canonical == "user:iddo111"
        assert env.reply.to_address == "session:abc123"

    def test_outbound_result_parses(self, outbound_result_envelope):
        env = amp.parse_envelope(outbound_result_envelope)
        assert env.direction == "outbound"
        assert env.payload.type == "harness_result"
        assert env.payload.body["ok"] is True

    def test_to_dict_matches_input_shape(self, inbound_task_envelope):
        env = amp.parse_envelope(inbound_task_envelope)
        out = env.to_dict()
        assert out["v"] == inbound_task_envelope["v"]
        assert out["id"] == inbound_task_envelope["id"]
        assert out["source"] == inbound_task_envelope["source"]
        assert out["payload"] == inbound_task_envelope["payload"]
        assert out["reply"] == inbound_task_envelope["reply"]

    def test_json_round_trip_is_lossless(self, outbound_result_envelope):
        env = amp.parse_envelope(outbound_result_envelope)
        serialized = json.dumps(env.to_dict(), ensure_ascii=False)
        reparsed_dict = json.loads(serialized)
        env2 = amp.parse_envelope(reparsed_dict)
        assert env2.to_dict() == env.to_dict()

    def test_serialize_helper_matches_to_dict(self, inbound_task_envelope):
        env = amp.parse_envelope(inbound_task_envelope)
        assert amp.serialize(env) == env.to_dict()

    def test_to_json_helper_round_trips(self, inbound_task_envelope):
        env = amp.parse_envelope(inbound_task_envelope)
        s = amp.to_json(env)
        assert json.loads(s) == env.to_dict()

    def test_build_envelope_produces_valid_result(self):
        env = amp.build_envelope(
            direction="outbound",
            source_brick="iddo-harness",
            source_instance="agent-01",
            channel="harness",
            identity_canonical="brick:iddo-harness",
            identity_self=False,
            payload_type="harness_result",
            payload_body={"task_id": "abc", "ok": True},
            to_channel="harness",
            to_address="session:abc123",
            reply_to_id="abc",
        )
        # build_envelope round-trips through parse_envelope internally, so
        # this alone proves the built envelope is schema-valid.
        assert env.direction == "outbound"
        assert env.payload.type == "harness_result"
        assert env.reply.reply_to_id == "abc"
        # Confirm it also survives a second independent parse.
        again = amp.parse_envelope(env.to_dict())
        assert again.to_dict() == env.to_dict()


# ---------------------------------------------------------------------------
# 2. Missing required fields
# ---------------------------------------------------------------------------

class TestMissingRequiredFields:
    @pytest.mark.parametrize(
        "field_to_remove",
        ["v", "id", "ts", "direction", "source", "channel", "identity", "payload", "reply"],
    )
    def test_missing_top_level_field_raises(self, inbound_task_envelope, field_to_remove):
        bad = copy.deepcopy(inbound_task_envelope)
        del bad[field_to_remove]
        with pytest.raises(amp.AmpValidationError, match=field_to_remove):
            amp.parse_envelope(bad)

    def test_missing_source_brick_raises(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        del bad["source"]["brick"]
        with pytest.raises(amp.AmpValidationError, match="brick"):
            amp.parse_envelope(bad)

    def test_missing_identity_canonical_raises(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        del bad["identity"]["canonical"]
        with pytest.raises(amp.AmpValidationError, match="canonical"):
            amp.parse_envelope(bad)

    def test_missing_payload_body_raises(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        del bad["payload"]["body"]
        with pytest.raises(amp.AmpValidationError, match="body"):
            amp.parse_envelope(bad)

    def test_missing_reply_to_address_raises(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        del bad["reply"]["to_address"]
        with pytest.raises(amp.AmpValidationError, match="to_address"):
            amp.parse_envelope(bad)

    def test_harness_task_missing_kind_raises(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        del bad["payload"]["body"]["kind"]
        with pytest.raises(amp.AmpValidationError, match="kind"):
            amp.parse_envelope(bad)

    def test_harness_result_missing_ok_raises(self, outbound_result_envelope):
        bad = copy.deepcopy(outbound_result_envelope)
        del bad["payload"]["body"]["ok"]
        with pytest.raises(amp.AmpValidationError, match="ok"):
            amp.parse_envelope(bad)


# ---------------------------------------------------------------------------
# 3. Bad identity format
# ---------------------------------------------------------------------------

class TestBadIdentityFormat:
    def test_identity_canonical_without_prefix_raises(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        bad["identity"]["canonical"] = "iddo111"  # no "prefix:" at all
        with pytest.raises(amp.AmpValidationError, match="not a valid identity"):
            amp.parse_envelope(bad)

    def test_identity_canonical_unknown_prefix_raises(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        bad["identity"]["canonical"] = "telegram:12345"  # not a recognized prefix
        with pytest.raises(amp.AmpValidationError, match="unknown identity prefix"):
            amp.parse_envelope(bad)

    def test_identity_self_wrong_type_raises(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        bad["identity"]["self"] = "yes"  # must be bool
        with pytest.raises(amp.AmpValidationError, match="self"):
            amp.parse_envelope(bad)

    def test_reply_to_address_bad_format_raises(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        bad["reply"]["to_address"] = "not-canonical-shaped"
        with pytest.raises(amp.AmpValidationError, match="not a valid identity"):
            amp.parse_envelope(bad)

    @pytest.mark.parametrize(
        "prefix",
        ["user", "wa", "mqtt", "brick", "session"],
    )
    def test_all_documented_prefixes_accepted(self, inbound_task_envelope, prefix):
        good = copy.deepcopy(inbound_task_envelope)
        good["identity"]["canonical"] = f"{prefix}:some-address"
        env = amp.parse_envelope(good)
        assert env.identity.canonical == f"{prefix}:some-address"


# ---------------------------------------------------------------------------
# 4. Wrong direction
# ---------------------------------------------------------------------------

class TestWrongDirection:
    def test_invalid_direction_value_raises(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        bad["direction"] = "sideways"
        with pytest.raises(amp.AmpValidationError, match="direction"):
            amp.parse_envelope(bad)

    def test_inbound_with_harness_result_payload_raises(self, inbound_task_envelope, outbound_result_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        bad["payload"] = outbound_result_envelope["payload"]
        with pytest.raises(amp.AmpValidationError, match="direction='inbound'"):
            amp.parse_envelope(bad)

    def test_outbound_with_harness_task_payload_raises(self, outbound_result_envelope, inbound_task_envelope):
        bad = copy.deepcopy(outbound_result_envelope)
        bad["payload"] = inbound_task_envelope["payload"]
        with pytest.raises(amp.AmpValidationError, match="direction='outbound'"):
            amp.parse_envelope(bad)


# ---------------------------------------------------------------------------
# 5. Unknown payload type
# ---------------------------------------------------------------------------

class TestUnknownPayloadType:
    def test_unknown_payload_type_raises(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        bad["payload"]["type"] = "text"  # valid in full AMP spec, not supported by harness
        with pytest.raises(amp.AmpValidationError, match="unknown payload.type"):
            amp.parse_envelope(bad)

    def test_completely_made_up_payload_type_raises(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        bad["payload"]["type"] = "made_up_type"
        with pytest.raises(amp.AmpValidationError, match="unknown payload.type"):
            amp.parse_envelope(bad)


# ---------------------------------------------------------------------------
# Extra coverage: major version rejection, malformed ts, is_amp_shaped
# ---------------------------------------------------------------------------

class TestMiscValidation:
    def test_unrecognized_major_version_rejected(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        bad["v"] = 2
        with pytest.raises(amp.AmpValidationError, match="unrecognized major version"):
            amp.parse_envelope(bad)

    def test_malformed_timestamp_rejected(self, inbound_task_envelope):
        bad = copy.deepcopy(inbound_task_envelope)
        bad["ts"] = "2026-07-23T07:45:00Z"  # missing millisecond precision
        with pytest.raises(amp.AmpValidationError, match="ts"):
            amp.parse_envelope(bad)

    def test_non_dict_input_rejected(self):
        with pytest.raises(amp.AmpValidationError, match="expected a JSON object"):
            amp.parse_envelope(["not", "a", "dict"])

    def test_is_amp_shaped_true_for_envelope(self, inbound_task_envelope):
        assert amp.is_amp_shaped(inbound_task_envelope) is True

    def test_is_amp_shaped_false_for_legacy_packet(self):
        legacy = {"id": "20260723-001-scan", "kind": "shell", "payload": {"command": "ls"}}
        assert amp.is_amp_shaped(legacy) is False

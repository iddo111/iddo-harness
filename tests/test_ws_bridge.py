"""
Tests for the WebSocket transport.

Most of these drive a real server over a real loopback socket, because the
things worth proving here are transport-level: that an unauthenticated caller
gets nowhere, that chunks arrive in order as they are produced, and that a
packet means the same thing over a socket as it does in the bridge repo.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import pytest

import amp
from executor import Result
from metrics import Metrics
from ws_bridge import (
    CLOSE_UNAUTHORIZED,
    DEFAULT_PORT,
    TASK_PATH,
    WsBridge,
    build_execute,
    extract_token,
    task_from_json,
    token_matches,
    websockets_available,
)

websockets = pytest.importorskip("websockets", reason="ws transport is an optional extra")
from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402

TOKEN = "test-token-abcdef"


# -- token handling ---------------------------------------------------------
def test_bare_token_matches():
    assert token_matches(TOKEN, TOKEN)


def test_bearer_prefix_is_tolerated():
    assert token_matches(f"Bearer {TOKEN}", TOKEN)
    assert token_matches(f"bearer  {TOKEN} ", TOKEN)


def test_wrong_token_is_rejected():
    assert not token_matches("Bearer nope", TOKEN)


def test_missing_token_is_rejected():
    assert not token_matches(None, TOKEN)
    assert not token_matches("", TOKEN)


def test_no_expected_token_never_matches():
    """A bridge with an empty token must not accept an empty credential."""
    assert not token_matches("", "")
    assert not token_matches("anything", "")


def test_bridge_refuses_to_exist_without_a_token():
    with pytest.raises(ValueError):
        WsBridge(execute=lambda task, emit: None, token="")


def test_extract_token_reads_authorization():
    assert extract_token({"Authorization": f"Bearer {TOKEN}"}) == f"Bearer {TOKEN}"


def test_extract_token_accepts_the_subprotocol_form():
    """Browsers cannot set handshake headers, so the subprotocol is the way in."""
    headers = {"Sec-WebSocket-Protocol": f"bearer.{TOKEN}"}
    assert extract_token(headers) == TOKEN


def test_extract_token_returns_none_when_absent():
    assert extract_token({}) is None
    assert extract_token(None) is None


# -- packet parsing ---------------------------------------------------------
def test_legacy_packet_is_lifted():
    task = task_from_json({"id": "t1", "kind": "read_file", "payload": {"path": "/tmp/x"}})
    assert task.id == "t1"
    assert task.kind == "read_file"
    assert task.payload == {"path": "/tmp/x"}
    assert task.envelope is None


def test_an_id_is_minted_when_the_producer_omits_one():
    task = task_from_json({"kind": "shell", "payload": {"command": "true"}})
    assert task.id.startswith("ws-")


def test_a_packet_without_a_kind_is_rejected():
    with pytest.raises(ValueError):
        task_from_json({"payload": {}})


def test_amp_packet_keeps_its_envelope_for_the_reply_address():
    envelope = amp.build_envelope(
        direction="inbound",
        source_brick="perplexity-computer",
        source_instance="test",
        channel="github",
        identity_canonical="brick:perplexity-computer",
        identity_self=False,
        payload_type="harness_task",
        payload_body={"kind": "shell", "command": "echo hi"},
        to_channel="github",
        to_address="brick:iddo-harness",
    )
    task = task_from_json(amp.serialize(envelope))
    assert task.kind == "shell"
    assert task.envelope is not None
    assert task.payload["command"] == "echo hi"


def test_socket_packets_parse_exactly_like_bridge_packets():
    """Same parser as the git bridge — a socket must not be a second dialect."""
    from poller import task_from_packet

    doc = {"id": "t9", "kind": "list_dir", "payload": {"path": "/tmp"}}
    over_socket = task_from_json(doc)
    in_repo = task_from_packet(dict(doc), None)
    assert (over_socket.id, over_socket.kind, over_socket.payload) == (
        in_repo.id,
        in_repo.kind,
        in_repo.payload,
    )


# -- build_execute ----------------------------------------------------------
def FakeResult(ok=True, metadata=None):
    """A real :class:`executor.Result` — the dataclass the reporters expect."""
    return Result(task_id="t1", ok=ok, decision="auto", metadata=metadata or {})


class StubExecutor:
    """Emits chunks through whatever ``chunk_sink`` is installed on it."""

    def __init__(self, chunks=(), result=None):
        self.chunk_sink = None
        self._chunks = list(chunks)
        self._result = result if result is not None else FakeResult()
        self.ran = []

    def run(self, task):
        self.ran.append(task)
        for seq, body in enumerate(self._chunks):
            self.chunk_sink(task, body, seq, False)
        return self._result


def test_build_execute_emits_a_final_frame_for_non_streaming_kinds():
    emitted = []
    execute = build_execute(StubExecutor())
    execute(task_from_json({"id": "t1", "kind": "read_file"}), lambda b, s, f: emitted.append((b, s, f)))
    assert len(emitted) == 1
    assert emitted[0][2] is True


def test_build_execute_does_not_double_up_a_streamed_final():
    """Streaming kinds close their own stream; two finals breaks the contract."""
    executor = StubExecutor(
        chunks=[{"stdout": "a"}],
        result=FakeResult(metadata={"final_chunk": {"ok": True}}),
    )
    emitted = []
    build_execute(executor)(
        task_from_json({"id": "t1", "kind": "shell"}), lambda b, s, f: emitted.append((b, s, f))
    )
    assert [f for _b, _s, f in emitted].count(True) == 0


def test_build_execute_restores_the_previous_sink():
    executor = StubExecutor()
    executor.chunk_sink = sentinel = object()
    build_execute(executor)(task_from_json({"id": "t1", "kind": "shell"}), lambda b, s, f: None)
    assert executor.chunk_sink is sentinel


def test_build_execute_also_writes_to_the_bridge_repo():
    """A socket task still leaves the same audit trail in git."""
    from tests.helpers import StubReporter

    reporter = StubReporter()
    executor = StubExecutor(chunks=[{"stdout": "a"}, {"stdout": "b"}])
    build_execute(executor, reporter)(
        task_from_json({"id": "t1", "kind": "shell"}), lambda b, s, f: None
    )
    assert len(reporter.chunks) == 3  # two streamed, one final


# -- live server ------------------------------------------------------------
@pytest.fixture
def echo_bridge():
    """A bridge whose executor emits two chunks then finishes."""
    metrics = Metrics(enabled=True)

    def execute(task, emit):
        emit({"stdout": "one"}, 0, False)
        emit({"stdout": "two"}, 1, False)
        emit({"ok": True, "decision": "auto", "kind": task.kind}, 2, True)

    bridge = WsBridge(execute=execute, token=TOKEN, port=0, metrics=metrics)
    bridge.start()
    try:
        yield bridge
    finally:
        bridge.stop()


def run(coro):
    return asyncio.run(coro)


def auth_headers(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_default_port_matches_the_brief():
    assert DEFAULT_PORT == 8477


def test_websockets_is_available_in_this_environment():
    assert websockets_available() is True


def test_port_zero_binds_a_real_port(echo_bridge):
    assert echo_bridge.bound_port > 0
    assert echo_bridge.url.endswith(TASK_PATH)


def test_a_task_streams_its_chunks_back(echo_bridge):
    async def scenario():
        async with connect(echo_bridge.url, additional_headers=auth_headers()) as ws:
            await ws.send(json.dumps({"id": "t1", "kind": "shell", "payload": {"command": "x"}}))
            return [json.loads(await ws.recv()) for _ in range(3)]

    frames = run(scenario())
    assert [f["stdout"] for f in frames[:2]] == ["one", "two"]
    assert frames[-1]["is_final"] is True
    assert frames[-1]["ok"] is True


def test_chunk_sequence_is_gapless_and_carries_the_task_id(echo_bridge):
    async def scenario():
        async with connect(echo_bridge.url, additional_headers=auth_headers()) as ws:
            await ws.send(json.dumps({"id": "seq-test", "kind": "shell"}))
            return [json.loads(await ws.recv()) for _ in range(3)]

    frames = run(scenario())
    assert [f["seq"] for f in frames] == [0, 1, 2]
    assert {f["task_id"] for f in frames} == {"seq-test"}
    assert [f["is_final"] for f in frames] == [False, False, True]


def test_two_tasks_on_one_connection(echo_bridge):
    async def scenario():
        async with connect(echo_bridge.url, additional_headers=auth_headers()) as ws:
            got = []
            for task_id in ("a", "b"):
                await ws.send(json.dumps({"id": task_id, "kind": "shell"}))
                for _ in range(3):
                    got.append(json.loads(await ws.recv()))
            return got

    frames = run(scenario())
    assert {f["task_id"] for f in frames} == {"a", "b"}


def test_the_subprotocol_credential_also_works(echo_bridge):
    async def scenario():
        async with connect(echo_bridge.url, subprotocols=[f"bearer.{TOKEN}"]) as ws:
            await ws.send(json.dumps({"id": "t1", "kind": "shell"}))
            return json.loads(await ws.recv())

    assert run(scenario())["stdout"] == "one"


# -- auth over the wire -----------------------------------------------------
def test_no_token_gets_an_immediate_401(echo_bridge):
    """The brief is explicit: no token, 401, before any packet is read."""

    async def scenario():
        async with connect(echo_bridge.url):
            pass

    with pytest.raises(InvalidStatus) as excinfo:
        run(scenario())
    assert excinfo.value.response.status_code == 401


def test_a_wrong_token_gets_a_401(echo_bridge):
    async def scenario():
        async with connect(echo_bridge.url, additional_headers=auth_headers("wrong")):
            pass

    with pytest.raises(InvalidStatus) as excinfo:
        run(scenario())
    assert excinfo.value.response.status_code == 401


def test_a_rejected_handshake_never_runs_a_task():
    ran = []

    def execute(task, emit):
        ran.append(task)

    bridge = WsBridge(execute=execute, token=TOKEN, port=0)
    bridge.start()
    try:

        async def scenario():
            async with connect(bridge.url):
                pass

        with pytest.raises(InvalidStatus):
            run(scenario())
        assert ran == []
    finally:
        bridge.stop()


def test_rejections_are_counted(echo_bridge):
    async def scenario():
        async with connect(echo_bridge.url):
            pass

    with pytest.raises(InvalidStatus):
        run(scenario())
    assert echo_bridge.metrics.snapshot()["ws_rejected"] >= 1


def test_accepted_connections_are_counted(echo_bridge):
    async def scenario():
        async with connect(echo_bridge.url, additional_headers=auth_headers()) as ws:
            await ws.send(json.dumps({"id": "t1", "kind": "shell"}))
            await ws.recv()

    run(scenario())
    assert echo_bridge.metrics.snapshot()["ws_connections"] >= 1


def test_an_unauthenticated_plain_http_probe_gets_401(echo_bridge):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            f"http://127.0.0.1:{echo_bridge.bound_port}{TASK_PATH}", timeout=5
        )
    assert excinfo.value.code == 401


def test_an_unknown_path_is_refused(echo_bridge):
    async def scenario():
        url = f"ws://127.0.0.1:{echo_bridge.bound_port}/nope"
        async with connect(url, additional_headers=auth_headers()):
            pass

    with pytest.raises(InvalidStatus) as excinfo:
        run(scenario())
    assert excinfo.value.response.status_code == 404


# -- bad input --------------------------------------------------------------
def test_malformed_json_gets_an_error_frame_not_a_dropped_socket(echo_bridge):
    async def scenario():
        async with connect(echo_bridge.url, additional_headers=auth_headers()) as ws:
            await ws.send("{not json")
            first = json.loads(await ws.recv())
            # The connection must survive so the consumer can retry.
            await ws.send(json.dumps({"id": "ok", "kind": "shell"}))
            second = json.loads(await ws.recv())
            return first, second

    bad, good = run(scenario())
    assert bad["ok"] is False and "bad packet" in bad["error"]
    assert good["stdout"] == "one"


def test_a_json_array_is_not_a_task_packet(echo_bridge):
    async def scenario():
        async with connect(echo_bridge.url, additional_headers=auth_headers()) as ws:
            await ws.send(json.dumps([1, 2, 3]))
            return json.loads(await ws.recv())

    assert run(scenario())["ok"] is False


def test_a_packet_missing_its_kind_gets_an_error_frame(echo_bridge):
    async def scenario():
        async with connect(echo_bridge.url, additional_headers=auth_headers()) as ws:
            await ws.send(json.dumps({"id": "t1", "payload": {}}))
            return json.loads(await ws.recv())

    assert run(scenario())["ok"] is False


def test_an_executor_crash_does_not_take_the_bridge_down():
    def execute(task, emit):
        raise RuntimeError("executor exploded")

    bridge = WsBridge(execute=execute, token=TOKEN, port=0)
    bridge.start()
    try:

        async def scenario():
            async with connect(bridge.url, additional_headers=auth_headers()) as ws:
                await ws.send(json.dumps({"id": "boom", "kind": "shell"}))
                with pytest.raises(ConnectionClosed):
                    await ws.recv()
            # A fresh connection still works.
            async with connect(bridge.url, additional_headers=auth_headers()):
                return True

        assert run(scenario()) is True
    finally:
        bridge.stop()


# -- lifecycle --------------------------------------------------------------
def test_start_is_idempotent(echo_bridge):
    port = echo_bridge.bound_port
    echo_bridge.start()
    assert echo_bridge.bound_port == port


def test_stop_closes_the_listener():
    bridge = WsBridge(execute=lambda t, e: None, token=TOKEN, port=0)
    bridge.start()
    url = bridge.url
    bridge.stop()

    async def scenario():
        async with connect(url, additional_headers=auth_headers(), open_timeout=2):
            pass

    with pytest.raises(OSError):
        run(scenario())


def test_stop_is_safe_before_start():
    WsBridge(execute=lambda t, e: None, token=TOKEN, port=0).stop()


def test_bound_port_falls_back_to_the_configured_port():
    assert WsBridge(execute=lambda t, e: None, token=TOKEN, port=1234).bound_port == 1234

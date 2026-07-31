import json
from types import SimpleNamespace

import httpx
import pytest

from perplexity_memory import PerplexityMemory
from perplexity_worker import PerplexityApiClient, PerplexityWorker, PerplexityWorkerError


class FakeExecutor:
    def __init__(self, decision="auto"):
        self.tasks = []
        self.decision = decision

    def run(self, task):
        self.tasks.append(task)
        return SimpleNamespace(
            task_id=task.id, ok=self.decision == "auto", decision=self.decision,
            stdout="ok", stderr="", exit_code=0 if self.decision == "auto" else None,
            error="owner approval required" if self.decision == "confirm_required" else "", metadata={},
        )


def _client(monkeypatch, handler, **kwargs):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-secret-never-log")
    return PerplexityApiClient(transport=httpx.MockTransport(handler), backoff_base=0, **kwargs)


def test_api_key_is_environment_only(monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    with pytest.raises(PerplexityWorkerError, match="environment variable"):
        PerplexityApiClient()


def test_tool_call_routes_through_injected_executor(monkeypatch, tmp_path):
    replies = iter([
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "shell", "arguments": json.dumps({"command": "dir"})},
        }]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "finished"}}]},
    ])

    def handler(request):
        assert request.headers["authorization"] == "Bearer test-secret-never-log"
        if request.url.path == "/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=next(replies))

    executor = FakeExecutor()
    worker = PerplexityWorker(
        client=_client(monkeypatch, handler), executor=executor,
        memory=PerplexityMemory(tmp_path / "memory.jsonl"), project="harness",
    )
    result = worker.run("inspect safely")
    assert result.done and result.final_message == "finished"
    assert len(executor.tasks) == 1
    assert executor.tasks[0].payload["agent_id"] == "perplexity"
    assert executor.tasks[0].payload["transport"] == "local-perplexity-api"
    assert executor.tasks[0].authenticated_agent_id == "perplexity"
    assert executor.tasks[0].transport == "local-perplexity-api"
    assert executor.tasks[0].kind == "shell"
    persisted = (tmp_path / "memory.jsonl").read_text(encoding="utf-8")
    assert "inspect safely" not in persisted
    assert "finished" not in persisted
    assert "prompt_sha256" in persisted


def test_confirmation_pauses_without_another_model_call(monkeypatch, tmp_path):
    calls = {"completion": 0}

    def handler(request):
        if request.url.path == "/models":
            return httpx.Response(200, json={})
        calls["completion"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "tool_calls": [{
            "id": "call_confirm", "type": "function",
            "function": {"name": "shell", "arguments": "{\"command\":\"danger\"}"},
        }]}}]})

    worker = PerplexityWorker(
        client=_client(monkeypatch, handler), executor=FakeExecutor("confirm_required"),
        memory=PerplexityMemory(tmp_path / "memory.jsonl"), project="harness",
    )
    result = worker.run("do task")
    assert result.paused and not result.done
    assert result.pending_confirmation["reason"] == "owner approval required"
    assert calls["completion"] == 1


def test_health_threshold_and_recovery(monkeypatch, tmp_path):
    state = {"ok": False}

    def handler(request):
        return httpx.Response(200 if state["ok"] else 503, json={})

    worker = PerplexityWorker(
        client=_client(monkeypatch, handler), executor=FakeExecutor(),
        memory=PerplexityMemory(tmp_path / "m.jsonl"), project="p",
        failure_threshold=3, probe_interval=0,
    )
    assert worker.handshake(force=True)["status"] == "degraded"
    assert worker.handshake(force=True)["status"] == "degraded"
    assert worker.handshake(force=True)["status"] == "harness_unreachable"
    state["ok"] = True
    recovered = worker.handshake(force=True)
    assert recovered["status"] == "ready"
    assert recovered["consecutive_failures"] == 0


def test_network_retries_are_bounded(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(request.url.path)
        raise httpx.ConnectError("offline", request=request)

    client = _client(monkeypatch, handler, max_retries=3)
    assert client.health() is False
    assert len(attempts) == 3


def test_handshake_respects_probe_interval(monkeypatch, tmp_path):
    probes = []
    now = [100.0]

    def handler(request):
        probes.append(request.url.path)
        return httpx.Response(200, json={})

    worker = PerplexityWorker(
        client=_client(monkeypatch, handler), executor=FakeExecutor(),
        memory=PerplexityMemory(tmp_path / "m.jsonl"), project="p",
        probe_interval=60, clock=lambda: now[0],
    )
    assert worker.handshake()["status"] == "ready"
    now[0] = 130.0
    assert worker.handshake()["status"] == "ready"
    assert len(probes) == 1
    now[0] = 161.0
    worker.handshake()
    assert len(probes) == 2

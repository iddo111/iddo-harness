"""
Tests for the v3 agent-native executor (agent/executor_v3.py) and its router hook.

Covers all 14 v3 kinds through ``ExecutorV3`` plus the ``Executor.run()``
dispatch, and asserts the two backward-compatibility properties Track C has to
hold: v1 and v2 kinds still route where they always did, and a v3 kind reaches
the policy engine on the same terms.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from executor import Executor, Result
from executor_v2 import V2_KINDS
from executor_v3 import V3_KINDS, ExecutorV3, _UNGATED_KINDS, default_schedule_path
from memory import MemoryStore
from policy import Decision, PolicyEngine
from scheduler import Scheduler

IS_WINDOWS = sys.platform.startswith("win")


@dataclass
class FakeTask:
    """Minimal stand-in for agent.poller.Task."""

    id: str
    kind: str
    payload: dict = field(default_factory=dict)
    envelope: object | None = None


def make_cfg(tmp_path: Path, **overrides) -> SimpleNamespace:
    cfg = SimpleNamespace(
        version=1,
        owner="tester",
        auto_allow={
            "commands": ["echo*", "printf*", "true", "false", "memory_*", "schedule*",
                         "spawn_task", "await_tasks", "workflow", "handshake"],
            "paths": {"read": [f"{tmp_path}/**"]},
        },
        require_confirm={"commands": [], "paths": {"write": [f"{tmp_path}/**"]}},
        block={"commands": ["rm -rf /*"], "paths": {"absolute_no_touch": ["**/.env"]}},
        polling={},
        paths={},
        transport={},
        confirm={"timeout_minutes": 30},
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class RecordingConfirm:
    """ConfirmManager stub that records parked tasks instead of notifying."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []

    def create(self, task, reason):
        self.created.append((task.id, reason))
        return SimpleNamespace(message=f"approve? {reason}")


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect ``Path.home()`` for every test in this module.

    The memory store and the schedule registry both default to a path under
    the home directory, so without this a test run would read and write the
    real one — and leak state into the next run.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


@pytest.fixture()
def policy(tmp_path: Path) -> PolicyEngine:
    return PolicyEngine(make_cfg(tmp_path))


@pytest.fixture()
def parent(policy: PolicyEngine) -> Executor:
    """A real Executor whose v3 state lives entirely under tmp_path."""
    executor = Executor(policy, RecordingConfirm())
    yield executor
    if executor._v3 is not None:
        executor._v3.shutdown()


@pytest.fixture()
def v3(parent: Executor) -> ExecutorV3:
    return parent.v3


def run(executor, kind: str, payload: dict | None = None, task_id: str = "t1") -> Result:
    return executor.run(FakeTask(id=task_id, kind=kind, payload=payload or {}))


# ---------------------------------------------------------------------------
# Router hook
# ---------------------------------------------------------------------------
def test_fourteen_v3_kinds_are_declared() -> None:
    assert len(V3_KINDS) == 14


def test_v3_kinds_do_not_collide_with_v1_or_v2() -> None:
    v1 = {"shell", "read_file", "write_file", "list_dir"}
    assert not (V3_KINDS & V2_KINDS)
    assert not (V3_KINDS & v1)


def test_the_router_recognises_every_v3_kind() -> None:
    assert all(Executor._is_v3_kind(kind) for kind in V3_KINDS)
    assert Executor._is_v3_kind("shell") is False


def test_the_v3_executor_is_cached(parent: Executor) -> None:
    """Live schedules, memory and sub-tasks must survive a polling cycle."""
    assert parent.v3 is parent.v3


def test_the_v3_executor_is_wired_to_its_parent(parent: Executor) -> None:
    assert parent.v3.parent_executor is parent


def test_v1_still_routes_to_v1(parent: Executor) -> None:
    result = run(parent, "read_file", {"path": "/definitely/missing"})
    assert result.decision != "unknown_kind"
    assert parent._v3 is None  # v1 must not even build the v3 executor


def test_an_unknown_kind_is_still_unknown(parent: Executor) -> None:
    assert run(parent, "teleport").decision == "unknown_kind"


def test_v3_kinds_reach_the_executor_through_the_router(parent: Executor) -> None:
    result = run(parent, "template_list")
    assert result.ok is True
    assert result.metadata["count"] == 5


def test_handles_matches_the_kind_set(v3: ExecutorV3) -> None:
    assert v3.handles("workflow") is True
    assert v3.handles("shell") is False


def test_an_unhandled_kind_is_reported(v3: ExecutorV3) -> None:
    assert run(v3, "not_a_kind").decision == "unknown_kind"


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell command")
def test_a_v3_workflow_can_run_v1_and_v2_children(parent: Executor, tmp_path: Path) -> None:
    """A child re-enters at the router, so any generation of kind is reachable."""
    (tmp_path / "a.txt").write_text("hello")
    result = run(
        parent,
        "workflow",
        {
            "nodes": [
                {"id": "v1", "kind": "shell", "payload": {"command": "echo one"}},
                {"id": "v2", "kind": "glob", "payload": {"path": str(tmp_path), "pattern": "*.txt"}},
            ],
            "max_parallel": 2,
        },
    )
    assert result.ok is True
    assert result.metadata["counts"]["succeeded"] == 2


# ---------------------------------------------------------------------------
# Memory kinds
# ---------------------------------------------------------------------------
def test_memory_set_then_get(v3: ExecutorV3) -> None:
    assert run(v3, "memory_set", {"key": "pm", "value": "pnpm"}).ok is True
    result = run(v3, "memory_get", {"key": "pm"})
    assert result.metadata["found"] is True
    assert result.metadata["value"] == "pnpm"


def test_a_memory_miss_is_a_real_answer_not_a_failure(v3: ExecutorV3) -> None:
    """ok=false would make every first-run lookup look broken."""
    result = run(v3, "memory_get", {"key": "never-stored"})
    assert result.ok is True
    assert result.metadata["found"] is False
    assert result.metadata["value"] is None


def test_memory_set_without_a_value_is_a_bad_request(v3: ExecutorV3) -> None:
    result = run(v3, "memory_set", {"key": "k"})
    assert result.decision == "bad_request"
    assert "value" in result.error


def test_memory_set_rejects_an_oversized_value(v3: ExecutorV3) -> None:
    result = run(v3, "memory_set", {"key": "k", "value": "x" * 2_000_000})
    assert result.decision == "bad_request"


def test_memory_set_carries_ttl_and_tags(v3: ExecutorV3) -> None:
    result = run(v3, "memory_set", {"key": "k", "value": 1, "ttl_seconds": 60, "tags": ["ci"]})
    record = result.metadata["record"]
    assert record["tags"] == ["ci"]
    assert record["expires_at"] is not None


def test_memory_list_filters_and_reports_namespaces(v3: ExecutorV3) -> None:
    run(v3, "memory_set", {"key": "build:a", "value": 1})
    run(v3, "memory_set", {"key": "other", "value": 2})
    run(v3, "memory_set", {"key": "x", "value": 3, "namespace": "side"})

    result = run(v3, "memory_list", {"prefix": "build:"})
    assert result.metadata["count"] == 1
    assert "side" in result.metadata["namespaces"]


def test_memory_list_can_span_every_namespace(v3: ExecutorV3) -> None:
    run(v3, "memory_set", {"key": "a", "value": 1})
    run(v3, "memory_set", {"key": "b", "value": 2, "namespace": "side"})
    assert run(v3, "memory_list", {"namespace": "*"}).metadata["count"] == 2


def test_memory_list_can_omit_the_values(v3: ExecutorV3) -> None:
    run(v3, "memory_set", {"key": "k", "value": "secret"})
    result = run(v3, "memory_list", {"include_values": False})
    assert "value" not in result.metadata["records"][0]


def test_memory_delete_reports_whether_it_existed(v3: ExecutorV3) -> None:
    run(v3, "memory_set", {"key": "k", "value": 1})
    assert run(v3, "memory_delete", {"key": "k"}).metadata["deleted"] is True
    assert run(v3, "memory_delete", {"key": "k"}).metadata["deleted"] is False


def test_memory_delete_can_clear_a_namespace(v3: ExecutorV3) -> None:
    run(v3, "memory_set", {"key": "a", "value": 1, "namespace": "scratch"})
    run(v3, "memory_set", {"key": "b", "value": 2, "namespace": "scratch"})
    result = run(v3, "memory_delete", {"namespace": "scratch", "clear_namespace": True})
    assert result.metadata["cleared"] == 2


def test_memory_delete_needs_a_key(v3: ExecutorV3) -> None:
    result = run(v3, "memory_delete", {})
    assert result.decision == "bad_request"
    assert "clear_namespace" in result.error


def test_memory_persists_under_the_home_directory(tmp_path: Path, policy: PolicyEngine) -> None:
    store = MemoryStore(tmp_path / "m.db")
    executor = ExecutorV3(policy, memory=store)
    run(executor, "memory_set", {"key": "k", "value": "v"})
    executor.shutdown()

    reopened = MemoryStore(tmp_path / "m.db")
    assert reopened.get("k") == "v"
    reopened.close()


# ---------------------------------------------------------------------------
# Sub-task kinds
# ---------------------------------------------------------------------------
@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell command")
def test_spawn_task_returns_ids_immediately(parent: Executor) -> None:
    result = run(parent, "spawn_task", {"tasks": [{"kind": "shell", "command": "echo hi"}]},
                 task_id="fan")
    assert result.ok is True
    assert result.metadata["task_ids"] == ["fan-sub-0"]
    assert run(parent, "await_tasks", {"task_ids": ["fan-sub-0"], "timeout_sec": 10}).ok is True


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell command")
def test_spawn_task_can_wait_inline(parent: Executor) -> None:
    result = run(
        parent, "spawn_task",
        {"tasks": [{"kind": "shell", "command": "echo hi"}], "await": True, "timeout_sec": 10},
        task_id="fan",
    )
    assert result.ok is True
    assert result.metadata["awaited"]["ok"] is True


def test_spawn_task_accepts_a_single_task(parent: Executor) -> None:
    result = run(parent, "spawn_task", {"task": {"kind": "list_dir", "path": "."}}, task_id="one")
    assert result.metadata["task_ids"] == ["one-sub-0"]


def test_spawn_task_needs_something_to_spawn(parent: Executor) -> None:
    result = run(parent, "spawn_task", {})
    assert result.decision == "bad_request"
    assert "'tasks' list" in result.error


def test_await_of_an_unknown_id_is_reported(parent: Executor) -> None:
    result = run(parent, "await_tasks", {"task_ids": ["ghost"], "timeout_sec": 1})
    assert result.ok is False
    assert result.metadata["unknown"] == ["ghost"]


def test_await_accepts_a_bare_string(parent: Executor) -> None:
    assert run(parent, "await_tasks", {"task_ids": "ghost", "timeout_sec": 1}).metadata["unknown"] == ["ghost"]


def test_await_with_no_ids_is_trivially_ok(parent: Executor) -> None:
    assert run(parent, "await_tasks", {}).ok is True


def test_await_rejects_a_non_list(parent: Executor) -> None:
    result = run(parent, "await_tasks", {"task_ids": {"a": 1}})
    assert result.decision == "bad_request"


def test_a_child_cannot_run_without_a_parent_executor(policy: PolicyEngine) -> None:
    """Spawning is fire-and-forget, so the wiring error lands on the child."""
    executor = ExecutorV3(policy)  # deliberately unwired
    spawned = run(executor, "spawn_task", {"tasks": [{"kind": "shell", "command": "echo hi"}]})
    awaited = run(
        executor, "await_tasks", {"task_ids": spawned.metadata["task_ids"]}, task_id="t2"
    )
    executor.shutdown()
    assert awaited.ok is False
    assert "no parent executor" in repr(awaited.metadata)


# ---------------------------------------------------------------------------
# Workflow kind
# ---------------------------------------------------------------------------
@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell command")
def test_workflow_runs_a_chain_and_passes_output_downstream(parent: Executor) -> None:
    result = run(
        parent,
        "workflow",
        {
            "nodes": [
                {"id": "version", "kind": "shell", "payload": {"command": "echo 1.2.3"}},
                {"id": "use", "kind": "shell", "depends_on": ["version"],
                 "payload": {"command": "echo tag=${nodes.version.stdout}"}},
            ]
        },
    )
    assert result.ok is True
    use = next(n for n in result.metadata["nodes"] if n["id"] == "use")
    assert "tag=1.2.3" in use["stdout"]


def test_a_malformed_workflow_is_a_bad_request(parent: Executor) -> None:
    result = run(parent, "workflow", {"nodes": []})
    assert result.decision == "bad_request"
    assert "non-empty" in result.error


def test_a_cyclic_workflow_is_rejected_before_running(parent: Executor) -> None:
    result = run(
        parent,
        "workflow",
        {"nodes": [
            {"id": "a", "kind": "shell", "depends_on": ["b"]},
            {"id": "b", "kind": "shell", "depends_on": ["a"]},
        ]},
    )
    assert result.decision == "bad_request"
    assert "cycle" in result.error


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell command")
def test_workflow_streams_a_chunk_per_node_and_one_final(policy: PolicyEngine) -> None:
    chunks: list[dict] = []
    parent = Executor(policy, RecordingConfirm(),
                      chunk_sink=lambda task, body, seq, final: chunks.append(body))
    run(parent, "workflow", {"nodes": [{"id": "a", "kind": "shell",
                                        "payload": {"command": "echo hi"}}]})
    parent.v3.shutdown()

    assert [c["seq"] for c in chunks] == list(range(len(chunks)))
    assert sum(1 for c in chunks if c["is_final"]) == 1
    assert chunks[-1]["is_final"] is True


def test_a_failing_chunk_sink_does_not_abort_the_work(policy: PolicyEngine) -> None:
    def broken_sink(task, body, seq, final):
        raise RuntimeError("transport down")

    parent = Executor(policy, RecordingConfirm(), chunk_sink=broken_sink)
    result = run(parent, "workflow", {"nodes": [{"id": "a", "kind": "list_dir",
                                                 "payload": {"path": "."}}]})
    parent.v3.shutdown()
    assert result.ok is True


def test_a_non_streaming_kind_emits_no_chunks(policy: PolicyEngine) -> None:
    """A memory_get must not open a chunk stream just to close it."""
    chunks: list[dict] = []
    parent = Executor(policy, RecordingConfirm(),
                      chunk_sink=lambda task, body, seq, final: chunks.append(body))
    run(parent, "memory_get", {"key": "k"})
    parent.v3.shutdown()
    assert chunks == []


# ---------------------------------------------------------------------------
# Schedule kinds
# ---------------------------------------------------------------------------
def test_schedule_task_registers_and_lists(v3: ExecutorV3) -> None:
    created = run(v3, "schedule_task",
                  {"id": "nightly", "task": {"kind": "shell", "payload": {"command": "echo hi"}},
                   "cron": "0 6 * * *"})
    assert created.ok is True
    assert created.metadata["schedule"]["cron"] == "0 6 * * *"

    listed = run(v3, "schedule_list")
    assert listed.metadata["count"] == 1
    assert listed.metadata["schedules"][0]["id"] == "nightly"


def test_a_bad_cron_expression_is_a_bad_request(v3: ExecutorV3) -> None:
    result = run(v3, "schedule_task", {"task": {"kind": "shell"}, "cron": "99 * * * *"})
    assert result.decision == "bad_request"


def test_two_triggers_are_refused(v3: ExecutorV3) -> None:
    result = run(v3, "schedule_task",
                 {"task": {"kind": "shell"}, "cron": "* * * * *", "interval_seconds": 60})
    assert result.decision == "bad_request"
    assert "exactly one of" in result.error


def test_schedule_cancel_reports_whether_it_existed(v3: ExecutorV3) -> None:
    run(v3, "schedule_task", {"id": "s", "task": {"kind": "shell"}, "cron": "0 6 * * *"})
    assert run(v3, "schedule_cancel", {"schedule_id": "s"}).ok is True
    missing = run(v3, "schedule_cancel", {"schedule_id": "s"})
    assert missing.ok is False
    assert "no such schedule" in missing.error


def test_schedule_cancel_needs_an_id(v3: ExecutorV3) -> None:
    assert run(v3, "schedule_cancel", {}).decision == "bad_request"


def test_schedule_list_can_hide_exhausted_schedules(v3: ExecutorV3) -> None:
    run(v3, "schedule_task", {"id": "once", "task": {"kind": "shell"},
                              "at": "2020-01-01T00:00:00", "max_runs": 1})
    v3.scheduler.get("once").run_count = 1
    assert run(v3, "schedule_list", {"include_exhausted": False}).metadata["count"] == 0


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell command")
def test_a_fired_schedule_runs_its_task_through_the_executor(parent: Executor) -> None:
    run(parent, "schedule_task",
        {"id": "s", "task": {"kind": "shell", "payload": {"command": "echo fired"}},
         "at": "2020-01-01T00:00:00"})
    fired = parent.v3.scheduler.run_due()
    assert [f["id"] for f in fired] == ["s"]
    assert fired[0]["error"] == ""


def test_schedules_persist_under_the_home_directory(parent: Executor, tmp_path: Path) -> None:
    run(parent, "schedule_task", {"id": "s", "task": {"kind": "shell"}, "cron": "0 6 * * *"})
    assert (tmp_path / ".iddo-harness" / "schedules.json").exists()
    assert default_schedule_path() == tmp_path / ".iddo-harness" / "schedules.json"


def test_an_injected_scheduler_is_used(policy: PolicyEngine, tmp_path: Path) -> None:
    scheduler = Scheduler(path=tmp_path / "s.json")
    executor = ExecutorV3(policy, scheduler=scheduler)
    run(executor, "schedule_task", {"id": "s", "task": {"kind": "shell"}, "cron": "0 6 * * *"})
    executor.shutdown()
    assert scheduler.get("s") is not None


# ---------------------------------------------------------------------------
# llm_task kind
# ---------------------------------------------------------------------------
def test_llm_task_with_the_echo_provider(v3: ExecutorV3) -> None:
    result = run(v3, "llm_task", {"prompt": "hello", "provider": "echo"})
    assert result.ok is True
    assert result.metadata["final_message"] == "echo: hello"
    assert result.metadata["provider"] == "echo"


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell command")
def test_llm_task_drives_a_real_tool_call(parent: Executor) -> None:
    result = run(
        parent,
        "llm_task",
        {
            "prompt": "run it",
            "provider": "scripted",
            "script": [
                {"tool_calls": [{"id": "c1", "name": "shell",
                                 "arguments": {"command": "echo from-the-model"}}]},
                "all done",
            ],
        },
    )
    assert result.ok is True
    assert "from-the-model" in result.metadata["tool_results"][0]["content"]


def test_llm_task_halts_at_a_blocked_tool(parent: Executor) -> None:
    """A model must not be able to talk its way past the policy engine."""
    result = run(
        parent,
        "llm_task",
        {
            "prompt": "delete everything",
            "provider": "scripted",
            "script": [
                {"tool_calls": [{"id": "c1", "name": "shell",
                                 "arguments": {"command": "rm -rf /tmp/whatever"}}]},
                "proceeding anyway",
            ],
        },
    )
    assert result.ok is False
    assert result.metadata["stop_reason"] in {"confirm_required", "blocked"}
    assert "llm loop stopped" in result.error


def test_llm_task_needs_a_prompt(v3: ExecutorV3) -> None:
    assert run(v3, "llm_task", {"provider": "echo"}).decision == "bad_request"


def test_an_unknown_provider_is_a_bad_request(v3: ExecutorV3) -> None:
    result = run(v3, "llm_task", {"prompt": "hi", "provider": "gpt-9"})
    assert result.decision == "bad_request"
    assert "available:" in result.error


def test_a_custom_system_prompt_reaches_the_transcript(v3: ExecutorV3) -> None:
    result = run(v3, "llm_task", {"prompt": "hi", "provider": "echo", "system_prompt": "be terse"})
    assert result.metadata["messages"][0]["content"] == "be terse"


def test_max_iterations_is_honoured(v3: ExecutorV3) -> None:
    result = run(v3, "llm_task", {
        "prompt": "loop", "provider": "scripted", "max_iterations": 2,
        "script": [{"tool_calls": [{"id": f"c{i}", "name": "list_dir",
                                    "arguments": {"path": "."}}]} for i in range(5)],
    })
    assert result.metadata["stop_reason"] == "max_iterations"
    assert result.metadata["iterations"] == 2


# ---------------------------------------------------------------------------
# handshake kind
# ---------------------------------------------------------------------------
def test_handshake_reports_every_generation(v3: ExecutorV3) -> None:
    body = run(v3, "handshake").metadata
    assert set(body["kinds"]["v3"]) == set(V3_KINDS)
    assert body["negotiation"]["compatible"] is True


def test_handshake_negotiates_required_kinds(v3: ExecutorV3) -> None:
    result = run(v3, "handshake", {"required_kinds": ["workflow", "teleport"]})
    assert result.ok is False
    assert result.metadata["negotiation"]["unsupported_kinds"] == ["teleport"]


def test_handshake_advertises_the_runtime_limits(v3: ExecutorV3) -> None:
    limits = run(v3, "handshake").metadata["limits"]
    assert limits["max_workflow_nodes"] == 100
    assert limits["max_memory_value_bytes"] > 0
    assert len(limits["templates"]) == 5


def test_handshake_summarises_policy_without_the_patterns(v3: ExecutorV3, tmp_path: Path) -> None:
    body = run(v3, "handshake").metadata
    assert body["policy"]["available"] is True
    assert str(tmp_path) not in repr(body["policy"])


# ---------------------------------------------------------------------------
# Template kinds
# ---------------------------------------------------------------------------
def test_template_list_reports_the_bundled_five(v3: ExecutorV3) -> None:
    body = run(v3, "template_list").metadata
    assert body["count"] == 5
    assert "nodes" not in body["templates"][0]


def test_template_list_can_include_the_nodes(v3: ExecutorV3) -> None:
    body = run(v3, "template_list", {"include_nodes": True}).metadata
    assert body["templates"][0]["nodes"]


def test_run_template_dry_run_renders_without_running(parent: Executor, tmp_path: Path) -> None:
    result = run(parent, "run_template",
                 {"template": "project_bootstrap", "dry_run": True,
                  "params": {"path": str(tmp_path / "new"), "name": "New"}})
    assert result.ok is True
    assert result.metadata["dry_run"] is True
    assert not (tmp_path / "new").exists()


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell commands")
def test_run_template_executes_the_rendered_plan(parent: Executor, tmp_path: Path) -> None:
    result = run(parent, "run_template",
                 {"template": "disk_space_report", "params": {"path": str(tmp_path), "top": 3}})
    assert result.metadata["template"] == "disk_space_report"
    assert result.metadata["params"]["top"] == 3
    assert result.metadata["counts"]["total"] == 2


def test_run_template_rejects_an_unknown_name(v3: ExecutorV3) -> None:
    result = run(v3, "run_template", {"template": "nope"})
    assert result.decision == "bad_request"
    assert "available:" in result.error


def test_run_template_rejects_a_missing_required_param(v3: ExecutorV3) -> None:
    result = run(v3, "run_template", {"template": "git_status_report", "dry_run": True})
    assert result.decision == "bad_request"
    assert "requires parameter" in result.error


def test_run_template_rejects_an_unknown_param(v3: ExecutorV3) -> None:
    result = run(v3, "run_template",
                 {"template": "git_status_report", "dry_run": True,
                  "params": {"repo": "/tmp", "reepo": "/tmp"}})
    assert result.decision == "bad_request"


def test_name_is_accepted_as_an_alias_for_template(v3: ExecutorV3) -> None:
    assert run(v3, "run_template", {"name": "disk_space_report", "dry_run": True}).ok is True


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def test_read_only_kinds_are_not_gated(tmp_path: Path) -> None:
    """An agent should not need approval to read its own notes."""
    cfg = make_cfg(tmp_path, auto_allow={"commands": [], "paths": {}})
    confirm = RecordingConfirm()
    executor = ExecutorV3(PolicyEngine(cfg), confirm)
    for kind in sorted(_UNGATED_KINDS):
        run(executor, kind, {"key": "k", "task_ids": []})
    executor.shutdown()
    assert confirm.created == []


def test_a_blocked_memory_write_never_reaches_the_store(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, block={"commands": ["memory_set secret*"], "paths": {}})
    executor = ExecutorV3(PolicyEngine(cfg), RecordingConfirm())
    result = run(executor, "memory_set", {"key": "secret-token", "value": "hunter2"})
    assert result.decision == "block"
    assert executor.memory.get("secret-token") is None
    executor.shutdown()


def test_a_confirm_rule_parks_the_task(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, auto_allow={"commands": [], "paths": {}},
                   require_confirm={"commands": ["memory_set*"], "paths": {}})
    confirm = RecordingConfirm()
    executor = ExecutorV3(PolicyEngine(cfg), confirm)
    result = run(executor, "memory_set", {"key": "k", "value": "v"})
    assert result.decision == "confirm_required"
    assert confirm.created == [("t1", result.error)]
    assert executor.memory.get("k") is None
    executor.shutdown()


def test_approving_a_parked_task_performs_it(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, auto_allow={"commands": [], "paths": {}},
                   require_confirm={"commands": ["memory_set*"], "paths": {}})
    executor = ExecutorV3(PolicyEngine(cfg), RecordingConfirm())
    task = FakeTask(id="t1", kind="memory_set", payload={"key": "k", "value": "v"})
    result = executor.resume_after_confirm(task, True)
    assert result.decision == "approved"
    assert executor.memory.get("k") == "v"
    executor.shutdown()


def test_denying_a_parked_task_drops_it(v3: ExecutorV3) -> None:
    task = FakeTask(id="t1", kind="memory_set", payload={"key": "k", "value": "v"})
    result = v3.resume_after_confirm(task, False)
    assert result.decision == "denied"
    assert v3.memory.get("k") is None


def test_resume_of_an_unknown_kind_is_reported(v3: ExecutorV3) -> None:
    result = v3.resume_after_confirm(FakeTask(id="t1", kind="teleport"), True)
    assert result.decision == "unknown_kind"


def test_the_router_forwards_a_v3_resume(parent: Executor) -> None:
    task = FakeTask(id="t1", kind="memory_set", payload={"key": "k", "value": "v"})
    assert parent.resume_after_confirm(task, True).decision == "approved"


def test_composite_kinds_are_gated_per_child_not_per_envelope(tmp_path: Path) -> None:
    """Approving an opaque envelope is not a decision an owner can make."""
    cfg = make_cfg(tmp_path, auto_allow={"commands": [], "paths": {}},
                   require_confirm={"commands": ["*"], "paths": {}})
    confirm = RecordingConfirm()
    parent = Executor(PolicyEngine(cfg), confirm)
    result = run(parent, "workflow", {"nodes": [{"id": "a", "kind": "shell",
                                                 "payload": {"command": "echo hi"}}]})
    parent.v3.shutdown()
    # The workflow itself was not parked; its child was.
    assert result.decision == "auto"
    assert [t for t, _ in confirm.created] == ["t1-a"]


def test_a_v3_kind_with_no_policy_engine_still_runs(tmp_path: Path) -> None:
    executor = ExecutorV3(None, memory=MemoryStore(tmp_path / "m.db"))
    assert run(executor, "memory_set", {"key": "k", "value": 1}).ok is True
    executor.shutdown()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def test_collaborators_are_built_lazily(policy: PolicyEngine, tmp_path: Path) -> None:
    """A harness that never sends a memory packet must not create a database."""
    executor = ExecutorV3(policy, memory_path=tmp_path / "m.db",
                          schedule_path=tmp_path / "s.json")
    assert executor._memory is None
    assert executor._scheduler is None
    assert executor._subtasks is None
    run(executor, "template_list")
    assert executor._memory is None
    assert not (tmp_path / "m.db").exists()
    executor.shutdown()


def test_shutdown_is_safe_before_anything_was_built(policy: PolicyEngine) -> None:
    ExecutorV3(policy).shutdown()


def test_shutdown_is_idempotent(v3: ExecutorV3) -> None:
    run(v3, "memory_set", {"key": "k", "value": 1})
    v3.shutdown()
    v3.shutdown()

"""
Tests for the workflow DAG (agent/workflow.py).

Validation is tested exhaustively because it is the whole point: a broken plan
should cost one fast rejection, not a half-executed run.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import pytest

from workflow import (
    DEFAULT_MAX_PARALLEL,
    MAX_NODES,
    REFERENCEABLE_FIELDS,
    NodeOutcome,
    WorkflowError,
    WorkflowNode,
    WorkflowRunner,
    parse_workflow,
    substitute,
    topological_order,
)


@dataclass
class FakeResult:
    ok: bool = True
    decision: str = "auto"
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = 0
    error: str = ""


def node(node_id: str, **kw: Any) -> dict[str, Any]:
    return {"id": node_id, "kind": "shell", "payload": {"command": node_id}, **kw}


def recorder() -> tuple[list[Any], Any]:
    """An execute stub that records the tasks it was handed."""
    seen: list[Any] = []

    def execute(task: Any) -> FakeResult:
        seen.append(task)
        return FakeResult(stdout=f"out:{task.id}")

    return seen, execute


# ---------------------------------------------------------------------------
# parse_workflow — shape
# ---------------------------------------------------------------------------
def test_minimal_workflow_parses() -> None:
    spec = parse_workflow({"nodes": [node("a")]})
    assert spec.node_ids == ["a"]
    assert spec.max_parallel == DEFAULT_MAX_PARALLEL
    assert spec.fail_fast is True


def test_steps_is_accepted_as_an_alias_for_nodes() -> None:
    assert parse_workflow({"steps": [node("a")]}).node_ids == ["a"]


def test_node_ids_default_to_their_index() -> None:
    assert parse_workflow({"nodes": [{"kind": "shell"}, {"kind": "shell"}]}).node_ids == [
        "node-0",
        "node-1",
    ]


def test_flattened_node_payload_is_accepted() -> None:
    spec = parse_workflow({"nodes": [{"id": "a", "kind": "shell", "command": "ls"}]})
    assert spec.by_id("a").payload == {"command": "ls"}


def test_depends_on_accepts_a_bare_string() -> None:
    spec = parse_workflow({"nodes": [node("a"), node("b", depends_on="a")]})
    assert spec.by_id("b").depends_on == ["a"]


def test_max_parallel_and_fail_fast_are_read() -> None:
    spec = parse_workflow({"nodes": [node("a")], "max_parallel": 7, "fail_fast": False})
    assert (spec.max_parallel, spec.fail_fast) == (7, False)


def test_by_id_raises_for_an_unknown_node() -> None:
    with pytest.raises(KeyError):
        parse_workflow({"nodes": [node("a")]}).by_id("zzz")


# ---------------------------------------------------------------------------
# parse_workflow — rejection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload", [{}, {"nodes": []}, {"nodes": "a"}])
def test_missing_or_empty_nodes_is_rejected(payload: dict) -> None:
    with pytest.raises(WorkflowError, match="non-empty 'nodes'"):
        parse_workflow(payload)


def test_non_object_payload_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="must be an object"):
        parse_workflow(["a"])  # type: ignore[arg-type]


def test_too_many_nodes_is_rejected() -> None:
    with pytest.raises(WorkflowError, match=f"limit is {MAX_NODES}"):
        parse_workflow({"nodes": [node(f"n{i}") for i in range(MAX_NODES + 1)]})


def test_non_object_node_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="node 0 must be an object"):
        parse_workflow({"nodes": ["shell"]})


def test_duplicate_node_id_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="duplicate node id: a"):
        parse_workflow({"nodes": [node("a"), node("a")]})


def test_node_without_a_kind_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="needs a 'kind'"):
        parse_workflow({"nodes": [{"id": "a"}]})


def test_non_object_node_payload_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="payload must be an object"):
        parse_workflow({"nodes": [{"id": "a", "kind": "shell", "payload": []}]})


def test_non_list_depends_on_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="depends_on must be a list"):
        parse_workflow({"nodes": [{"id": "a", "kind": "shell", "depends_on": {"x": 1}}]})


def test_dependency_on_an_unknown_node_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="depends on unknown node: ghost"):
        parse_workflow({"nodes": [node("a", depends_on=["ghost"])]})


def test_self_dependency_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="depends on itself"):
        parse_workflow({"nodes": [node("a", depends_on=["a"])]})


def test_cycle_is_rejected_before_anything_runs() -> None:
    with pytest.raises(WorkflowError, match="dependency cycle"):
        parse_workflow(
            {"nodes": [node("a", depends_on=["b"]), node("b", depends_on=["a"])]}
        )


def test_max_parallel_below_one_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="max_parallel must be >= 1"):
        parse_workflow({"nodes": [node("a")], "max_parallel": 0})


# ---------------------------------------------------------------------------
# parse_workflow — references
# ---------------------------------------------------------------------------
def test_valid_reference_parses() -> None:
    spec = parse_workflow(
        {
            "nodes": [
                node("a"),
                {"id": "b", "kind": "shell", "depends_on": ["a"],
                 "payload": {"command": "echo ${nodes.a.stdout}"}},
            ]
        }
    )
    assert spec.by_id("b").payload["command"] == "echo ${nodes.a.stdout}"


def test_reference_to_an_unknown_node_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="references unknown node: ghost"):
        parse_workflow(
            {"nodes": [{"id": "a", "kind": "shell", "payload": {"c": "${nodes.ghost.stdout}"}}]}
        )


def test_reference_to_an_unsupported_field_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="unsupported field 'metadata'"):
        parse_workflow(
            {
                "nodes": [
                    node("a"),
                    {"id": "b", "kind": "shell", "depends_on": ["a"],
                     "payload": {"c": "${nodes.a.metadata}"}},
                ]
            }
        )


def test_reference_without_a_dependency_is_rejected() -> None:
    """Reading a value you did not wait for is a race, so it is a parse error."""
    with pytest.raises(WorkflowError, match="does not depend on it"):
        parse_workflow(
            {
                "nodes": [
                    node("a"),
                    {"id": "b", "kind": "shell", "payload": {"c": "${nodes.a.stdout}"}},
                ]
            }
        )


def test_references_are_found_inside_nested_payloads() -> None:
    with pytest.raises(WorkflowError, match="references unknown node: ghost"):
        parse_workflow(
            {
                "nodes": [
                    {"id": "a", "kind": "shell",
                     "payload": {"env": {"list": ["${nodes.ghost.ok}"]}}}
                ]
            }
        )


# ---------------------------------------------------------------------------
# topological_order
# ---------------------------------------------------------------------------
def test_topological_order_respects_dependencies() -> None:
    nodes = [
        WorkflowNode(id="c", kind="shell", depends_on=["a", "b"]),
        WorkflowNode(id="a", kind="shell"),
        WorkflowNode(id="b", kind="shell", depends_on=["a"]),
    ]
    order = topological_order(nodes)
    assert order.index("a") < order.index("b") < order.index("c")


def test_topological_order_breaks_ties_by_declaration_order() -> None:
    nodes = [WorkflowNode(id=i, kind="shell") for i in ("z", "m", "a")]
    assert topological_order(nodes) == ["z", "m", "a"]


def test_topological_order_names_the_stuck_nodes() -> None:
    nodes = [
        WorkflowNode(id="a", kind="shell", depends_on=["b"]),
        WorkflowNode(id="b", kind="shell", depends_on=["a"]),
    ]
    with pytest.raises(WorkflowError, match=r"\['a', 'b'\]"):
        topological_order(nodes)


# ---------------------------------------------------------------------------
# substitute
# ---------------------------------------------------------------------------
def _outcomes() -> dict[str, NodeOutcome]:
    return {
        "a": NodeOutcome(id="a", state="succeeded", ok=True,
                         result=FakeResult(stdout="hello", exit_code=3)),
    }


def test_whole_string_reference_keeps_its_native_type() -> None:
    assert substitute("${nodes.a.exit_code}", _outcomes()) == 3
    assert substitute("${nodes.a.ok}", _outcomes()) is True


def test_embedded_reference_is_stringified() -> None:
    assert substitute("say ${nodes.a.stdout}!", _outcomes()) == "say hello!"


def test_substitution_descends_into_containers() -> None:
    value = {"cmd": ["echo", "${nodes.a.stdout}"], "code": "${nodes.a.exit_code}"}
    assert substitute(value, _outcomes()) == {"cmd": ["echo", "hello"], "code": 3}


def test_unknown_reference_resolves_to_none() -> None:
    assert substitute("${nodes.ghost.stdout}", _outcomes()) is None
    assert substitute("x${nodes.ghost.stdout}y", _outcomes()) == "xy"


def test_error_and_ok_come_from_the_outcome_not_the_result() -> None:
    outcomes = {"a": NodeOutcome(id="a", state="failed", ok=False, error="nope")}
    assert substitute("${nodes.a.error}", outcomes) == "nope"
    assert substitute("${nodes.a.ok}", outcomes) is False


def test_non_string_scalars_pass_through() -> None:
    assert substitute(7, _outcomes()) == 7


def test_every_referenceable_field_resolves() -> None:
    for field_name in REFERENCEABLE_FIELDS:
        substitute(f"${{nodes.a.{field_name}}}", _outcomes())


# ---------------------------------------------------------------------------
# WorkflowRunner
# ---------------------------------------------------------------------------
def test_runs_a_linear_chain_in_order() -> None:
    seen, execute = recorder()
    spec = parse_workflow(
        {"nodes": [node("a"), node("b", depends_on=["a"]), node("c", depends_on=["b"])],
         "max_parallel": 3}
    )
    summary = WorkflowRunner(execute).run(spec)
    assert summary["ok"] is True
    assert [t.id for t in seen] == ["a", "b", "c"]
    assert summary["counts"] == {"total": 3, "succeeded": 3, "failed": 0, "skipped": 0, "cancelled": 0}


def test_independent_nodes_run_in_parallel() -> None:
    barrier = threading.Barrier(3, timeout=5)

    def execute(task: Any) -> FakeResult:
        barrier.wait()  # only releases if all three are in flight at once
        return FakeResult()

    spec = parse_workflow({"nodes": [node("a"), node("b"), node("c")], "max_parallel": 3})
    assert WorkflowRunner(execute).run(spec)["ok"] is True


def test_max_parallel_is_respected() -> None:
    live = 0
    peak = 0
    lock = threading.Lock()

    def execute(task: Any) -> FakeResult:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.02)
        with lock:
            live -= 1
        return FakeResult()

    spec = parse_workflow({"nodes": [node(f"n{i}") for i in range(6)], "max_parallel": 2})
    WorkflowRunner(execute).run(spec)
    assert peak <= 2


def test_downstream_node_sees_upstream_output() -> None:
    seen: list[Any] = []

    def execute(task: Any) -> FakeResult:
        seen.append(task)
        return FakeResult(stdout="v1.2.3")

    spec = parse_workflow(
        {
            "nodes": [
                node("version"),
                {"id": "tag", "kind": "shell", "depends_on": ["version"],
                 "payload": {"command": "git tag ${nodes.version.stdout}"}},
            ]
        }
    )
    WorkflowRunner(execute).run(spec)
    assert seen[1].payload["command"] == "git tag v1.2.3"


def test_failure_skips_dependents_and_says_why() -> None:
    def execute(task: Any) -> FakeResult:
        return FakeResult(ok=False, error="boom") if task.id.endswith("a") else FakeResult()

    for fail_fast in (True, False):
        spec = parse_workflow(
            {"nodes": [node("a"), node("b", depends_on=["a"])], "fail_fast": fail_fast}
        )
        summary = WorkflowRunner(execute).run(spec)
        assert summary["ok"] is False
        states = {n["id"]: n for n in summary["nodes"]}
        assert states["a"]["state"] == "failed"
        assert states["b"]["state"] == "skipped"
        # The reason names the culprit either way — whether the node was
        # evaluated normally or resolved by the stop-launching path.
        assert states["b"]["skipped_because"] == "dependency a failed"


def test_continue_on_error_makes_a_node_advisory() -> None:
    def execute(task: Any) -> FakeResult:
        return FakeResult(ok=False) if task.id.endswith("lint") else FakeResult()

    spec = parse_workflow(
        {"nodes": [node("lint", continue_on_error=True), node("build")], "max_parallel": 2}
    )
    summary = WorkflowRunner(execute).run(spec)
    assert summary["ok"] is True
    assert summary["counts"]["failed"] == 1


def test_fail_fast_false_still_runs_independent_nodes() -> None:
    def execute(task: Any) -> FakeResult:
        return FakeResult(ok=False) if task.id.endswith("a") else FakeResult()

    spec = parse_workflow(
        {"nodes": [node("a"), node("b"), node("c")], "fail_fast": False, "max_parallel": 1}
    )
    summary = WorkflowRunner(execute).run(spec)
    assert summary["counts"]["succeeded"] == 2
    assert summary["ok"] is False


def test_a_raising_node_fails_rather_than_propagating() -> None:
    def execute(task: Any) -> FakeResult:
        raise RuntimeError("kaboom")

    summary = WorkflowRunner(execute).run(parse_workflow({"nodes": [node("a")]}))
    assert summary["ok"] is False
    assert "RuntimeError: kaboom" in summary["nodes"][0]["error"]


def test_on_node_done_fires_once_per_node() -> None:
    seen_ids: list[str] = []
    _, execute = recorder()
    spec = parse_workflow({"nodes": [node("a"), node("b", depends_on=["a"])]})
    WorkflowRunner(execute, on_node_done=lambda o: seen_ids.append(o.id)).run(spec)
    assert sorted(seen_ids) == ["a", "b"]


def test_a_failing_hook_does_not_abort_the_plan() -> None:
    def bad_hook(outcome: NodeOutcome) -> None:
        raise RuntimeError("reporting broke")

    _, execute = recorder()
    summary = WorkflowRunner(execute, on_node_done=bad_hook).run(
        parse_workflow({"nodes": [node("a")]})
    )
    assert summary["ok"] is True


def test_cancel_before_the_run_cancels_everything() -> None:
    _, execute = recorder()
    runner = WorkflowRunner(execute)
    runner.cancel()
    summary = runner.run(parse_workflow({"nodes": [node("a"), node("b")]}))
    assert summary["cancelled"] is True
    assert summary["ok"] is False
    assert {n["state"] for n in summary["nodes"]} == {"cancelled"}


def test_node_task_ids_are_namespaced_by_the_parent() -> None:
    seen, execute = recorder()
    parent = type("P", (), {"id": "wf-9", "envelope": None})()
    WorkflowRunner(execute, parent=parent).run(parse_workflow({"nodes": [node("a")]}))
    assert seen[0].id == "wf-9-a"


def test_node_task_ids_fall_back_to_the_node_id() -> None:
    seen, execute = recorder()
    WorkflowRunner(execute).run(parse_workflow({"nodes": [node("a")]}))
    assert seen[0].id == "a"


def test_summary_reports_the_topological_order() -> None:
    _, execute = recorder()
    spec = parse_workflow({"nodes": [node("b", depends_on=["a"]), node("a")]})
    assert WorkflowRunner(execute).run(spec)["order"] == ["a", "b"]


# ---------------------------------------------------------------------------
# NodeOutcome
# ---------------------------------------------------------------------------
def test_outcome_duration_is_none_before_it_starts() -> None:
    assert NodeOutcome(id="a", state="skipped").duration is None


def test_outcome_dict_omits_result_fields_when_there_is_no_result() -> None:
    body = NodeOutcome(id="a", state="skipped", skipped_because="upstream failure").to_dict()
    assert "stdout" not in body
    assert body["skipped_because"] == "upstream failure"


def test_outcome_dict_truncates_long_output() -> None:
    outcome = NodeOutcome(
        id="a", state="succeeded", ok=True, result=FakeResult(stdout="x" * 9000, stderr="y" * 5000)
    )
    body = outcome.to_dict()
    assert len(body["stdout"]) == 4000
    assert len(body["stderr"]) == 2000

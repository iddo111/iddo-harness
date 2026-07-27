"""
Workflow DAG — one packet that describes a whole plan.

``spawn_task``/``await_tasks`` give a producer fan-out, but the producer still
has to hold the shape of the plan and drive it step by step. A workflow moves
the shape itself onto the harness: a list of nodes, each naming the nodes it
depends on, submitted once.

What that buys beyond a loop of sub-tasks:

* **Ordering is declared, not sequenced.** Independent nodes run in parallel up
  to ``max_parallel``; dependent ones wait. Nobody writes the scheduling.
* **Cycles are caught before anything runs.** A DAG with a loop is a producer
  bug, and it should fail in validation rather than deadlock at step four.
* **Outputs flow downstream.** ``${nodes.build.stdout}`` in a later payload is
  substituted with what the ``build`` node actually printed.
* **Failure has a policy.** ``fail_fast`` stops the run; ``continue_on_error``
  on a node makes it advisory; skipped nodes say *why* they were skipped
  instead of silently not appearing.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

try:
    from subtasks import SubTask, build_subtask
except ImportError:  # pragma: no cover - packaged import
    from agent.subtasks import SubTask, build_subtask  # type: ignore[no-redef]

log = logging.getLogger("harness.workflow")

DEFAULT_MAX_PARALLEL = 3
DEFAULT_NODE_TIMEOUT = 600.0
MAX_NODES = 100

#: ``${nodes.<id>.<field>}`` — the only interpolation form. Deliberately not a
#: template language: a workflow is a plan, and a plan that needs branching
#: logic wants a script node, not more syntax.
_REF_RE = re.compile(r"\$\{nodes\.([A-Za-z0-9_\-]+)\.([A-Za-z0-9_]+)\}")

#: Fields of a node result that a downstream node may reference.
REFERENCEABLE_FIELDS = frozenset({"stdout", "stderr", "exit_code", "ok", "error", "decision"})


class WorkflowError(ValueError):
    """Raised when a workflow spec is malformed: bad node, cycle, bad reference."""


@dataclass
class WorkflowNode:
    """One step of a plan."""

    id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    continue_on_error: bool = False
    timeout_sec: float = DEFAULT_NODE_TIMEOUT


@dataclass
class NodeOutcome:
    """What happened to one node."""

    id: str
    state: str  # succeeded | failed | skipped | cancelled
    ok: bool = False
    result: Any = None
    error: str = ""
    skipped_because: str = ""
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def duration(self) -> float | None:
        if self.started_at is None:
            return None
        return (self.finished_at or time.time()) - self.started_at

    def to_dict(self) -> dict[str, Any]:
        """Render for a workflow result body."""
        body: dict[str, Any] = {
            "id": self.id,
            "state": self.state,
            "ok": self.ok,
            "duration_sec": self.duration,
        }
        if self.error:
            body["error"] = self.error
        if self.skipped_because:
            body["skipped_because"] = self.skipped_because
        if self.result is not None:
            body["stdout"] = (getattr(self.result, "stdout", "") or "")[-4000:]
            body["stderr"] = (getattr(self.result, "stderr", "") or "")[-2000:]
            body["exit_code"] = getattr(self.result, "exit_code", None)
            body["decision"] = getattr(self.result, "decision", "")
        return body


@dataclass
class WorkflowSpec:
    """A validated, acyclic plan."""

    nodes: list[WorkflowNode]
    max_parallel: int = DEFAULT_MAX_PARALLEL
    fail_fast: bool = True

    @property
    def node_ids(self) -> list[str]:
        return [n.id for n in self.nodes]

    def by_id(self, node_id: str) -> WorkflowNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)


def parse_workflow(payload: dict[str, Any]) -> WorkflowSpec:
    """Validate a ``workflow`` payload into a :class:`WorkflowSpec`.

    Everything that can be wrong statically is caught here — unknown
    dependency, duplicate id, cycle, dangling ``${nodes...}`` reference — so a
    broken plan costs one fast rejection rather than a half-executed run.
    """
    if not isinstance(payload, dict):
        raise WorkflowError("workflow payload must be an object")

    raw_nodes = payload.get("nodes") or payload.get("steps")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise WorkflowError("workflow needs a non-empty 'nodes' list")
    if len(raw_nodes) > MAX_NODES:
        raise WorkflowError(f"workflow has {len(raw_nodes)} nodes; the limit is {MAX_NODES}")

    nodes: list[WorkflowNode] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise WorkflowError(f"node {index} must be an object")
        node_id = str(raw.get("id") or f"node-{index}").strip()
        if not node_id:
            raise WorkflowError(f"node {index} has an empty id")
        if node_id in seen:
            raise WorkflowError(f"duplicate node id: {node_id}")
        seen.add(node_id)

        kind = str(raw.get("kind") or "").strip()
        if not kind:
            raise WorkflowError(f"node {node_id} needs a 'kind'")

        node_payload = raw.get("payload")
        if node_payload is None:
            node_payload = {
                k: v
                for k, v in raw.items()
                if k not in {"id", "kind", "payload", "depends_on", "continue_on_error", "timeout_sec"}
            }
        if not isinstance(node_payload, dict):
            raise WorkflowError(f"node {node_id} payload must be an object")

        depends = raw.get("depends_on") or []
        if isinstance(depends, str):
            depends = [depends]
        if not isinstance(depends, list):
            raise WorkflowError(f"node {node_id} depends_on must be a list")

        nodes.append(
            WorkflowNode(
                id=node_id,
                kind=kind,
                payload=dict(node_payload),
                depends_on=[str(d) for d in depends],
                continue_on_error=bool(raw.get("continue_on_error", False)),
                timeout_sec=float(raw.get("timeout_sec", DEFAULT_NODE_TIMEOUT)),
            )
        )

    ids = {n.id for n in nodes}
    for node in nodes:
        for dep in node.depends_on:
            if dep not in ids:
                raise WorkflowError(f"node {node.id} depends on unknown node: {dep}")
            if dep == node.id:
                raise WorkflowError(f"node {node.id} depends on itself")
        for ref_id, ref_field in _references(node.payload):
            if ref_id not in ids:
                raise WorkflowError(f"node {node.id} references unknown node: {ref_id}")
            if ref_field not in REFERENCEABLE_FIELDS:
                raise WorkflowError(
                    f"node {node.id} references unsupported field '{ref_field}'; "
                    f"expected one of {sorted(REFERENCEABLE_FIELDS)}"
                )
            if ref_id not in node.depends_on:
                raise WorkflowError(
                    f"node {node.id} references {ref_id} but does not depend on it — "
                    "add it to depends_on so the value exists when it is read"
                )

    topological_order(nodes)  # raises on a cycle

    max_parallel = int(payload.get("max_parallel", DEFAULT_MAX_PARALLEL))
    if max_parallel < 1:
        raise WorkflowError("max_parallel must be >= 1")

    return WorkflowSpec(
        nodes=nodes,
        max_parallel=max_parallel,
        fail_fast=bool(payload.get("fail_fast", True)),
    )


def topological_order(nodes: Iterable[WorkflowNode]) -> list[str]:
    """Return node ids in a dependency-respecting order.

    Kahn's algorithm; ties broken by declaration order so the same plan always
    produces the same order and a test can assert on it. Raises
    :class:`WorkflowError` naming the nodes still stuck when a cycle exists.
    """
    node_list = list(nodes)
    remaining = {n.id: set(n.depends_on) for n in node_list}
    order: list[str] = []

    while remaining:
        ready = [n.id for n in node_list if n.id in remaining and not remaining[n.id]]
        if not ready:
            raise WorkflowError(f"workflow has a dependency cycle among: {sorted(remaining)}")
        for node_id in ready:
            order.append(node_id)
            remaining.pop(node_id)
        for deps in remaining.values():
            deps.difference_update(ready)
    return order


def _references(value: Any) -> list[tuple[str, str]]:
    """Every ``${nodes.x.y}`` reference inside a nested payload."""
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.extend(_REF_RE.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_references(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_references(item))
    return found


def substitute(value: Any, outcomes: dict[str, NodeOutcome]) -> Any:
    """Replace ``${nodes.x.y}`` references with the values produced so far.

    A whole-string reference keeps its native type (an ``exit_code`` stays an
    int), while a reference embedded in surrounding text is stringified — the
    behaviour a shell command needs.
    """
    if isinstance(value, str):
        whole = _REF_RE.fullmatch(value.strip())
        if whole is not None:
            return _lookup(whole.group(1), whole.group(2), outcomes)
        return _REF_RE.sub(
            lambda m: str(_lookup(m.group(1), m.group(2), outcomes) or ""), value
        )
    if isinstance(value, dict):
        return {k: substitute(v, outcomes) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, outcomes) for v in value]
    return value


def _lookup(node_id: str, field_name: str, outcomes: dict[str, NodeOutcome]) -> Any:
    outcome = outcomes.get(node_id)
    if outcome is None:
        return None
    if field_name == "ok":
        return outcome.ok
    if field_name == "error":
        return outcome.error
    if outcome.result is None:
        return None
    return getattr(outcome.result, field_name, None)


class WorkflowRunner:
    """Executes a :class:`WorkflowSpec`, respecting dependencies and parallelism.

    ``execute`` is injected for the same reason it is in
    :class:`agent.subtasks.SubtaskManager`: this class schedules, and knows
    nothing about how a kind is actually run.
    """

    def __init__(
        self,
        execute: Callable[[Any], Any],
        *,
        parent: Any = None,
        on_node_done: Callable[[NodeOutcome], None] | None = None,
    ) -> None:
        self.execute = execute
        self.parent = parent
        self.on_node_done = on_node_done
        self._lock = threading.RLock()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Stop launching new nodes. In-flight nodes finish on their own."""
        self._cancelled.set()

    def run(self, spec: WorkflowSpec) -> dict[str, Any]:
        """Execute every node and return a summary of the whole plan."""
        outcomes: dict[str, NodeOutcome] = {}
        started_at = time.time()
        stop_launching = False

        with ThreadPoolExecutor(
            max_workers=spec.max_parallel, thread_name_prefix="workflow"
        ) as pool:
            in_flight: dict[Any, WorkflowNode] = {}
            pending = {n.id: n for n in spec.nodes}

            while pending or in_flight:
                if not stop_launching and not self._cancelled.is_set():
                    for node in self._ready_nodes(pending, outcomes, len(in_flight), spec):
                        skip_reason = self._skip_reason(node, outcomes)
                        if skip_reason:
                            outcomes[node.id] = NodeOutcome(
                                id=node.id, state="skipped", skipped_because=skip_reason
                            )
                            pending.pop(node.id, None)
                            self._notify(outcomes[node.id])
                            continue
                        pending.pop(node.id)
                        future = pool.submit(self._run_node, node, outcomes)
                        in_flight[future] = node

                if not in_flight:
                    if not pending:
                        break
                    # Nothing running and nothing launched this pass: the rest
                    # of the plan can never start. Resolve it explicitly rather
                    # than spinning — a silent hang is the one outcome a
                    # workflow must not have.
                    cancelled = self._cancelled.is_set()
                    for node in list(pending.values()):
                        reason = "workflow cancelled" if cancelled else (
                            self._skip_reason(node, outcomes) or "upstream failure"
                        )
                        outcomes[node.id] = NodeOutcome(
                            id=node.id,
                            state="cancelled" if cancelled else "skipped",
                            skipped_because=reason,
                        )
                        pending.pop(node.id)
                        self._notify(outcomes[node.id])
                    break

                done, _ = wait(list(in_flight), return_when="FIRST_COMPLETED")
                for future in done:
                    node = in_flight.pop(future)
                    outcome = future.result()
                    with self._lock:
                        outcomes[node.id] = outcome
                    self._notify(outcome)
                    if not outcome.ok and not node.continue_on_error and spec.fail_fast:
                        stop_launching = True

        ordered = [outcomes[n.id] for n in spec.nodes if n.id in outcomes]
        failed = [o for o in ordered if o.state == "failed"]
        blocking_failures = [
            o for o in failed if not spec.by_id(o.id).continue_on_error
        ]
        return {
            "ok": not blocking_failures and not self._cancelled.is_set(),
            "cancelled": self._cancelled.is_set(),
            "nodes": [o.to_dict() for o in ordered],
            "order": topological_order(spec.nodes),
            "counts": {
                "total": len(spec.nodes),
                "succeeded": sum(1 for o in ordered if o.state == "succeeded"),
                "failed": len(failed),
                "skipped": sum(1 for o in ordered if o.state == "skipped"),
                "cancelled": sum(1 for o in ordered if o.state == "cancelled"),
            },
            "duration_sec": time.time() - started_at,
        }

    # -- internals ----------------------------------------------------------

    def _ready_nodes(
        self,
        pending: dict[str, WorkflowNode],
        outcomes: dict[str, NodeOutcome],
        in_flight: int,
        spec: WorkflowSpec,
    ) -> list[WorkflowNode]:
        """Nodes whose dependencies have all resolved, up to the parallel cap."""
        slots = max(0, spec.max_parallel - in_flight)
        if not slots:
            return []
        ready = [
            node
            for node in spec.nodes
            if node.id in pending and all(dep in outcomes for dep in node.depends_on)
        ]
        return ready[:slots]

    @staticmethod
    def _skip_reason(node: WorkflowNode, outcomes: dict[str, NodeOutcome]) -> str:
        """Why this node cannot run, given how its dependencies turned out."""
        for dep in node.depends_on:
            outcome = outcomes.get(dep)
            if outcome is None:
                continue
            if outcome.state == "skipped":
                return f"dependency {dep} was skipped"
            if outcome.state == "cancelled":
                return f"dependency {dep} was cancelled"
            if not outcome.ok:
                return f"dependency {dep} failed"
        return ""

    def _run_node(self, node: WorkflowNode, outcomes: dict[str, NodeOutcome]) -> NodeOutcome:
        outcome = NodeOutcome(id=node.id, state="running", started_at=time.time())
        try:
            with self._lock:
                resolved = substitute(node.payload, dict(outcomes))
            task = build_subtask(
                {"id": self._node_task_id(node), "kind": node.kind, "payload": resolved},
                parent=self.parent,
            )
            result = self.execute(task)
            outcome.result = result
            outcome.ok = bool(getattr(result, "ok", False))
            outcome.state = "succeeded" if outcome.ok else "failed"
            if not outcome.ok:
                outcome.error = getattr(result, "error", "") or "node returned ok=false"
        except Exception as exc:
            log.exception("workflow node %s raised", node.id)
            outcome.state = "failed"
            outcome.error = f"{type(exc).__name__}: {exc}"
        finally:
            outcome.finished_at = time.time()
        return outcome

    def _node_task_id(self, node: WorkflowNode) -> str:
        parent_id = getattr(self.parent, "id", None)
        return f"{parent_id}-{node.id}" if parent_id else node.id

    def _notify(self, outcome: NodeOutcome) -> None:
        if self.on_node_done is None:
            return
        try:
            self.on_node_done(outcome)
        except Exception:  # a reporting failure must not abort the plan
            log.exception("workflow on_node_done hook failed for %s", outcome.id)


__all__ = [
    "NodeOutcome",
    "WorkflowError",
    "WorkflowNode",
    "WorkflowRunner",
    "WorkflowSpec",
    "parse_workflow",
    "substitute",
    "topological_order",
    "SubTask",
]

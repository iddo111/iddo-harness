"""
Tests for task templates (agent/templates.py).

Beyond rendering mechanics, this file pins two design decisions: a template
renders to an ordinary workflow that ``parse_workflow`` accepts (there is no
second execution path), and only ``project_bootstrap`` writes anything.
"""
from __future__ import annotations

from typing import Any

import pytest

from templates import (
    BUNDLED_TEMPLATES,
    MAX_RENDER_DEPTH,
    Template,
    TemplateError,
    TemplateParam,
    TemplateRegistry,
    render,
    resolve_params,
    substitute_params,
)
from workflow import parse_workflow

DEMO = Template(
    name="demo",
    description="A template for tests.",
    params=[
        TemplateParam(name="path", description="where", required=True),
        TemplateParam(name="count", description="how many", default=5),
    ],
    nodes=[{"id": "a", "kind": "shell", "payload": {"command": "ls ${path}", "n": "${count}"}}],
)


@pytest.fixture()
def registry() -> TemplateRegistry:
    return TemplateRegistry()


# ---------------------------------------------------------------------------
# The bundled five
# ---------------------------------------------------------------------------
def test_five_templates_ship_in_the_box(registry: TemplateRegistry) -> None:
    assert len(BUNDLED_TEMPLATES) == 5
    assert registry.names() == [
        "disk_space_report",
        "git_status_report",
        "log_error_scan",
        "project_bootstrap",
        "python_test_run",
    ]


@pytest.mark.parametrize("template", BUNDLED_TEMPLATES, ids=lambda t: t.name)
def test_every_bundled_template_has_a_description(template: Template) -> None:
    assert template.description
    assert template.nodes
    assert all(p.description for p in template.params)


@pytest.mark.parametrize("template", BUNDLED_TEMPLATES, ids=lambda t: t.name)
def test_every_bundled_template_renders_to_a_valid_workflow(template: Template) -> None:
    """A template is not a second execution path — it produces a plain plan."""
    params = {p.name: "/tmp/demo" for p in template.params if p.required}
    spec = parse_workflow(render(template, params))
    assert len(spec.nodes) == len(template.nodes)


def test_only_project_bootstrap_writes() -> None:
    """The easiest thing to invoke by name should not be the thing that writes."""
    writers = [t.name for t in BUNDLED_TEMPLATES if t.writes]
    assert writers == ["project_bootstrap"]


def test_git_status_report_wires_the_repo_through(registry: TemplateRegistry) -> None:
    rendered = registry.render("git_status_report", {"repo": "/srv/app", "log_count": 3})
    commands = [n["payload"]["command"] for n in rendered["nodes"]]
    assert "git -C '/srv/app' rev-parse --abbrev-ref HEAD" in commands
    assert "git -C '/srv/app' log --oneline -n 3" in commands
    assert rendered["fail_fast"] is False


def test_python_test_run_defaults_are_applied(registry: TemplateRegistry) -> None:
    rendered = registry.render("python_test_run", {"repo": "/srv/app"})
    tests = next(n for n in rendered["nodes"] if n["id"] == "tests")
    assert tests["payload"]["command"] == "cd '/srv/app' && python3 -m pytest tests/ -q"
    assert tests["depends_on"] == ["interpreter"]


def test_disk_space_report_needs_nothing(registry: TemplateRegistry) -> None:
    rendered = registry.render("disk_space_report")
    assert rendered["params"] == {"path": ".", "top": 15}


def test_log_error_scan_uses_glob_and_grep_nodes(registry: TemplateRegistry) -> None:
    rendered = registry.render("log_error_scan", {"path": "/var/log"})
    assert {n["kind"] for n in rendered["nodes"]} == {"glob", "grep", "shell"}


def test_project_bootstrap_writes_under_the_caller_named_path(registry: TemplateRegistry) -> None:
    rendered = registry.render("project_bootstrap", {"path": "/srv/new", "name": "New"})
    readme = next(n for n in rendered["nodes"] if n["id"] == "readme")
    assert readme["kind"] == "write_file"
    assert readme["payload"]["path"] == "/srv/new/README.md"
    assert readme["payload"]["content"].startswith("# New")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def test_unknown_template_lists_the_alternatives(registry: TemplateRegistry) -> None:
    with pytest.raises(TemplateError, match="available:"):
        registry.get("nope")


def test_register_adds_and_replaces(registry: TemplateRegistry) -> None:
    registry.register(DEMO)
    assert registry.get("demo") is DEMO
    replacement = Template(name="demo", description="v2", nodes=[{"id": "a", "kind": "shell"}])
    registry.register(replacement)
    assert registry.get("demo") is replacement


def test_a_nameless_template_is_refused(registry: TemplateRegistry) -> None:
    with pytest.raises(TemplateError, match="needs a name"):
        registry.register(Template(name="", description="x"))


def test_lookup_tolerates_surrounding_whitespace(registry: TemplateRegistry) -> None:
    assert registry.get("  git_status_report  ").name == "git_status_report"


def test_a_registry_can_be_built_from_a_custom_set() -> None:
    assert TemplateRegistry([DEMO]).names() == ["demo"]


def test_listing_hides_the_nodes_by_default(registry: TemplateRegistry) -> None:
    """The point of a template is that a caller need not read its steps."""
    entry = registry.list()[0]
    assert "nodes" not in entry
    assert entry["node_count"] >= 1
    assert registry.list(include_nodes=True)[0]["nodes"]


def test_listing_reports_the_parameters(registry: TemplateRegistry) -> None:
    entry = next(e for e in registry.list() if e["name"] == "git_status_report")
    repo = next(p for p in entry["params"] if p["name"] == "repo")
    assert repo["required"] is True
    assert next(p for p in entry["params"] if p["name"] == "log_count")["default"] == 10


# ---------------------------------------------------------------------------
# resolve_params
# ---------------------------------------------------------------------------
def test_defaults_fill_the_blanks() -> None:
    assert resolve_params(DEMO, {"path": "/tmp"}) == {"path": "/tmp", "count": 5}


def test_given_values_override_defaults() -> None:
    assert resolve_params(DEMO, {"path": "/tmp", "count": 9})["count"] == 9


def test_an_explicit_none_falls_back_to_the_default() -> None:
    assert resolve_params(DEMO, {"path": "/tmp", "count": None})["count"] == 5


def test_a_missing_required_parameter_is_named() -> None:
    with pytest.raises(TemplateError, match="requires parameter\\(s\\): path"):
        resolve_params(DEMO, {})


def test_an_unknown_parameter_is_an_error_not_a_shrug() -> None:
    """A typo'd parameter that silently does nothing produces a quiet wrong run."""
    with pytest.raises(TemplateError, match="has no parameter\\(s\\): pathh"):
        resolve_params(DEMO, {"path": "/tmp", "pathh": "/tmp"})


def test_no_params_at_all_is_accepted() -> None:
    template = Template(name="t", description="d", nodes=[{"id": "a", "kind": "shell"}])
    assert resolve_params(template, None) == {}


# ---------------------------------------------------------------------------
# substitute_params
# ---------------------------------------------------------------------------
def test_whole_string_reference_keeps_its_native_type() -> None:
    assert substitute_params("${count}", {"count": 10}) == 10
    assert substitute_params("${flag}", {"flag": True}) is True


def test_embedded_reference_is_stringified() -> None:
    assert substitute_params("-n ${count}", {"count": 10}) == "-n 10"


def test_substitution_descends_into_containers() -> None:
    value = {"cmd": ["ls", "${path}"], "n": "${count}"}
    assert substitute_params(value, {"path": "/tmp", "count": 3}) == {
        "cmd": ["ls", "/tmp"], "n": 3
    }


def test_node_references_survive_rendering() -> None:
    """``${nodes.x.y}`` belongs to the workflow layer and must pass through."""
    assert substitute_params("${nodes.build.stdout}", {"path": "/tmp"}) == "${nodes.build.stdout}"


def test_unknown_parameter_references_are_left_alone() -> None:
    assert substitute_params("${mystery}", {"path": "/tmp"}) == "${mystery}"


def test_non_string_scalars_pass_through() -> None:
    assert substitute_params(7, {}) == 7
    assert substitute_params(None, {}) is None


def test_absurdly_nested_structures_are_refused() -> None:
    value: Any = "leaf"
    for _ in range(MAX_RENDER_DEPTH + 3):
        value = [value]
    with pytest.raises(TemplateError, match="too deep"):
        substitute_params(value, {})


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------
def test_render_returns_a_workflow_payload() -> None:
    rendered = render(DEMO, {"path": "/tmp"})
    assert set(rendered) == {"nodes", "max_parallel", "fail_fast", "template", "params"}
    assert rendered["template"] == "demo"
    assert rendered["params"] == {"path": "/tmp", "count": 5}


def test_render_substitutes_into_the_nodes() -> None:
    node = render(DEMO, {"path": "/tmp"})["nodes"][0]
    assert node["payload"] == {"command": "ls /tmp", "n": 5}


def test_render_does_not_mutate_the_template() -> None:
    render(DEMO, {"path": "/tmp"})
    assert DEMO.nodes[0]["payload"]["command"] == "ls ${path}"


def test_registry_render_is_the_same_thing_by_name() -> None:
    registry = TemplateRegistry([DEMO])
    assert registry.render("demo", {"path": "/tmp"}) == render(DEMO, {"path": "/tmp"})


def test_rendered_output_parses_as_a_workflow() -> None:
    assert parse_workflow(render(DEMO, {"path": "/tmp"})).node_ids == ["a"]


def test_template_to_dict_reports_the_write_flag() -> None:
    assert Template(name="t", description="d", writes=True).to_dict()["writes"] is True

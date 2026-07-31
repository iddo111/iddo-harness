"""
Task templates — ``kind: run_template``, the named plans that ship in the box.

A workflow is expressive, and that is exactly its cost: "check the repo" is
six nodes of JSON that a producer has to get right every single time, and one
typo in ``depends_on`` is a rejected packet. Templates put the plans that get
run over and over behind a name and a couple of parameters.

A template renders to an ordinary workflow spec. There is no second execution
path: ``run_template`` fills the blanks, hands the result to
:mod:`agent.workflow`, and everything downstream — DAG validation, parallelism,
``${nodes...}`` references, policy — behaves identically to a hand-written plan.

The five bundled templates are all read-mostly on purpose. A template is the
easiest thing in the harness to invoke by name, and the easiest thing to invoke
should not be the thing that deletes your files. ``project_bootstrap`` is the
only one that writes, and everything it writes goes under a path the caller
names — where policy sees it as a write and gates it accordingly.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger("harness.templates")

#: ``${name}`` — parameters only. Node references (``${nodes.x.y}``) belong to
#: the workflow layer and are deliberately left untouched here so a template
#: can wire its own steps together.
_PARAM_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

MAX_RENDER_DEPTH = 20


class TemplateError(ValueError):
    """Raised for an unknown template, a missing parameter, or a bad override."""


@dataclass
class TemplateParam:
    """One blank a caller may (or must) fill in."""

    name: str
    description: str = ""
    required: bool = False
    default: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "required": self.required,
            "default": self.default,
        }


@dataclass
class Template:
    """A named, parameterised workflow."""

    name: str
    description: str
    params: list[TemplateParam] = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    max_parallel: int = 2
    fail_fast: bool = True
    writes: bool = False

    def to_dict(self, *, include_nodes: bool = False) -> dict[str, Any]:
        """Render for ``template_list`` — nodes only when asked, since the
        point of a template is that a caller does not need to read them."""
        body: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
            "node_count": len(self.nodes),
            "max_parallel": self.max_parallel,
            "fail_fast": self.fail_fast,
            "writes": self.writes,
        }
        if include_nodes:
            body["nodes"] = self.nodes
        return body


def _p(name: str, description: str, *, required: bool = False, default: Any = None) -> TemplateParam:
    return TemplateParam(name=name, description=description, required=required, default=default)


# ---------------------------------------------------------------------------
# The bundled five
# ---------------------------------------------------------------------------
GIT_STATUS_REPORT = Template(
    name="git_status_report",
    description="Branch, working-tree status and recent history for a git checkout.",
    params=[
        _p("repo", "Path to the git checkout.", required=True),
        _p("log_count", "How many recent commits to include.", default=10),
    ],
    nodes=[
        {
            "id": "branch",
            "kind": "shell",
            "payload": {"command": "git -C '${repo}' rev-parse --abbrev-ref HEAD", "paths": ["${repo}"]},
        },
        {
            "id": "status",
            "kind": "shell",
            "payload": {"command": "git -C '${repo}' status --short --branch", "paths": ["${repo}"]},
        },
        {
            "id": "log",
            "kind": "shell",
            "payload": {
                "command": "git -C '${repo}' log --oneline -n ${log_count}",
                "paths": ["${repo}"],
            },
        },
    ],
    max_parallel=3,
    fail_fast=False,
)

PYTHON_TEST_RUN = Template(
    name="python_test_run",
    description="Report the interpreter, then run a project's pytest suite.",
    params=[
        _p("repo", "Path to the project root.", required=True),
        _p("test_path", "Directory or file to run.", default="tests/"),
        _p("python", "Interpreter to use.", default="python3"),
        _p("pytest_args", "Extra arguments passed to pytest.", default="-q"),
    ],
    nodes=[
        {
            "id": "interpreter",
            "kind": "shell",
            "payload": {"command": "${python} --version", "paths": ["${repo}"]},
        },
        {
            "id": "tests",
            "kind": "shell",
            "depends_on": ["interpreter"],
            "payload": {
                "command": "cd '${repo}' && ${python} -m pytest ${test_path} ${pytest_args}",
                "paths": ["${repo}"],
                "timeout_sec": 1800,
            },
        },
    ],
    max_parallel=1,
)

DISK_SPACE_REPORT = Template(
    name="disk_space_report",
    description="Filesystem usage plus the largest directories under a path.",
    params=[
        _p("path", "Directory to measure.", default="."),
        _p("top", "How many large directories to list.", default=15),
    ],
    nodes=[
        {"id": "filesystems", "kind": "shell", "payload": {"command": "df -h", "paths": ["${path}"]}},
        {
            "id": "largest",
            "kind": "shell",
            "payload": {
                "command": "du -h -d 2 '${path}' 2>/dev/null | sort -rh | head -n ${top}",
                "paths": ["${path}"],
                "timeout_sec": 300,
            },
        },
    ],
    max_parallel=2,
    fail_fast=False,
)

LOG_ERROR_SCAN = Template(
    name="log_error_scan",
    description="Search a log tree for error-shaped lines, then tail the newest file.",
    params=[
        _p("path", "Directory holding the logs.", required=True),
        _p("pattern", "Regex to search for.", default="ERROR|CRITICAL|Traceback|FATAL"),
        _p("glob", "Which files count as logs.", default="**/*.log"),
        _p("tail_lines", "Lines of the newest log to include.", default=100),
    ],
    nodes=[
        {
            "id": "files",
            "kind": "glob",
            "payload": {"path": "${path}", "pattern": "${glob}"},
        },
        {
            "id": "matches",
            "kind": "grep",
            "payload": {"path": "${path}", "pattern": "${pattern}", "context": 2},
        },
        {
            "id": "tail",
            "kind": "shell",
            "depends_on": ["files"],
            "payload": {
                "command": "ls -1t '${path}'/*.log 2>/dev/null | head -1 | xargs -r tail -n ${tail_lines}",
                "paths": ["${path}"],
            },
        },
    ],
    max_parallel=2,
    fail_fast=False,
)

PROJECT_BOOTSTRAP = Template(
    name="project_bootstrap",
    description="Create a directory, initialise git, and write a starter README.",
    params=[
        _p("path", "Directory to create the project in.", required=True),
        _p("name", "Project name, used in the README heading.", required=True),
        _p("description", "One-line project description.", default="TODO: describe this project."),
    ],
    nodes=[
        {
            "id": "mkdir",
            "kind": "shell",
            "payload": {"command": "mkdir -p '${path}'", "paths": ["${path}"]},
        },
        {
            "id": "readme",
            "kind": "write_file",
            "depends_on": ["mkdir"],
            "payload": {
                "path": "${path}/README.md",
                "content": "# ${name}\n\n${description}\n",
            },
        },
        {
            "id": "git_init",
            "kind": "shell",
            "depends_on": ["readme"],
            "payload": {"command": "git -C '${path}' init --quiet && git -C '${path}' status --short", "paths": ["${path}"]},
        },
    ],
    max_parallel=1,
    writes=True,
)

BUNDLED_TEMPLATES: tuple[Template, ...] = (
    GIT_STATUS_REPORT,
    PYTHON_TEST_RUN,
    DISK_SPACE_REPORT,
    LOG_ERROR_SCAN,
    PROJECT_BOOTSTRAP,
)


class TemplateRegistry:
    """Holds the bundled templates and any a caller registers at runtime."""

    def __init__(self, templates: Iterable[Template] | None = None) -> None:
        self._templates: dict[str, Template] = {}
        for template in templates if templates is not None else BUNDLED_TEMPLATES:
            self.register(template)

    def register(self, template: Template) -> Template:
        """Add or replace a template by name."""
        if not template.name:
            raise TemplateError("template needs a name")
        self._templates[template.name] = template
        return template

    def get(self, name: str) -> Template:
        """Look one up, listing the alternatives when the name is wrong."""
        template = self._templates.get(str(name or "").strip())
        if template is None:
            raise TemplateError(
                f"unknown template: {name!r}; available: {sorted(self._templates)}"
            )
        return template

    def names(self) -> list[str]:
        return sorted(self._templates)

    def list(self, *, include_nodes: bool = False) -> list[dict[str, Any]]:
        """Every template, alphabetically, for ``template_list``."""
        return [self._templates[n].to_dict(include_nodes=include_nodes) for n in self.names()]

    def render(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Resolve ``name`` with ``params`` into a workflow payload."""
        return render(self.get(name), params)


def resolve_params(template: Template, given: dict[str, Any] | None) -> dict[str, Any]:
    """Merge caller values over defaults, rejecting missing and unknown names.

    Unknown names are an error rather than being ignored: a typo'd parameter
    that silently does nothing produces a plan that runs and is quietly wrong,
    which is the most expensive kind of wrong.
    """
    given = dict(given or {})
    known = {p.name: p for p in template.params}
    unknown = sorted(set(given) - set(known))
    if unknown:
        raise TemplateError(
            f"template {template.name} has no parameter(s): {', '.join(unknown)}; "
            f"expected: {sorted(known)}"
        )

    resolved: dict[str, Any] = {}
    missing: list[str] = []
    for name, param in known.items():
        if name in given and given[name] is not None:
            resolved[name] = given[name]
        elif param.required:
            missing.append(name)
        else:
            resolved[name] = param.default
    if missing:
        raise TemplateError(
            f"template {template.name} requires parameter(s): {', '.join(sorted(missing))}"
        )
    return resolved


def substitute_params(value: Any, params: dict[str, Any], *, depth: int = 0) -> Any:
    """Replace ``${name}`` throughout a nested structure.

    A reference that is the entire string keeps the parameter's native type,
    so ``"${log_count}"`` stays the integer 10 rather than becoming ``"10"``.
    Unknown ``${...}`` names are left alone — that is how ``${nodes.x.y}``
    survives rendering to be resolved later by the workflow runner.
    """
    if depth > MAX_RENDER_DEPTH:
        raise TemplateError("template nesting is too deep to render")
    if isinstance(value, str):
        whole = _PARAM_RE.fullmatch(value.strip())
        if whole is not None and whole.group(1) in params:
            return params[whole.group(1)]
        return _PARAM_RE.sub(
            lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), value
        )
    if isinstance(value, dict):
        return {k: substitute_params(v, params, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_params(v, params, depth=depth + 1) for v in value]
    return value


def render(template: Template, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fill in ``template`` and return a payload :func:`agent.workflow.parse_workflow` accepts."""
    resolved = resolve_params(template, params)
    nodes = substitute_params(template.nodes, resolved)
    log.debug("rendered template %s with %d node(s)", template.name, len(nodes))
    return {
        "nodes": nodes,
        "max_parallel": template.max_parallel,
        "fail_fast": template.fail_fast,
        "template": template.name,
        "params": resolved,
    }

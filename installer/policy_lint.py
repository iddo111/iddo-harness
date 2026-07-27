"""
Policy sanity linter.

    python -m installer.policy_lint policy.yaml

policy.yaml is the only thing standing between a task packet and the machine, and
it is hand-edited. A typo does not announce itself: an empty ``block:`` list, a
pattern that quietly matches nothing, or the same path appearing in both
``auto_allow`` and ``block`` all leave a policy that loads fine and enforces less
than its author believed.

Exit codes are meant for CI:

    0 — clean
    1 — warnings only (worth a look, not fatal)
    2 — errors (this policy should not be deployed)

On "every pattern must be a valid regex": policy patterns are fnmatch globs, not
regexes — ``PolicyEngine`` matches them with :func:`fnmatch.fnmatchcase`. So the
check is that ``fnmatch.translate(pattern)`` compiles, which is the meaningful
form of the requirement: it catches the character classes and bracket
expressions that fnmatch hands straight to :mod:`re` and that silently fail to
match when unbalanced — including the reversed range that Python 3.13+ rewrites
into a never-matching regex rather than rejecting.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

EXIT_OK = 0
EXIT_WARN = 1
EXIT_ERROR = 2

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

# Patterns broad enough that putting them in an allow list hands over the
# machine. In `block` they are exactly right, which is why the check is
# section-aware.
OVERBROAD_PATTERNS = frozenset({"*", "**", "**/*", "*/*", "/**", "/*", "~/**/*"})

# Paths that should only ever appear under `block.paths.absolute_no_touch`.
PROTECTED_PATHS = (
    "/etc/**",
    "/boot/**",
    "/sys/**",
    "/proc/**",
    "/dev/**",
    "/System/**",
    "C:\\Windows\\**",
    "C:\\Program Files\\**",
)

# Commands that must stay in the block list. A policy that auto-allows any of
# these is a policy that can destroy the host without asking.
DESTRUCTIVE_COMMANDS = ("rm -rf /", "rm -rf /*", "mkfs", "format", "dd if=", "shutdown", "reboot")

# The minimum a block list should contain to be doing its job.
RECOMMENDED_BLOCK_COMMANDS = ("rm -rf /", "mkfs*", "shutdown*")

REQUIRED_SECTIONS = ("auto_allow", "require_confirm", "block")


@dataclass
class Finding:
    """One lint result: where, how bad, and what to do about it."""

    severity: str
    location: str
    message: str
    hint: str = ""

    def format(self) -> str:
        label = "ERROR  " if self.severity == SEVERITY_ERROR else "WARNING"
        line = f"{label} {self.location}: {self.message}"
        if self.hint:
            line += f"\n         → {self.hint}"
        return line


# ---------------------------------------------------------------------------
def _normalise(pattern: str) -> str:
    """Case-fold and unify separators, the way ``_path_matches`` does."""
    return str(pattern).replace("\\", "/").strip().lower()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _walk_patterns(node: Any, prefix: str) -> Iterable[tuple[str, Any]]:
    """Yield ``(location, pattern)`` for every leaf string under ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_patterns(value, f"{prefix}.{key}")
    elif isinstance(node, (list, tuple)):
        for i, value in enumerate(node):
            yield from _walk_patterns(value, f"{prefix}[{i}]")
    elif node is not None:
        yield prefix, node


# ---------------------------------------------------------------------------
def lint_document(doc: Any) -> list[Finding]:
    """Lint an already-parsed policy document."""
    findings: list[Finding] = []

    if not isinstance(doc, dict):
        return [
            Finding(SEVERITY_ERROR, "policy", "top level is not a mapping",
                    "the file should start with `version: 1` and section keys")
        ]

    findings += _check_sections(doc)
    findings += _check_patterns(doc)
    findings += _check_block_list(doc)
    findings += _check_contradictions(doc)
    findings += _check_dangerous(doc)
    findings += _check_settings(doc)
    return findings


def _check_sections(doc: dict) -> list[Finding]:
    findings = []
    for section in REQUIRED_SECTIONS:
        if section not in doc:
            findings.append(
                Finding(SEVERITY_ERROR, section, "section is missing",
                        "PolicyEngine reads this section directly and will raise without it")
            )
        elif not isinstance(doc[section], dict):
            findings.append(
                Finding(SEVERITY_ERROR, section, "section is not a mapping",
                        "expected `commands:` and/or `paths:` keys underneath")
            )
    return findings


def _check_patterns(doc: dict) -> list[Finding]:
    """Every pattern must be a non-empty string that compiles as a glob."""
    findings = []
    for section in REQUIRED_SECTIONS:
        node = doc.get(section)
        if not isinstance(node, dict):
            continue
        for location, pattern in _walk_patterns(node, section):
            if not isinstance(pattern, str):
                findings.append(
                    Finding(SEVERITY_ERROR, location,
                            f"pattern is {type(pattern).__name__}, not a string",
                            "quote it in YAML if it looks like a number or a bool")
                )
                continue
            if not pattern.strip():
                findings.append(
                    Finding(SEVERITY_ERROR, location, "pattern is empty",
                            "an empty pattern matches nothing — delete the entry")
                )
                continue
            try:
                translated = fnmatch.translate(pattern)
                re.compile(translated)
            except re.error as e:
                findings.append(
                    Finding(SEVERITY_ERROR, location,
                            f"pattern does not compile as a glob: {pattern!r} ({e})",
                            "check for an unbalanced [...] character class")
                )
                continue
            # Python 3.13+ rewrites an impossible character range into `(?!)`
            # instead of raising, so a reversed `[9-1]` compiles happily and then
            # matches nothing. Older versions raise above; catch both.
            if "(?!)" in translated:
                findings.append(
                    Finding(SEVERITY_ERROR, location,
                            f"pattern {pattern!r} can never match anything",
                            "a character range is reversed — [1-9], not [9-1]")
                )
    return findings


def _check_block_list(doc: dict) -> list[Finding]:
    """The block list must exist and must not be empty."""
    findings = []
    block = doc.get("block")
    if not isinstance(block, dict):
        return findings

    commands = _as_list(block.get("commands"))
    paths = block.get("paths") or {}
    no_touch = _as_list(paths.get("absolute_no_touch")) if isinstance(paths, dict) else []

    if not commands:
        findings.append(
            Finding(SEVERITY_ERROR, "block.commands", "block list is empty",
                    "with nothing blocked, `rm -rf /` only needs one confirmation to run")
        )
    if not no_touch:
        findings.append(
            Finding(SEVERITY_WARNING, "block.paths.absolute_no_touch", "no protected paths",
                    "consider blocking **/.ssh/id_*, **/.env and **/.aws/credentials")
        )

    have = [_normalise(c) for c in commands if isinstance(c, str)]
    for recommended in RECOMMENDED_BLOCK_COMMANDS:
        want = _normalise(recommended)
        if not any(pat == want or fnmatch.fnmatchcase(want.rstrip("*"), pat) for pat in have):
            findings.append(
                Finding(SEVERITY_WARNING, "block.commands",
                        f"nothing appears to block {recommended!r}",
                        "add it unless it is deliberately allowed")
            )
    return findings


def _group_of(location: str) -> str:
    """The rule group a location belongs to: ``commands``, ``paths.read``, ...

    Two patterns only contradict each other if they govern the same kind of
    rule. ``D:\\CLAUDE\\**`` under ``auto_allow.paths.read`` and under
    ``require_confirm.paths.write`` is the intended design — read freely, ask
    before writing — not a mistake.
    """
    body = location.split(".", 1)[1] if "." in location else location
    return re.sub(r"\[\d+\]$", "", body)


def _check_contradictions(doc: dict) -> list[Finding]:
    """A pattern in two sections at once means one of them never fires."""
    findings = []
    sections: dict[str, dict[tuple[str, str], str]] = {}
    for section in REQUIRED_SECTIONS:
        node = doc.get(section)
        if not isinstance(node, dict):
            continue
        seen: dict[tuple[str, str], str] = {}
        for location, pattern in _walk_patterns(node, section):
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            key = (_group_of(location), _normalise(pattern))
            if key in seen:
                findings.append(
                    Finding(SEVERITY_WARNING, location,
                            f"duplicate pattern {pattern!r} (also at {seen[key]})",
                            "harmless, but one of the two is dead weight")
                )
            else:
                seen[key] = location
        sections[section] = seen

    # block.paths.absolute_no_touch outranks every path rule, whatever its group.
    blocked_paths = {
        pattern: location
        for (group, pattern), location in sections.get("block", {}).items()
        if group.startswith("paths")
    }
    blocked_commands = {
        pattern: location
        for (group, pattern), location in sections.get("block", {}).items()
        if group == "commands"
    }

    for section in ("auto_allow", "require_confirm"):
        for (group, pattern), location in sections.get(section, {}).items():
            # block is evaluated first, so the auto/confirm entry is dead —
            # and the author clearly believed otherwise.
            clash = blocked_commands.get(pattern) if group == "commands" else blocked_paths.get(pattern)
            if clash:
                findings.append(
                    Finding(SEVERITY_ERROR, location,
                            f"pattern also appears in {clash}",
                            "block wins (it is checked first), so this entry never applies")
                )

    auto = sections.get("auto_allow", {})
    for key, location in sections.get("require_confirm", {}).items():
        if key in auto:
            findings.append(
                Finding(SEVERITY_WARNING, location,
                        f"pattern also appears in {auto[key]}",
                        "require_confirm is checked before auto_allow, so this one wins")
            )
    return findings


def _check_dangerous(doc: dict) -> list[Finding]:
    """Flag patterns that are only safe inside the block section."""
    findings = []
    protected = {_normalise(p) for p in PROTECTED_PATHS}

    for section in ("auto_allow", "require_confirm"):
        node = doc.get(section)
        if not isinstance(node, dict):
            continue
        for location, pattern in _walk_patterns(node, section):
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            key = _normalise(pattern)
            if key in OVERBROAD_PATTERNS:
                findings.append(
                    Finding(SEVERITY_WARNING, location,
                            f"pattern {pattern!r} matches everything",
                            f"in {section} this grants the whole filesystem — narrow it to a root")
                )
            if key in protected:
                findings.append(
                    Finding(SEVERITY_ERROR, location,
                            f"system path {pattern!r} is reachable from {section}",
                            "system paths belong in block.paths.absolute_no_touch")
                )
            if section == "auto_allow":
                for destructive in DESTRUCTIVE_COMMANDS:
                    if fnmatch.fnmatchcase(_normalise(destructive), key):
                        findings.append(
                            Finding(SEVERITY_ERROR, location,
                                    f"pattern {pattern!r} auto-allows {destructive!r}",
                                    "this would run without any confirmation")
                        )

    block = doc.get("block")
    if isinstance(block, dict):
        no_touch = {_normalise(p) for p in _as_list((block.get("paths") or {}).get("absolute_no_touch"))
                    if isinstance(p, str)} if isinstance(block.get("paths"), dict) else set()
        for missing in protected - no_touch:
            if missing in {"/sys/**", "/proc/**", "/dev/**"}:
                continue  # rarely listed, and reads there are usually harmless
            findings.append(
                Finding(SEVERITY_WARNING, "block.paths.absolute_no_touch",
                        f"{missing} is not protected",
                        "a task can reach it with a single confirmation")
            )
    return findings


def _check_settings(doc: dict) -> list[Finding]:
    """Sanity-check the numeric knobs outside the three rule sections."""
    findings = []

    polling = doc.get("polling")
    if isinstance(polling, dict):
        interval = polling.get("interval_seconds")
        if isinstance(interval, (int, float)) and interval <= 0:
            findings.append(
                Finding(SEVERITY_ERROR, "polling.interval_seconds",
                        f"must be positive, got {interval}",
                        "a zero interval busy-loops against the transport")
            )
        concurrent = polling.get("max_concurrent_tasks")
        if isinstance(concurrent, int) and concurrent < 1:
            findings.append(
                Finding(SEVERITY_ERROR, "polling.max_concurrent_tasks",
                        f"must be at least 1, got {concurrent}",
                        "no task would ever run")
            )

    approval = doc.get("approval")
    if isinstance(approval, dict):
        mode = approval.get("mode")
        if mode is not None and str(mode).lower() not in ("local", "notification", "remote"):
            findings.append(
                Finding(SEVERITY_ERROR, "approval.mode", f"unknown mode {mode!r}",
                        "expected one of: local, notification, remote")
            )
        timeout = approval.get("timeout_seconds")
        if isinstance(timeout, (int, float)) and timeout <= 0:
            findings.append(
                Finding(SEVERITY_ERROR, "approval.timeout_seconds",
                        f"must be positive, got {timeout}",
                        "every approval would auto-deny immediately")
            )

    sandbox = doc.get("sandbox")
    if isinstance(sandbox, dict):
        levels = ("none", "light", "strict")
        default = sandbox.get("default")
        if default is not None and str(default).lower() not in levels:
            findings.append(
                Finding(SEVERITY_ERROR, "sandbox.default", f"unknown level {default!r}",
                        f"expected one of: {', '.join(levels)}")
            )
        per_kind = sandbox.get("per_kind")
        if isinstance(per_kind, dict):
            for kind, level in per_kind.items():
                if str(level).lower() not in levels:
                    findings.append(
                        Finding(SEVERITY_ERROR, f"sandbox.per_kind.{kind}",
                                f"unknown level {level!r}",
                                f"expected one of: {', '.join(levels)}")
                    )

    health = doc.get("health")
    if isinstance(health, dict) and health.get("enabled"):
        host = str(health.get("host", "127.0.0.1"))
        if host not in ("127.0.0.1", "localhost", "::1"):
            findings.append(
                Finding(SEVERITY_WARNING, "health.host",
                        f"endpoint bound to {host!r}, not loopback",
                        "/audit/tail and /policy expose what the agent has done — keep it local")
            )
    return findings


# ---------------------------------------------------------------------------
def lint_file(path: Path | str) -> list[Finding]:
    """Parse and lint a policy file. Parse failures come back as one error."""
    p = Path(path)
    if not p.exists():
        return [Finding(SEVERITY_ERROR, str(p), "file not found")]
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a hard dependency
        return [Finding(SEVERITY_ERROR, str(p), "PyYAML is not installed",
                        "pip install -r requirements.txt")]
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as e:
        return [Finding(SEVERITY_ERROR, str(p), f"YAML does not parse: {e}")]
    return lint_document(doc)


def exit_code_for(findings: Iterable[Finding]) -> int:
    """Worst severity present, as a process exit code."""
    codes = [EXIT_OK]
    for f in findings:
        codes.append(EXIT_ERROR if f.severity == SEVERITY_ERROR else EXIT_WARN)
    return max(codes)


def default_policy_path() -> Path:
    """Where to look when the caller names no file."""
    for candidate in (
        Path.cwd() / "policy.yaml",
        Path.home() / ".iddo-harness" / "policy.yaml",
        Path(__file__).parent.parent / "policy.yaml",
    ):
        if candidate.exists():
            return candidate
    return Path("policy.yaml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m installer.policy_lint",
        description="Check policy.yaml for empty block lists, bad patterns and contradictions.",
    )
    parser.add_argument("policy", nargs="?", help="path to policy.yaml (default: ./policy.yaml)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="print errors only, suppress warnings")
    parser.add_argument("--strict", action="store_true",
                        help="treat warnings as errors (exit 2)")
    args = parser.parse_args(argv)

    path = Path(args.policy) if args.policy else default_policy_path()
    findings = lint_file(path)

    errors = [f for f in findings if f.severity == SEVERITY_ERROR]
    warnings = [f for f in findings if f.severity == SEVERITY_WARNING]

    shown = errors if args.quiet else errors + warnings
    for finding in shown:
        print(finding.format())

    print(f"\n{path}: {len(errors)} error(s), {len(warnings)} warning(s)")
    if not errors and not warnings:
        print("policy looks sane")

    if args.strict and warnings:
        return EXIT_ERROR
    return exit_code_for(findings)


if __name__ == "__main__":
    sys.exit(main())

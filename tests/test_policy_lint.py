"""
Tests for installer/policy_lint.py.

The linter's job is to fail loudly on a policy that *loads* fine but enforces
less than its author believed. So most tests here take a deliberately clean
document, break one thing in it, and assert both the finding and the exit code.

The shipped ``policy.yaml`` is also linted, because a linter that flags the
project's own policy is a linter nobody will run.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from installer import policy_lint
from installer.policy_lint import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_WARN,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    exit_code_for,
    lint_document,
    lint_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# A document the linter has nothing to say about. Every test that wants a
# specific finding starts from this and breaks exactly one thing.
CLEAN = {
    "version": 1,
    "owner": "test",
    "auto_allow": {
        "commands": ["ls*", "pwd", "git status"],
        "paths": {"read": ["~/projects/**"]},
    },
    "require_confirm": {
        "commands": ["pip install*", "git push*"],
        "paths": {"write": ["~/projects/**"]},
    },
    "block": {
        "commands": ["rm -rf /", "rm -rf /*", "mkfs*", "shutdown*", "format*"],
        "paths": {
            "absolute_no_touch": [
                "/etc/**",
                "/boot/**",
                "/System/**",
                "C:\\Windows\\**",
                "C:\\Program Files\\**",
                "**/.ssh/id_*",
                "**/.env",
            ]
        },
    },
}


@pytest.fixture
def doc():
    return copy.deepcopy(CLEAN)


def _messages(findings) -> str:
    return "\n".join(f.format() for f in findings)


def _errors(findings):
    return [f for f in findings if f.severity == SEVERITY_ERROR]


def _warnings(findings):
    return [f for f in findings if f.severity == SEVERITY_WARNING]


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------
def test_a_sane_policy_produces_no_findings(doc):
    assert lint_document(doc) == []


def test_the_shipped_policy_lints_clean():
    """If the project's own policy does not pass, nobody will run the linter."""
    findings = lint_file(REPO_ROOT / "policy.yaml")
    assert findings == [], _messages(findings)


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def test_a_non_mapping_document_is_an_error():
    findings = lint_document(["not", "a", "mapping"])
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_ERROR


@pytest.mark.parametrize("section", ["auto_allow", "require_confirm", "block"])
def test_a_missing_section_is_an_error(doc, section):
    del doc[section]
    assert any(f.location == section and "missing" in f.message for f in _errors(lint_document(doc)))


def test_a_section_that_is_a_list_is_an_error(doc):
    doc["auto_allow"] = ["ls*"]
    assert any("not a mapping" in f.message for f in _errors(lint_document(doc)))


# ---------------------------------------------------------------------------
# The block list must not be empty
# ---------------------------------------------------------------------------
def test_an_empty_block_command_list_is_an_error(doc):
    doc["block"]["commands"] = []
    errors = _errors(lint_document(doc))
    assert any(f.location == "block.commands" and "empty" in f.message for f in errors)


def test_an_empty_block_list_fails_the_exit_code(doc):
    doc["block"]["commands"] = []
    assert exit_code_for(lint_document(doc)) == EXIT_ERROR


def test_a_missing_block_commands_key_is_an_error(doc):
    del doc["block"]["commands"]
    assert any(f.location == "block.commands" for f in _errors(lint_document(doc)))


def test_no_protected_paths_is_a_warning(doc):
    doc["block"]["paths"] = {"absolute_no_touch": []}
    findings = lint_document(doc)
    assert any(f.location == "block.paths.absolute_no_touch" for f in _warnings(findings))
    assert _errors(findings) == []


def test_dropping_rm_rf_root_from_the_block_list_warns(doc):
    doc["block"]["commands"] = ["mkfs*", "shutdown*"]
    assert any("rm -rf /" in f.message for f in _warnings(lint_document(doc)))


def test_an_unprotected_system_path_warns(doc):
    doc["block"]["paths"]["absolute_no_touch"].remove("/etc/**")
    assert any("/etc/**" in f.message for f in _warnings(lint_document(doc)))


# ---------------------------------------------------------------------------
# Patterns must compile
# ---------------------------------------------------------------------------
def test_a_reversed_character_range_is_an_error(doc):
    """`[9-6]` compiles on 3.13+ and raises on older versions — both are errors."""
    doc["auto_allow"]["commands"].append("http_local * http://172.1[9-6].*")
    errors = _errors(lint_document(doc))
    assert any(
        "does not compile" in f.message or "never match" in f.message for f in errors
    ), _messages(errors)


def test_a_valid_character_class_is_accepted(doc):
    doc["auto_allow"]["commands"].append("http_local * http://172.1[6-9].*")
    assert lint_document(doc) == []


def test_an_empty_pattern_is_an_error(doc):
    doc["auto_allow"]["commands"].append("   ")
    assert any("empty" in f.message for f in _errors(lint_document(doc)))


def test_a_non_string_pattern_is_an_error(doc):
    doc["require_confirm"]["commands"].append(42)
    assert any("not a string" in f.message for f in _errors(lint_document(doc)))


def test_the_location_names_the_offending_index(doc):
    doc["auto_allow"]["commands"] = ["ls*", ""]
    assert any(f.location == "auto_allow.commands[1]" for f in _errors(lint_document(doc)))


# ---------------------------------------------------------------------------
# Contradictions
# ---------------------------------------------------------------------------
def test_a_pattern_in_both_auto_allow_and_block_is_an_error(doc):
    doc["auto_allow"]["commands"].append("shutdown*")
    errors = _errors(lint_document(doc))
    assert any("also appears in block.commands" in f.message for f in errors)


def test_a_blocked_path_reappearing_under_auto_allow_is_an_error(doc):
    doc["auto_allow"]["paths"]["read"].append("**/.env")
    assert any("also appears in block" in f.message for f in _errors(lint_document(doc)))


def test_a_pattern_in_both_confirm_and_auto_allow_is_a_warning(doc):
    doc["require_confirm"]["commands"].append("ls*")
    findings = lint_document(doc)
    assert any("also appears in auto_allow.commands" in f.message for f in _warnings(findings))
    assert _errors(findings) == []


def test_a_duplicate_within_one_section_is_a_warning(doc):
    doc["auto_allow"]["commands"].append("ls*")
    assert any("duplicate" in f.message for f in _warnings(lint_document(doc)))


def test_read_here_and_write_there_is_not_a_contradiction(doc):
    """Read freely, ask before writing, is the intended design — not a mistake."""
    doc["auto_allow"]["paths"]["read"] = ["D:\\CLAUDE\\**"]
    doc["require_confirm"]["paths"]["write"] = ["D:\\CLAUDE\\**"]
    assert lint_document(doc) == []


def test_separator_style_does_not_hide_a_contradiction(doc):
    doc["block"]["paths"]["absolute_no_touch"].append("D:/secrets/**")
    doc["auto_allow"]["paths"]["read"].append("D:\\secrets\\**")
    assert any("also appears in block" in f.message for f in _errors(lint_document(doc)))


# ---------------------------------------------------------------------------
# Dangerous patterns
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("pattern", ["**/*", "*", "/**"])
def test_an_overbroad_allow_pattern_warns(doc, pattern):
    doc["auto_allow"]["paths"]["read"].append(pattern)
    findings = lint_document(doc)
    assert any("matches everything" in f.message for f in _warnings(findings))


def test_an_overbroad_pattern_is_fine_inside_block(doc):
    doc["block"]["paths"]["absolute_no_touch"].append("**/*")
    assert not any("matches everything" in f.message for f in lint_document(doc))


@pytest.mark.parametrize("pattern", ["/etc/**", "C:\\Windows\\**"])
def test_a_system_path_reachable_from_auto_allow_is_an_error(doc, pattern):
    doc["auto_allow"]["paths"]["read"].append(pattern)
    assert any("system path" in f.message for f in _errors(lint_document(doc)))


def test_a_system_path_under_require_confirm_is_also_an_error(doc):
    doc["require_confirm"]["paths"]["write"].append("/boot/**")
    assert any("system path" in f.message for f in _errors(lint_document(doc)))


def test_auto_allowing_a_destructive_command_is_an_error(doc):
    doc["auto_allow"]["commands"].append("rm -rf*")
    errors = _errors(lint_document(doc))
    assert any("auto-allows" in f.message for f in errors)


def test_a_wildcard_that_swallows_shutdown_is_caught(doc):
    doc["auto_allow"]["commands"].append("shut*")
    assert any("auto-allows" in f.message for f in _errors(lint_document(doc)))


def test_confirming_a_destructive_command_is_not_an_error(doc):
    """A confirmation prompt is the point — only silent auto-allow is fatal."""
    doc["require_confirm"]["commands"].append("shutdown -h now")
    assert not any("auto-allows" in f.message for f in lint_document(doc))


# ---------------------------------------------------------------------------
# Settings outside the rule sections
# ---------------------------------------------------------------------------
def test_a_zero_polling_interval_is_an_error(doc):
    doc["polling"] = {"interval_seconds": 0, "max_concurrent_tasks": 3}
    assert any(f.location == "polling.interval_seconds" for f in _errors(lint_document(doc)))


def test_zero_concurrency_is_an_error(doc):
    doc["polling"] = {"interval_seconds": 5, "max_concurrent_tasks": 0}
    assert any(f.location == "polling.max_concurrent_tasks" for f in _errors(lint_document(doc)))


def test_sane_polling_settings_pass(doc):
    doc["polling"] = {"interval_seconds": 5, "max_concurrent_tasks": 3}
    assert lint_document(doc) == []


def test_an_unknown_approval_mode_is_an_error(doc):
    doc["approval"] = {"mode": "telepathy"}
    assert any(f.location == "approval.mode" for f in _errors(lint_document(doc)))


def test_the_three_approval_modes_are_accepted(doc):
    for mode in ("local", "notification", "remote"):
        doc["approval"] = {"mode": mode, "timeout_seconds": 300}
        assert lint_document(doc) == []


def test_a_non_positive_approval_timeout_is_an_error(doc):
    doc["approval"] = {"mode": "local", "timeout_seconds": 0}
    assert any(f.location == "approval.timeout_seconds" for f in _errors(lint_document(doc)))


def test_an_unknown_sandbox_level_is_an_error(doc):
    doc["sandbox"] = {"enabled": True, "default": "paranoid"}
    assert any(f.location == "sandbox.default" for f in _errors(lint_document(doc)))


def test_an_unknown_per_kind_sandbox_level_names_the_kind(doc):
    doc["sandbox"] = {"enabled": True, "default": "none", "per_kind": {"shell": "hard"}}
    assert any(f.location == "sandbox.per_kind.shell" for f in _errors(lint_document(doc)))


def test_a_health_endpoint_on_a_public_interface_warns(doc):
    doc["health"] = {"enabled": True, "host": "0.0.0.0", "port": 8478}
    assert any(f.location == "health.host" for f in _warnings(lint_document(doc)))


def test_a_loopback_health_endpoint_is_fine(doc):
    doc["health"] = {"enabled": True, "host": "127.0.0.1", "port": 8478}
    assert lint_document(doc) == []


def test_a_disabled_health_endpoint_is_not_checked(doc):
    doc["health"] = {"enabled": False, "host": "0.0.0.0"}
    assert lint_document(doc) == []


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
def test_exit_code_is_zero_without_findings():
    assert exit_code_for([]) == EXIT_OK


def test_exit_code_is_one_for_warnings_only(doc):
    doc["block"]["paths"]["absolute_no_touch"] = []
    assert exit_code_for(lint_document(doc)) == EXIT_WARN


def test_an_error_outranks_a_warning(doc):
    doc["block"]["commands"] = []
    doc["block"]["paths"]["absolute_no_touch"] = []
    assert exit_code_for(lint_document(doc)) == EXIT_ERROR


# ---------------------------------------------------------------------------
# File handling and the CLI
# ---------------------------------------------------------------------------
def _write(tmp_path: Path, document) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_a_missing_file_is_an_error(tmp_path):
    findings = lint_file(tmp_path / "absent.yaml")
    assert len(findings) == 1
    assert "not found" in findings[0].message


def test_unparseable_yaml_is_one_error(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("block:\n  commands:\n   - x\n  - broken\n", encoding="utf-8")
    findings = lint_file(path)
    assert len(findings) == 1
    assert "does not parse" in findings[0].message


def test_cli_exits_zero_on_a_clean_policy(tmp_path, doc, capsys):
    assert policy_lint.main([str(_write(tmp_path, doc))]) == EXIT_OK
    assert "policy looks sane" in capsys.readouterr().out


def test_cli_exits_two_on_an_empty_block_list(tmp_path, doc, capsys):
    doc["block"]["commands"] = []
    assert policy_lint.main([str(_write(tmp_path, doc))]) == EXIT_ERROR
    assert "block list is empty" in capsys.readouterr().out


def test_cli_exits_one_on_warnings_only(tmp_path, doc):
    doc["block"]["paths"]["absolute_no_touch"] = []
    assert policy_lint.main([str(_write(tmp_path, doc))]) == EXIT_WARN


def test_strict_promotes_a_warning_to_an_error(tmp_path, doc):
    doc["block"]["paths"]["absolute_no_touch"] = []
    path = str(_write(tmp_path, doc))
    assert policy_lint.main([path]) == EXIT_WARN
    assert policy_lint.main([path, "--strict"]) == EXIT_ERROR


def test_quiet_suppresses_warnings_but_not_errors(tmp_path, doc, capsys):
    doc["block"]["commands"] = []
    doc["block"]["paths"]["absolute_no_touch"] = []
    policy_lint.main([str(_write(tmp_path, doc)), "--quiet"])
    out = capsys.readouterr().out
    assert "block list is empty" in out
    assert "no protected paths" not in out


def test_cli_prints_a_summary_count(tmp_path, doc, capsys):
    doc["block"]["commands"] = []
    policy_lint.main([str(_write(tmp_path, doc))])
    assert "error(s)" in capsys.readouterr().out


def test_cli_falls_back_to_the_default_path(tmp_path, doc, monkeypatch, capsys):
    _write(tmp_path, doc)
    monkeypatch.chdir(tmp_path)
    assert policy_lint.main([]) == EXIT_OK


def test_default_policy_path_prefers_the_cwd(tmp_path, doc, monkeypatch):
    path = _write(tmp_path, doc)
    monkeypatch.chdir(tmp_path)
    assert policy_lint.default_policy_path() == path


def test_a_finding_formats_with_its_hint():
    f = policy_lint.Finding(SEVERITY_ERROR, "block.commands", "block list is empty", "add rm -rf /")
    text = f.format()
    assert text.startswith("ERROR")
    assert "block.commands" in text
    assert "add rm -rf /" in text


def test_a_warning_formats_with_its_label():
    assert policy_lint.Finding(SEVERITY_WARNING, "x", "y").format().startswith("WARNING")

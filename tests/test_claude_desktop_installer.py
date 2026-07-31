from __future__ import annotations

import json
from pathlib import Path

import pytest

from installer.claude_desktop import default_config_path, install, merged_config, server_entry


def test_default_path_uses_claude_appdata_directory(tmp_path):
    assert default_config_path({"APPDATA": str(tmp_path)}) == (
        tmp_path / "Claude" / "claude_desktop_config.json"
    )


def test_server_entry_uses_module_stdio_and_current_python():
    entry = server_entry(python="python-test", source_root="C:/harness-test")
    assert entry["command"] == "python-test"
    assert entry["args"] == [
        "-m", "agent.mcp_server", "--transport", "stdio",
        "--config", str(Path("C:/harness-test/policy.yaml").resolve()),
        "--runtime-config", str(Path("C:/harness-test/config.yaml").resolve()),
    ]
    assert entry["env"]["PYTHONPATH"] == str(Path("C:/harness-test").resolve())


def test_merge_preserves_other_servers_and_settings():
    result = merged_config(
        {"theme": "dark", "mcpServers": {"other": {"command": "other"}}},
        {"command": "python"},
    )
    assert result["theme"] == "dark"
    assert result["mcpServers"]["other"]["command"] == "other"
    assert result["mcpServers"]["iddo-harness"]["command"] == "python"


def test_dry_run_does_not_write(tmp_path):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    returned, rendered = install(path=path, dry_run=True, python="python-test")
    assert returned == path
    assert not path.exists()
    assert json.loads(rendered)["mcpServers"]["iddo-harness"]["command"] == "python-test"


def test_install_writes_valid_merged_json(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": {"existing": {"command": "x"}}}), encoding="utf-8")
    install(path=path, python="python-test")
    body = json.loads(path.read_text(encoding="utf-8"))
    assert set(body["mcpServers"]) == {"existing", "iddo-harness"}


def test_installer_refuses_invalid_existing_json(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid JSON"):
        install(path=path)

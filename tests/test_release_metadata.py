"""M0-19: the plugin manifest, its bundled MCP server pin, the marketplace entry and
pyproject.toml must all agree on the version, and the marketplace must point at the
plugin directory in this repo.
"""

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugin"


def _pyproject_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    version: str = data["project"]["version"]
    return version


def _plugin_manifest() -> dict:
    return json.loads((PLUGIN_DIR / ".claude-plugin" / "plugin.json").read_text())


def _plugin_mcp_config() -> dict:
    return json.loads((PLUGIN_DIR / ".mcp.json").read_text())


def _marketplace() -> dict:
    return json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())


def test_json_files_parse() -> None:
    assert _plugin_manifest()["name"] == "chipgraph"
    assert "mcpServers" in _plugin_mcp_config()
    assert "plugins" in _marketplace()


def test_plugin_manifest_version_matches_pyproject() -> None:
    assert _plugin_manifest()["version"] == _pyproject_version()


def test_mcp_server_command_pins_the_same_version() -> None:
    version = _pyproject_version()
    server = _plugin_mcp_config()["mcpServers"]["chipgraph"]
    args = server["args"]

    pinned = None
    for arg in args:
        m = re.fullmatch(r"chipgraph(?:@|==)([^\s]+)", arg)
        if m:
            pinned = m.group(1)
            break

    assert pinned is not None, f"no chipgraph@<version> or chipgraph==<version> in {args!r}"
    assert pinned == version


def test_marketplace_entry_points_at_the_plugin_directory() -> None:
    entries = _marketplace()["plugins"]
    chipgraph_entries = [e for e in entries if e["name"] == "chipgraph"]
    assert len(chipgraph_entries) == 1
    assert chipgraph_entries[0]["source"] == "./plugin"


def test_marketplace_entry_version_matches_when_present() -> None:
    entries = _marketplace()["plugins"]
    entry = next(e for e in entries if e["name"] == "chipgraph")
    if "version" in entry:
        assert entry["version"] == _pyproject_version()

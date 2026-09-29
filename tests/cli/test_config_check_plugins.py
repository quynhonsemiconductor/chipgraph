"""`chipgraph config check` lists local plugins (text and JSON): loaded and disabled."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from conftest import init_git, write_profile
from typer.testing import CliRunner

from chipgraph.cli import app

runner = CliRunner()

_FIXTURES = Path(__file__).parents[1] / "core" / "plugin_api" / "local_plugin_fixtures"


def _install_tiny_layout(root: Path) -> None:
    dest = root / ".chipgraph" / "plugins" / "tiny_layout.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_FIXTURES / "tiny_layout.py", dest)


def test_config_check_lists_loaded_plugin_text(tmp_path: Path) -> None:
    init_git(tmp_path)
    _install_tiny_layout(tmp_path)
    write_profile(tmp_path, "project: demo\nplugins: ['.chipgraph/plugins/tiny_layout.py']\n")

    result = runner.invoke(app, ["-C", str(tmp_path), "config", "check"])

    assert result.exit_code == 0, result.output
    assert "plugins[0]" in result.output
    assert ".chipgraph/plugins/tiny_layout.py" in result.output
    assert "sha256" in result.output
    assert "registered check/tiny_layout" in result.output
    assert "review it like code" in result.output


def test_config_check_lists_loaded_plugin_json(tmp_path: Path) -> None:
    init_git(tmp_path)
    _install_tiny_layout(tmp_path)
    write_profile(tmp_path, "project: demo\nplugins: ['.chipgraph/plugins/tiny_layout.py']\n")

    result = runner.invoke(app, ["-C", str(tmp_path), "--json", "config", "check"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    entry = next(i for i in payload if i["key"] == "plugins[0]")
    assert entry["severity"] == "info"
    assert "check/tiny_layout" in entry["message"]
    assert "tiny_layout.py" in entry["message"]


def test_config_check_warns_when_disabled_by_org(tmp_path: Path) -> None:
    init_git(tmp_path)
    _install_tiny_layout(tmp_path)
    org = tmp_path / "rules" / "org.yml"
    org.parent.mkdir(parents=True, exist_ok=True)
    org.write_text("project: org\npolicy: { local_plugins: deny }\n", encoding="utf-8")
    write_profile(
        tmp_path,
        "project: demo\n"
        "extends: [path:rules/org.yml]\n"
        "plugins: ['.chipgraph/plugins/tiny_layout.py']\n",
    )

    result = runner.invoke(app, ["-C", str(tmp_path), "config", "check"])

    assert result.exit_code == 0, result.output
    assert "warning plugins[0]" in result.output
    assert "not loaded" in result.output
    assert str(org) in result.output


def test_config_check_warns_when_disabled_by_env(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    init_git(tmp_path)
    _install_tiny_layout(tmp_path)
    write_profile(tmp_path, "project: demo\nplugins: ['.chipgraph/plugins/tiny_layout.py']\n")
    monkeypatch.setenv("CHIPGRAPH_LOCAL_PLUGINS", "0")

    result = runner.invoke(app, ["-C", str(tmp_path), "config", "check"])

    assert result.exit_code == 0, result.output
    assert "warning plugins[0]" in result.output
    assert "CHIPGRAPH_LOCAL_PLUGINS=0" in result.output

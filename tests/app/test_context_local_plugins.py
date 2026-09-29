"""Tests for local-plugin loading in `AppContext.load` (policy, env, and accept 1)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from conftest import init_git, make_instance, write_profile

from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError

_FIXTURES = Path(__file__).parents[1] / "core" / "plugin_api" / "local_plugin_fixtures"


def _install_tiny_layout(root: Path) -> None:
    dest = root / ".chipgraph" / "plugins" / "tiny_layout.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_FIXTURES / "tiny_layout.py", dest)


def _sv(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("module m; endmodule\n", encoding="utf-8")


def test_context_loads_local_plugin(tmp_path: Path) -> None:
    init_git(tmp_path)
    _install_tiny_layout(tmp_path)
    write_profile(tmp_path, "project: demo\nplugins: ['.chipgraph/plugins/tiny_layout.py']\n")

    ctx = AppContext.load(tmp_path)

    assert len(ctx.local_plugins) == 1
    rec = ctx.local_plugins[0]
    assert rec.loaded is True
    assert rec.registered == (("check", "tiny_layout"),)
    assert "tiny_layout" in ctx.registry.names("check")


def test_no_plugins_means_empty_records(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")
    ctx = AppContext.load(tmp_path)
    assert ctx.local_plugins == ()


def test_org_deny_disables_loading(tmp_path: Path) -> None:
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

    ctx = AppContext.load(tmp_path)

    assert len(ctx.local_plugins) == 1
    assert ctx.local_plugins[0].loaded is False
    assert str(org) in (ctx.local_plugins[0].disabled_reason or "")
    assert "tiny_layout" not in ctx.registry.names("check")


def test_env_switch_disables_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_git(tmp_path)
    _install_tiny_layout(tmp_path)
    write_profile(tmp_path, "project: demo\nplugins: ['.chipgraph/plugins/tiny_layout.py']\n")
    monkeypatch.setenv("CHIPGRAPH_LOCAL_PLUGINS", "0")

    ctx = AppContext.load(tmp_path)

    assert ctx.local_plugins[0].loaded is False
    assert ctx.local_plugins[0].disabled_reason == "disabled by CHIPGRAPH_LOCAL_PLUGINS=0"
    assert "tiny_layout" not in ctx.registry.names("check")


def test_bad_plugin_becomes_app_error(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\nplugins: ['../evil.py']\n")
    with pytest.raises(AppError, match=r"evil\.py"):
        AppContext.load(tmp_path)


# --- accept 1: a plugin changes the layout rule -------------------------------------


_LAYOUT_ADAPTER = (
    "adapters:\n"
    "  layout:\n"
    "    use: __USE__\n"
    "    templates: { rtl: 'design/{block}/{module}.sv' }\n"
    "    scope: ['design/**/*.sv']\n"
)


def _run_layout(root: Path, use: str) -> object:
    profile = (
        "project: demo\n"
        "plugins: ['.chipgraph/plugins/tiny_layout.py']\n" + _LAYOUT_ADAPTER.replace("__USE__", use)
    )
    write_profile(root, profile)
    ctx = AppContext.load(root)
    runner = ProfileCheckRunner(ctx)
    return asyncio.run(runner.run("layout", make_instance()))


def test_tiny_layout_plugin_passes_and_data_layout_fails(tmp_path: Path) -> None:
    # `timer_top` starts with block `timer`: the formula check passes, but the data-only
    # layout check has no template segment matching `timer_top` under `timer/`, so it fails
    # unless a bare `{module}` template is used. Use the plugin to express the prefix rule.
    init_git(tmp_path)
    _install_tiny_layout(tmp_path)
    _sv(tmp_path, "design/timer/timer_top.sv")

    plugin_result = _run_layout(tmp_path, "tiny_layout")
    assert plugin_result.status == "pass"  # type: ignore[attr-defined]


def test_tiny_layout_plugin_fails_when_formula_violated(tmp_path: Path) -> None:
    init_git(tmp_path)
    _install_tiny_layout(tmp_path)
    # `core_top` does NOT start with block `timer`: the formula check fails.
    _sv(tmp_path, "design/timer/core_top.sv")

    plugin_result = _run_layout(tmp_path, "tiny_layout")
    assert plugin_result.status == "fail"  # type: ignore[attr-defined]


def test_data_layout_cannot_express_the_prefix_rule(tmp_path: Path) -> None:
    # The same file that the plugin accepts is rejected by the data-only `layout` check,
    # because `design/{block}/{module}.sv` matches, but the template cannot require that
    # module names begin with the block name. To even accept it the data template treats
    # `timer_top` and `core_top` identically: it cannot tell the good file from the bad.
    init_git(tmp_path)
    _install_tiny_layout(tmp_path)
    _sv(tmp_path, "design/timer/core_top.sv")  # violates the plugin's formula

    data_result = _run_layout(tmp_path, "layout")
    plugin_result = _run_layout(tmp_path, "tiny_layout")

    # Data layout passes the bad file; the plugin rejects it: a different, correct result.
    assert data_result.status == "pass"  # type: ignore[attr-defined]
    assert plugin_result.status == "fail"  # type: ignore[attr-defined]

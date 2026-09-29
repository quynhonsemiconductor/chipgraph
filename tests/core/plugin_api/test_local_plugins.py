"""Tests for `chipgraph.core.plugin_api.local`: loading a project's own plugins safely."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from chipgraph.core.contracts import CheckSpec
from chipgraph.core.plugin_api.local import load_local_plugins
from chipgraph.core.plugin_api.registry import PluginError, Registry
from chipgraph.core.plugin_api.types import ToolContext

_FIXTURES = Path(__file__).parent / "local_plugin_fixtures"


class _NoRunner:
    name = "no-runner"

    async def run(self, cmd, *, cwd, env=None, timeout_s=None):  # type: ignore[no-untyped-def]
        raise AssertionError("not used")


def _plugins_dir(root: Path) -> Path:
    d = root / ".chipgraph" / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _install(root: Path, fixture: str, dest: str | None = None) -> str:
    """Copy a fixture into `root/.chipgraph/plugins/` and return its repo-relative path."""
    rel = dest or f".chipgraph/plugins/{fixture}"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_FIXTURES / fixture, target)
    return rel


def _write(root: Path, rel: str, content: str) -> str:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return rel


# --- happy path ---------------------------------------------------------------------


def test_load_registers_and_records(tmp_path: Path) -> None:
    rel = _install(tmp_path, "tiny_layout.py")
    registry = Registry()

    records = load_local_plugins(tmp_path, (rel,), registry, enabled=True)

    assert len(records) == 1
    rec = records[0]
    assert rec.loaded is True
    assert rec.path == ".chipgraph/plugins/tiny_layout.py"
    assert rec.sha256 and len(rec.sha256) == 64
    assert rec.registered == (("check", "tiny_layout"),)
    assert "tiny_layout" in registry.names("check")


def test_records_follow_profile_order(tmp_path: Path) -> None:
    a = _write(tmp_path, ".chipgraph/plugins/a.py", _REGISTER_TEMPLATE.format(name="chk_a"))
    b = _write(tmp_path, ".chipgraph/plugins/b.py", _REGISTER_TEMPLATE.format(name="chk_b"))
    registry = Registry()

    records = load_local_plugins(tmp_path, (b, a), registry, enabled=True)

    assert [r.registered[0][1] for r in records] == ["chk_b", "chk_a"]


def test_class_is_instantiated_and_usable(tmp_path: Path) -> None:
    rel = _install(tmp_path, "tiny_layout.py")
    (tmp_path / "design" / "timer").mkdir(parents=True)
    (tmp_path / "design" / "timer" / "timer_top.sv").write_text("module x; endmodule\n")
    registry = Registry()

    load_local_plugins(tmp_path, (rel,), registry, enabled=True)
    check = registry.get("check", "tiny_layout")

    spec = CheckSpec(id="tiny_layout", capability="layout", adapter="tiny_layout", args={})
    ctx = ToolContext(repo_root=tmp_path, runner=_NoRunner())
    result = asyncio.run(check.run(spec, ctx))
    assert result.status == "pass"


# --- disabled -----------------------------------------------------------------------


def test_disabled_loads_nothing(tmp_path: Path) -> None:
    rel = _install(tmp_path, "tiny_layout.py")
    registry = Registry()

    records = load_local_plugins(
        tmp_path, (rel,), registry, enabled=False, disabled_reason="disabled by org"
    )

    assert len(records) == 1
    assert records[0].loaded is False
    assert records[0].disabled_reason == "disabled by org"
    assert records[0].path is None
    assert registry.names("check") == ()


# --- security edges -----------------------------------------------------------------


def test_absolute_path_rejected(tmp_path: Path) -> None:
    rel = _install(tmp_path, "tiny_layout.py")
    absolute = str((tmp_path / rel).resolve())
    with pytest.raises(PluginError, match="absolute"):
        load_local_plugins(tmp_path, (absolute,), Registry(), enabled=True)


def test_parent_escape_rejected(tmp_path: Path) -> None:
    _plugins_dir(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("def register(api):\n    pass\n")
    with pytest.raises(PluginError, match="under"):
        load_local_plugins(tmp_path, ("../outside.py",), Registry(), enabled=True)


def test_path_outside_plugins_dir_rejected(tmp_path: Path) -> None:
    _plugins_dir(tmp_path)
    _write(tmp_path, "elsewhere/mod.py", "def register(api):\n    pass\n")
    with pytest.raises(PluginError, match="under"):
        load_local_plugins(tmp_path, ("elsewhere/mod.py",), Registry(), enabled=True)


def test_symlink_out_of_plugins_dir_rejected(tmp_path: Path) -> None:
    plugins = _plugins_dir(tmp_path)
    real = tmp_path / "secret.py"
    real.write_text("def register(api):\n    pass\n")
    link = plugins / "link.py"
    link.symlink_to(real)
    with pytest.raises(PluginError, match="under"):
        load_local_plugins(tmp_path, (".chipgraph/plugins/link.py",), Registry(), enabled=True)


def test_missing_file_rejected(tmp_path: Path) -> None:
    _plugins_dir(tmp_path)
    with pytest.raises(PluginError, match="no file"):
        load_local_plugins(tmp_path, (".chipgraph/plugins/nope.py",), Registry(), enabled=True)


def test_non_py_file_rejected(tmp_path: Path) -> None:
    _write(tmp_path, ".chipgraph/plugins/data.txt", "not python")
    with pytest.raises(PluginError, match=r"\.py file"):
        load_local_plugins(tmp_path, (".chipgraph/plugins/data.txt",), Registry(), enabled=True)


# --- bad plugins --------------------------------------------------------------------


def test_import_error_is_plugin_error(tmp_path: Path) -> None:
    rel = _install(tmp_path, "raises_on_import.py")
    with pytest.raises(PluginError, match=r"raises_on_import\.py") as exc:
        load_local_plugins(tmp_path, (rel,), Registry(), enabled=True)
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_missing_register_is_plugin_error(tmp_path: Path) -> None:
    rel = _install(tmp_path, "no_register.py")
    with pytest.raises(PluginError, match="must define a callable register"):
        load_local_plugins(tmp_path, (rel,), Registry(), enabled=True)


def test_duplicate_registration_is_plugin_error(tmp_path: Path) -> None:
    rel = _install(tmp_path, "duplicate.py")
    with pytest.raises(PluginError, match=r"duplicate\.py"):
        load_local_plugins(tmp_path, (rel,), Registry(), enabled=True)


def test_may_not_replace_existing_registration(tmp_path: Path) -> None:
    rel = _install(tmp_path, "tiny_layout.py", dest=".chipgraph/plugins/shadow.py")
    # Rewrite it to register the name a real check already uses.
    (tmp_path / rel).write_text(_REGISTER_TEMPLATE.format(name="layout"), encoding="utf-8")
    registry = Registry()
    registry.discover()  # registers the built-in `layout` check
    with pytest.raises(PluginError, match=r"shadow\.py"):
        load_local_plugins(tmp_path, (rel,), registry, enabled=True)


# --- module isolation ---------------------------------------------------------------


def test_same_stem_in_two_subdirs_do_not_clash(tmp_path: Path) -> None:
    a = _write(tmp_path, ".chipgraph/plugins/x/rules.py", _REGISTER_TEMPLATE.format(name="from_x"))
    b = _write(tmp_path, ".chipgraph/plugins/y/rules.py", _REGISTER_TEMPLATE.format(name="from_y"))
    registry = Registry()

    records = load_local_plugins(tmp_path, (a, b), registry, enabled=True)

    assert {r.registered[0][1] for r in records} == {"from_x", "from_y"}
    assert {"from_x", "from_y"} <= set(registry.names("check"))


def test_module_not_left_in_sys_modules(tmp_path: Path) -> None:
    before = set(sys.modules)
    rel = _install(tmp_path, "tiny_layout.py")
    load_local_plugins(tmp_path, (rel,), Registry(), enabled=True)
    added = set(sys.modules) - before
    assert not any(name.startswith("chipgraph_local_plugin_") for name in added)


_REGISTER_TEMPLATE = '''\
"""Generated fixture plugin."""

from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api.types import ToolContext


class _Chk:
    id = "{name}"
    name = "{name}"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        return CheckResult(
            check_id=spec.id, status="pass", issues=(), duration_s=0.0, idempotency_key="k"
        )


def register(api):
    api.register("check", "{name}", _Chk)
'''

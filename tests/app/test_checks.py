"""Tests for `chipgraph.app.checks.ProfileCheckRunner`."""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import init_git, make_instance, write_profile

from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext


def test_check_runner_maps_to_cmd_tool(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(
        tmp_path,
        'project: demo\nadapters:\n  hello:\n    use: cmd\n    cmd: "python3 -c \\"print(1)\\""\n',
    )
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx)

    result = asyncio.run(runner.run("hello", make_instance()))

    assert result.status == "pass"
    assert result.check_id == "hello"


def test_check_runner_maps_to_layout_check(tmp_path: Path) -> None:
    init_git(tmp_path)
    (tmp_path / "design" / "a").mkdir(parents=True)
    (tmp_path / "design" / "a" / "m_a.sv").write_text("module m_a; endmodule\n")
    write_profile(
        tmp_path,
        "project: demo\n"
        "adapters:\n"
        "  layout:\n"
        "    use: layout\n"
        "    scope: ['design/**']\n"
        "    templates:\n"
        "      rtl: 'design/{block}/m_{block}.sv'\n",
    )
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx)

    result = asyncio.run(runner.run("layout", make_instance()))

    assert result.status == "pass"


def test_check_runner_unknown_check_id(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\nadapters:\n  lint:\n    use: cmd\n")
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx)

    result = asyncio.run(runner.run("nope", make_instance()))

    assert result.status == "error"
    assert "nope" in result.issues[0].msg
    assert "lint" in result.issues[0].msg


def test_check_runner_unknown_adapter_name(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\nadapters:\n  lint:\n    use: does-not-exist\n")
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx)

    result = asyncio.run(runner.run("lint", make_instance()))

    assert result.status == "error"
    assert "does-not-exist" in result.issues[0].msg

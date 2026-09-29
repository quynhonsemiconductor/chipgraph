"""Tests for `chipgraph.mcp.server`, using the `mcp` SDK's in-process `Client`
(`mcp.Client(server)` connects to an `MCPServer` without a real transport).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import init_git, write_gen_pack, write_profile
from mcp import Client

from chipgraph.mcp.server import build_server


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def test_list_tools_has_m0_and_m102_tools(tmp_path: Path) -> None:
    init_git(tmp_path)
    server = build_server(tmp_path)

    async def _list() -> list[str]:
        async with Client(server) as client:
            result = await client.list_tools()
            for tool in result.tools:
                assert tool.description, f"{tool.name} has no description"
                assert tool.input_schema.get("type") == "object"
            return sorted(tool.name for tool in result.tools)

    names = _run(_list())
    # M0 tools: approve, build, check, config_show, status; M1-02: model_*
    expected = [
        "approve",
        "build",
        "check",
        "config_show",
        "model_block",
        "model_find",
        "model_impact",
        "model_module",
        "model_neighbors",
        "model_search",
        "model_trace",
        "status",
    ]
    assert names == expected


def test_config_show_on_tmp_profile(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\npacks: []\nblocks:\n  a: {}\n")
    server = build_server(tmp_path)

    async def _call() -> dict[str, object]:
        async with Client(server) as client:
            result = await client.call_tool("config_show", {})
            assert not result.is_error
            assert isinstance(result.structured_content, dict)
            return result.structured_content

    payload = _run(_call())
    assert payload["profile"]["project"] == "demo"  # type: ignore[index]


def test_config_show_explain(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")
    server = build_server(tmp_path)

    async def _call() -> dict[str, object]:
        async with Client(server) as client:
            result = await client.call_tool("config_show", {"explain": True})
            assert not result.is_error
            return result.structured_content

    payload = _run(_call())
    explain = payload["explain"]  # type: ignore[index]
    assert any(row["key"] == "project" for row in explain)  # type: ignore[index]


def test_config_show_without_profile_is_a_tool_error(tmp_path: Path) -> None:
    init_git(tmp_path)
    server = build_server(tmp_path)

    async def _call() -> bool:
        async with Client(server) as client:
            result = await client.call_tool("config_show", {})
            return result.is_error

    assert _run(_call()) is True


def test_check_pass_and_fail_via_cmd_adapter(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(
        tmp_path,
        "project: demo\n"
        "adapters:\n"
        "  ok:\n"
        '    use: cmd\n    cmd: "python3 -c \\"print(1)\\""\n'
        "  bad:\n"
        '    use: cmd\n    cmd: "python3 -c \\"import sys; sys.exit(1)\\""\n',
    )
    server = build_server(tmp_path)

    async def _call() -> dict[str, object]:
        async with Client(server) as client:
            result = await client.call_tool("check", {})
            assert not result.is_error
            return result.structured_content

    payload = _run(_call())
    assert payload["ok"] is False  # type: ignore[index]
    statuses = {r["check_id"]: r["status"] for r in payload["results"]}  # type: ignore[index]
    assert statuses == {"ok": "pass", "bad": "fail"}


def test_check_only_filters_check_ids(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(
        tmp_path,
        "project: demo\nadapters:\n  ok:\n    use: cmd\n    cmd: \"python3 -c 'print(1)'\"\n",
    )
    server = build_server(tmp_path)

    async def _call() -> dict[str, object]:
        async with Client(server) as client:
            result = await client.call_tool("check", {"only": ["ok"]})
            return result.structured_content

    payload = _run(_call())
    assert payload["ok"] is True  # type: ignore[index]
    assert len(payload["results"]) == 1  # type: ignore[index]


def test_build_then_status(tmp_path: Path) -> None:
    write_gen_pack(tmp_path)
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\npacks: [demo]\nblocks:\n  a: {}\n  b: {}\n")
    server = build_server(tmp_path)

    async def _call() -> tuple[dict[str, object], dict[str, object]]:
        async with Client(server) as client:
            build_result = await client.call_tool("build", {"target": "*"})
            assert not build_result.is_error
            status_result = await client.call_tool("status", {})
            assert not status_result.is_error
            return build_result.structured_content, status_result.structured_content

    build_payload, status_payload = _run(_call())
    assert sorted(build_payload["done"]) == ["demo/gen_out[block=a]", "demo/gen_out[block=b]"]  # type: ignore[index]
    assert status_payload["run_id"] == build_payload["run_id"]
    assert (tmp_path / "out" / "a.txt").read_text() == "hi"


def test_status_with_no_runs_is_empty(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")
    server = build_server(tmp_path)

    async def _call() -> dict[str, object]:
        async with Client(server) as client:
            result = await client.call_tool("status", {})
            return result.structured_content

    assert _run(_call()) == {"runs": []}


def test_approve_a_waiting_gate(tmp_path: Path) -> None:
    write_gen_pack(tmp_path, gated=True)
    init_git(tmp_path)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "a.md").write_text("spec for a")
    write_profile(tmp_path, "project: demo\npacks: [demo]\nblocks:\n  a: {}\n")
    server = build_server(tmp_path)

    async def _call() -> tuple[dict[str, object], dict[str, object]]:
        async with Client(server) as client:
            first = await client.call_tool("build", {"target": "*"})
            approval = await client.call_tool(
                "approve",
                {
                    "gate_id": "spec:a",
                    "instance": "demo/gen_out[block=a]",
                    "by": "tester",
                },
            )
            assert not approval.is_error
            second = await client.call_tool("build", {"target": "*"})
            return first.structured_content, second.structured_content

    first_payload, second_payload = _run(_call())
    assert first_payload["waiting_gate"] == ["demo/gen_out[block=a]"]  # type: ignore[index]
    assert second_payload["done"] == ["demo/gen_out[block=a]"]  # type: ignore[index]


def test_build_without_profile_is_a_tool_error_not_a_crash(tmp_path: Path) -> None:
    init_git(tmp_path)
    server = build_server(tmp_path)

    async def _call() -> tuple[bool, str]:
        async with Client(server) as client:
            result = await client.call_tool("build", {"target": "*"})
            text = "".join(getattr(block, "text", "") for block in result.content)
            return result.is_error, text

    is_error, text = _run(_call())
    assert is_error is True
    assert "chipgraph init" in text or ".chipgraph.yml" in text

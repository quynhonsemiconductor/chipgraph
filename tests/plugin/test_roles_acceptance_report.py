"""M2-01: the acceptance script's report (`docs/roles-claude-code/report.py`) on synthetic
`claude -p --output-format stream-json` streams over a real fixture project, so a real run
is graded as intended: PASS needs the author's output, the tb-author's output (or
needs_human), and no tb-author tool call that read, or got to see, RTL.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from mcp import Client

from chipgraph.mcp.server import build_server

REPO = Path(__file__).resolve().parents[2]
HERE = REPO / "docs" / "roles-claude-code"
MCP = "mcp__plugin_chipgraph_chipgraph__"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


report = _load("roles_acceptance_report", HERE / "report.py")
fx = sys.modules["fixture"]  # report.py imports its sibling fixture.py


def _call(root: Path, tool: str, args: dict[str, Any]) -> Any:
    async def _run() -> Any:
        async with Client(build_server(root)) as client:
            return await client.call_tool(tool, args)

    result = asyncio.run(_run())
    assert not result.is_error, result.content
    return result.structured_content


class Stream:
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []
        self.n = 0

    def call(
        self,
        name: str,
        args: dict[str, Any],
        result: Any,
        parent: str | None = None,
        error: bool = False,
    ) -> str:
        self.n += 1
        use_id = f"toolu_{self.n}"
        block = {"type": "tool_use", "id": use_id, "name": name, "input": args}
        self.lines.append(
            {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [block]}}
        )
        text = result if isinstance(result, str) else json.dumps(result)
        res = {
            "type": "tool_result",
            "tool_use_id": use_id,
            "content": [{"type": "text", "text": text}],
        }
        if error:
            res["is_error"] = True
        self.lines.append(
            {"type": "user", "parent_tool_use_id": parent, "message": {"content": [res]}}
        )
        return use_id

    def write(self, path: Path) -> None:
        final = {
            "type": "result",
            "subtype": "success",
            "num_turns": self.n,
            "total_cost_usd": 0.02,
        }
        path.write_text("".join(json.dumps(x) + "\n" for x in [*self.lines, final]))


@pytest.fixture
def out(tmp_path: Path) -> Path:
    """A run directory whose project went through next_task (both tasks dispatched)."""
    out = tmp_path / "cg-roles-test"
    fx.make_roles_project(out / "tinysoc")
    _call(out / "tinysoc", "next_task", {"target": fx.TARGET})
    return out


def _run(
    out: Path,
    *,
    tb_calls: list[tuple[str, dict[str, Any], Any, bool]],
    author_output: bool = True,
    tb_output: bool = True,
    tb_subagent: str = "chipgraph:tb-author",
) -> int:
    proj = out / "tinysoc"
    s = Stream()
    s.call(MCP + "next_task", {"target": fx.TARGET}, {"tasks": []})
    author = s.call(
        "Agent",
        {
            "subagent_type": "chipgraph:author",
            "model": "haiku",
            "description": f"chipgraph {fx.AUTHOR_TASK}",
        },
        "done",
    )
    tb = s.call(
        "Agent",
        {"subagent_type": tb_subagent, "model": "haiku", "description": f"chipgraph {fx.TB_TASK}"},
        "done",
    )
    context = _call(proj, "get_context", {"task_id": fx.AUTHOR_TASK})
    s.call(MCP + "get_context", {"task_id": fx.AUTHOR_TASK}, context, parent=author)
    s.call("Read", {"file_path": str(proj / fx.RTL)}, (proj / fx.RTL).read_text(), parent=author)
    if author_output:
        (proj / fx.AUTHOR_OUTPUT).parent.mkdir(parents=True, exist_ok=True)
        (proj / fx.AUTHOR_OUTPUT).write_text("- pin_out: out, 8\n")
        s.call("Write", {"file_path": str(proj / fx.AUTHOR_OUTPUT)}, "ok", parent=author)
    for name, args, result, error in tb_calls:
        if result == "<context>":
            result = _call(proj, "get_context", {"task_id": fx.TB_TASK})
        s.call(name, args, result, parent=tb, error=error)
    if tb_output:
        (proj / fx.TB_OUTPUT).parent.mkdir(parents=True, exist_ok=True)
        (proj / fx.TB_OUTPUT).write_text("async def test_ports(dut):\n    pass\n")
        s.call("Write", {"file_path": str(proj / fx.TB_OUTPUT), "content": "..."}, "ok", parent=tb)
    s.write(out / "stream.jsonl")
    (out / "build.json").write_text("{}")
    return int(report.main(out))


CONTEXT = (MCP + "get_context", {"task_id": "roles/gpio_port_test[]"}, "<context>", False)


def test_a_clean_run_passes(out: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(out, tb_calls=[CONTEXT]) == 0
    text = capsys.readouterr().out
    assert "PASS" in text and "subagent: chipgraph:tb-author model=haiku" in text
    assert "denied_reads: ['rtl/**'" in text


def test_a_tb_author_read_attempt_fails_even_when_refused(out: Path) -> None:
    refused = ("Read", {"file_path": "rtl/tiny_gpio.sv"}, "chipgraph: refused", True)
    assert _run(out, tb_calls=[CONTEXT, refused]) == 1


def test_a_glob_or_grep_fails(out: Path) -> None:
    assert _run(out, tb_calls=[CONTEXT, ("Glob", {"pattern": "**/*.md"}, "[]", False)]) == 1


def test_another_tool_naming_rtl_fails(out: Path) -> None:
    call = ("mcp__fs__read", {"path": "rtl/tiny_gpio.sv"}, "refused", True)
    assert _run(out, tb_calls=[CONTEXT, call]) == 1


def test_rtl_text_in_a_tb_author_result_fails(out: Path) -> None:
    rtl = (out / "tinysoc" / fx.RTL).read_text()
    leak = (MCP + "get_context", {"task_id": fx.TB_TASK}, {"inputs": [{"content": rtl}]}, False)
    assert _run(out, tb_calls=[leak]) == 1


def test_needs_human_without_output_passes(out: Path) -> None:
    proj = out / "tinysoc"
    _call(proj, "get_context", {"task_id": fx.TB_TASK})
    _call(
        proj,
        "submit",
        {"task_id": fx.TB_TASK, "result": {"status": "needs_human", "open_questions": ["ports?"]}},
    )
    assert _run(out, tb_calls=[(*CONTEXT[:2], "{}", False)], tb_output=False) == 0


def test_missing_outputs_fail(out: Path) -> None:
    assert _run(out, tb_calls=[CONTEXT], tb_output=False) == 1


def test_missing_author_output_fails(out: Path) -> None:
    assert _run(out, tb_calls=[CONTEXT], author_output=False) == 1


def test_the_wrong_subagent_type_fails(out: Path) -> None:
    assert _run(out, tb_calls=[CONTEXT], tb_subagent="chipgraph:author") == 1

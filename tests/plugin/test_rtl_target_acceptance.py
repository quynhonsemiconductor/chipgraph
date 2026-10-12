"""M2-11b: the acceptance run of `docs/rtl-target-claude-code/` without a model.

The fixture (`fixture.py`) is built for real, and the two sessions of `run.sh` are played
here through the MCP server exactly as `/chipgraph:plan pwm` and `/chipgraph:rtl pwm`
drive it, each "subagent" scripted (the planner writes the M2-04 plan of `pwm`, the RTL
author a lint-clean `tiny_pwm`, the testbench author a cocotb test, the Critic a review);
every call is written to `plan.jsonl` / `rtl.jsonl` in the shape of `claude -p
--output-format stream-json`. The script's own steps run as in `run.sh`: `chipgraph plan
show pwm --json` and its sanity check, `chipgraph approve plan:pwm`, then `chipgraph
approve pr:pwm` and `chipgraph rtl pwm`. `report.py` must then say PASS, and FAIL when a
stream shows a model approving, a testbench author reading, or the main session using a
file tool.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from mcp import Client
from typer.testing import CliRunner

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs" / "rtl-target-claude-code"
_RTL_TESTS = REPO / "tests" / "packs" / "digital_rtl"
if str(_RTL_TESTS) not in sys.path:
    sys.path.insert(0, str(_RTL_TESTS))

from rtl_author_helpers import profile_of, render_pwm  # noqa: E402

import chipgraph.cli as cli  # noqa: E402
from chipgraph.mcp.server import build_server  # noqa: E402


def _load(name: str, path: Path) -> ModuleType:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


fx = _load("rtl_target_acceptance_fixture", DOCS / "fixture.py")
report = _load("rtl_target_acceptance_report", DOCS / "report.py")

MCP = "mcp__plugin_chipgraph_chipgraph__"
PASS = {"use": "cmd", "cmd": ["python3", "-c", "pass"]}
PLAN_TEXT = (REPO / "docs" / "rtl-claude-code" / "pwm.plan.yml").read_text(encoding="utf-8")
TB_TEXT = '''\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

PERIOD = 0x0
DUTY = 0x1
CTRL = 0x2


async def start(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.addr.value = 0
    dut.wr_en.value = 0
    dut.wdata.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def write(dut, addr: int, data: int) -> None:
    dut.addr.value = addr
    dut.wdata.value = data
    dut.wr_en.value = 1
    await RisingEdge(dut.clk)
    dut.wr_en.value = 0


async def read(dut, addr: int) -> int:
    dut.addr.value = addr
    await RisingEdge(dut.clk)
    await ReadOnly()
    value = dut.rdata.value.to_unsigned()
    await RisingEdge(dut.clk)
    return value


@cocotb.test()
async def test_pwm(dut) -> None:
    """The counter wraps at PERIOD and pwm_out is high below DUTY; registers read back."""
    # verifies: REQ-PWM-001, REQ-PWM-002, REQ-PWM-003, REQ-PWM-004
    await start(dut)
    await write(dut, PERIOD, 3)
    await write(dut, DUTY, 2)
    assert await read(dut, PERIOD) == 3, "REQ-PWM-003/004: PERIOD reads back what was written"
    await write(dut, CTRL, 1)
    highs = 0
    for _ in range(8):
        await RisingEdge(dut.clk)
        await ReadOnly()
        highs += int(dut.pwm_out.value)
    assert highs == 4, f"REQ-PWM-001/002: expected 2 high cycles per period of 4, got {highs}"
    assert await read(dut, 0x3) == 0, "REQ-PWM-004: offset 0x3 reads 0"
'''


class Recorder:
    """Writes the calls of one session as `claude -p --output-format stream-json` does."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.lines: list[dict[str, Any]] = [{"type": "system", "subtype": "init"}]
        self.n = 0

    def _use(self, name: str, args: dict[str, Any], result: Any, parent: str | None) -> str:
        self.n += 1
        use_id = f"toolu_{self.n:03d}"
        block = {"type": "tool_use", "id": use_id, "name": name, "input": args}
        self.lines.append(
            {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [block]}}
        )
        text = result if isinstance(result, str) else json.dumps(result)
        res = {"type": "tool_result", "tool_use_id": use_id, "content": text}
        self.lines.append(
            {"type": "user", "parent_tool_use_id": parent, "message": {"content": [res]}}
        )
        return use_id

    async def mcp(self, tool: str, parent: str | None = None, **args: Any) -> dict[str, Any]:
        result = await self.client.call_tool(tool, args)
        assert not result.is_error, (tool, result.content)
        data = result.structured_content
        assert isinstance(data, dict)
        self._use(f"{MCP}{tool}", args, data, parent)
        return data

    def call(self, name: str, args: dict[str, Any], result: Any, parent: str | None) -> str:
        return self._use(name, args, result, parent)

    def save(self, path: Path, text: str, turns: int) -> None:
        self.lines.append(
            {
                "type": "result",
                "subtype": "success",
                "num_turns": turns,
                "total_cost_usd": 0.5,
                "duration_ms": 1000,
                "result": text,
            }
        )
        path.write_text("\n".join(json.dumps(x) for x in self.lines) + "\n", encoding="utf-8")


async def _subagent(rec: Recorder, proj: Path, task: dict[str, Any]) -> dict[str, Any]:
    """One scripted subagent run: the Agent call, its get_context and writes; the result."""
    agent = rec.call(
        "Agent",
        {
            "subagent_type": task["agent"],
            "model": task["model"],
            "description": f"chipgraph {task['task_id']}",
            "prompt": task["prompt"],
        },
        "done",
        None,
    )
    context = await rec.mcp("get_context", parent=agent, task_id=task["task_id"])
    if task.get("reply") == "review":
        review = context["review"]
        return {
            "status": "done",
            "review": {
                "schema_version": 1,
                "target": review["target"],
                "base": review["base"],
                "head": review["head"],
                "reviewed": list(review["changed"]),
                "comments": [],
                "summary": "tiny_pwm and its test follow the MAS.",
                "verdict": "approve",
            },
        }
    rule = task["rule"]
    if rule == "digital-rtl/plan":
        text = PLAN_TEXT
    elif rule == "digital-rtl/rtl_module":
        text = render_pwm(profile_of(fx.EXEMPLARS))
    else:
        text = TB_TEXT
    for rel in task["outputs"]:
        out = proj / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        rec.call("Write", {"file_path": str(out), "content": text}, "File created", agent)
    return {"status": "done", "files_written": task["outputs"]}


async def _loop(rec: Recorder, proj: Path, target: str) -> dict[str, Any]:
    for _ in range(20):
        answer = await rec.mcp("next_task", target=target)
        if answer["done"] or answer["stopped"]:
            return answer
        for task in answer["tasks"]:
            result = await _subagent(rec, proj, task)
            done = await rec.mcp("submit", task_id=task["task_id"], result=result)
            assert done["status"] == "accepted", done["reasons"]
    raise AssertionError("no stop in 20 rounds")


def _cg(proj: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli.app, ["-C", str(proj), *args])
    return result.exit_code, result.output


def _play(out: Path) -> None:
    """The run of `run.sh`, with the two sessions scripted."""
    proj = out / "tinysoc"
    lint = None if shutil.which("verilator") else dict(PASS)
    fx.make_target_project(
        proj, origin=out / "origin.git", tiers={"medium": "sonnet", "large": "sonnet"}, lint=lint
    )

    async def _plan() -> None:
        async with Client(build_server(proj)) as client:
            rec = Recorder(client)
            last = await _loop(rec, proj, fx.PLAN_TARGET)
            assert last["stopped"]
            shown = await rec.mcp("plan_show", block=fx.BLOCK)
            rec.save(
                out / "plan.jsonl",
                f"Not approved: no person answered. Run: {shown['gate']['approve']}",
                turns=9,
            )

    asyncio.run(_plan())
    code, shown = _cg(proj, "--json", "plan", "show", fx.BLOCK)
    assert code == 0, shown
    (out / "plan_show.json").write_text(shown, encoding="utf-8")
    assert report.check_plan(json.loads(shown)) == []
    code, text = _cg(proj, "approve", "plan:pwm", "--instance", fx.EXPAND, "--by", "e2e-human")
    assert code == 0, text

    async def _rtl() -> None:
        async with Client(build_server(proj)) as client:
            rec = Recorder(client)
            await rec.mcp("plan_show", block=fx.BLOCK)
            last = await _loop(rec, proj, fx.TARGET)
            assert [b["instance"] for b in last["blocked"]] == [fx.PR_GATE]
            await rec.mcp("status", run_id=last["run_id"])
            rec.save(out / "rtl.jsonl", "Waiting for a reviewer at gate pr:pwm.", turns=20)

    asyncio.run(_rtl())
    code, text = _cg(proj, "approve", "pr:pwm", "--instance", fx.PR_GATE, "--by", "e2e-reviewer")
    assert code == 0, text
    code, text = _cg(proj, "rtl", fx.BLOCK)
    (out / "final.txt").write_text(text, encoding="utf-8")
    (out / "final.exit").write_text(f"{code}\n", encoding="utf-8")
    (out / "timing.txt").write_text("plan_s=1\nrtl_s=2\ntotal_s=3\n", encoding="utf-8")


@pytest.fixture(scope="module")
def played(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("acceptance") / "cg-rtl-target-test"
    out.mkdir()
    _play(out)
    return out


def _copy(played: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "cg-rtl-target-copy"
    shutil.copytree(played, dest, symlinks=True)
    return dest


def _edit(out: Path, name: str, edit: Any) -> None:
    path = out / f"{name}.jsonl"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    edit(lines)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def _tool_use(name: str, args: dict[str, Any], parent: str | None, use_id: str) -> dict[str, Any]:
    block = {"type": "tool_use", "id": use_id, "name": name, "input": args}
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [block]}}


def test_the_played_run_passes(played: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert report.main([str(played)]) == 0
    out = capsys.readouterr().out
    assert "== verdict\n  PASS" in out
    for crit in "abcdefghijk":
        assert f"({crit}) ok" in out, crit
    assert fx.module_task("tiny_pwm") in out and "chipgraph:rtl-author:sonnet" in out
    assert "total_cost_usd 1.0000" in out


def test_the_plan_sanity_check(played: Path) -> None:
    shown = json.loads((played / "plan_show.json").read_text(encoding="utf-8"))
    assert report.check_plan(shown) == []
    bad = copy.deepcopy(shown)
    bad["modules"][0]["writes"] = [{"path": "dv/pwm/x.sv", "replaces": False}]
    bad["coverage"] = bad["coverage"][1:]
    problems = report.check_plan(bad)
    assert any("writes dv/pwm/x.sv" in p for p in problems)
    assert any("REQ-PWM-001" in p for p in problems)
    assert report.check_plan({"exists": False}) == ["there is no plan"]


def test_a_model_approving_fails(
    played: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _copy(played, tmp_path)
    _edit(out, "plan", lambda lines: lines.insert(-1, _tool_use(f"{MCP}approve", {}, None, "x1")))
    assert report.report(out) is False
    assert "(c) FAIL" in capsys.readouterr().out


def test_a_testbench_author_reading_fails(
    played: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _copy(played, tmp_path)
    stream = report.load_stream(out, "rtl")
    [agent] = stream.agents_for(fx.TB_TASK)
    read = _tool_use("Read", {"file_path": "rtl/tiny_pwm.sv"}, agent.id, "x2")
    _edit(out, "rtl", lambda lines: lines.insert(-1, read))
    assert report.report(out) is False
    assert "(h) FAIL" in capsys.readouterr().out


def test_rtl_text_in_a_testbench_authors_result_fails(
    played: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _copy(played, tmp_path)
    stream = report.load_stream(out, "rtl")
    [agent] = stream.agents_for(fx.TB_TASK)
    leak = (out / "tinysoc" / "rtl" / "tiny_timer.sv").read_text(encoding="utf-8")
    use = _tool_use(f"{MCP}get_context", {}, agent.id, "x3")
    res = {
        "type": "user",
        "parent_tool_use_id": agent.id,
        "message": {"content": [{"type": "tool_result", "tool_use_id": "x3", "content": leak}]},
    }

    def _insert(lines: list[dict[str, Any]]) -> None:
        lines[-1:-1] = [use, res]

    _edit(out, "rtl", _insert)
    assert report.report(out) is False
    assert "(h) FAIL" in capsys.readouterr().out


def test_a_main_session_file_tool_fails(
    played: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _copy(played, tmp_path)
    write = _tool_use("Write", {"file_path": "rtl/x.sv"}, None, "x4")
    _edit(out, "rtl", lambda lines: lines.insert(-1, write))
    assert report.report(out) is False
    assert "(i) FAIL" in capsys.readouterr().out


def test_too_many_turns_fails(
    played: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _copy(played, tmp_path)
    _edit(out, "rtl", lambda lines: lines[-1].update(num_turns=report.TURN_BOUNDS["rtl"] + 1))
    assert report.report(out) is False
    assert "(j) FAIL" in capsys.readouterr().out

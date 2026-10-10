"""M2-06: the acceptance report (`docs/tb-claude-code/report.py`) on synthetic runs.

No model runs: a stream-json is made up as Claude Code would write it, with the
subagents' calls and the real `get_context` answers. A clean run passes; a subagent that
reads an RTL file, or gets RTL text in a tool result, fails it. The simulation part is
informational and replaced here (it needs Verilator; the e2e suite covers it).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tb_author_helpers import GOOD_GPIO_TEST, TB_DOCS, fx, get_context, load_module, next_task

report = load_module("tb_acceptance_report", TB_DOCS / "report.py")

TIMER_TEST = '''\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge


@cocotb.test()
async def test_timer(dut) -> None:
    """REQ-TIM-001 REQ-TIM-002 REQ-TIM-003 REQ-TIM-004 REQ-TIM-005"""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.addr.value = 0
    dut.wr_en.value = 0
    dut.wdata.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    assert dut.irq.value == 0, "REQ-TIM-003: irq is 0 after reset"
'''
TESTS = {"gpio": GOOD_GPIO_TEST, "timer": TIMER_TEST}


def _event(content: dict, parent: str | None = None) -> dict:
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [content]}}


def _result(use_id: str, text: str, parent: str | None = None) -> dict:
    return {
        "type": "user",
        "parent_tool_use_id": parent,
        "message": {"content": [{"type": "tool_result", "tool_use_id": use_id, "content": text}]},
    }


def _run(tmp_path: Path, extra: dict[str, list[dict]] | None = None) -> Path:
    """A run dir: the fixture project, its tasks done by 'subagents' in a made-up stream."""
    out = tmp_path / "cg-tb-test"
    proj = fx.make_tb_project(out / "tinysoc", blocks=fx.BLOCKS)
    events: list[dict] = [{"type": "system", "subtype": "init", "plugins": []}]
    answer = next_task(proj)
    events.append(_event({"type": "tool_use", "id": "m1", "name": "next_task", "input": {}}))
    events.append(_result("m1", json.dumps(answer)))
    for n, block in enumerate(fx.BLOCKS):
        task_id = fx.TASKS[block]
        agent = f"a{n}"
        events.append(
            _event(
                {
                    "type": "tool_use",
                    "id": agent,
                    "name": "Agent",
                    "input": {
                        "subagent_type": "chipgraph:tb-author",
                        "model": "sonnet",
                        "prompt": f"You are doing chipgraph task {task_id!r}.",
                    },
                }
            )
        )
        context = get_context(proj, task_id)
        name = report.CHIPGRAPH_MCP + "get_context"
        events.append(_event({"type": "tool_use", "id": f"c{n}", "name": name, "input": {}}, agent))
        events.append(_result(f"c{n}", json.dumps(context), agent))
        for item in (extra or {}).get(block, []):
            events.append(_event(item["use"], agent))
            events.append(_result(item["use"]["id"], item["result"], agent))
        out_path = proj / fx.OUTPUTS[block]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(TESTS[block])
        write = {"file_path": str(out_path), "content": TESTS[block]}
        events.append(
            _event({"type": "tool_use", "id": f"w{n}", "name": "Write", "input": write}, agent)
        )
        events.append(_result(f"w{n}", "ok", agent))
    (out / "stream.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return out


@pytest.fixture(autouse=True)
def _no_sim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report, "simulate", lambda out, proj, block: ("skipped", []))


def test_a_clean_run_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = _run(tmp_path)
    assert report.main(out) == 0, capsys.readouterr().out
    text = capsys.readouterr().out
    assert text.count("(b) tb_static passed: True") == 2
    assert "read no RTL and saw none" in text and "PASS" in text


def test_a_read_of_the_rtl_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    read = {
        "use": {"type": "tool_use", "id": "r0", "name": "Read", "input": {"file_path": "x"}},
        "result": "refused",
    }
    out = _run(tmp_path, {"gpio": [read]})
    assert report.main(out) == 1
    text = capsys.readouterr().out
    assert "- Read" in text and "dv/tb_module[block=gpio]: FAIL" in text
    assert "dv/tb_module[block=timer]: PASS" in text


def test_rtl_text_in_a_tool_result_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    leak = {
        "use": {"type": "tool_use", "id": "x0", "name": "mcp__other__thing", "input": {}},
        "result": "  // chipgraph-tb-fingerprint: tiny_timer body",
    }
    out = _run(tmp_path, {"timer": [leak]})
    assert report.main(out) == 1
    assert "holds RTL text" in capsys.readouterr().out


def test_the_fingerprints_are_planted_in_every_rtl_body(tmp_path: Path) -> None:
    proj = fx.make_tb_project(tmp_path / "t", ingest=False)
    for path in sorted((proj / "rtl").glob("*.sv")):
        text = path.read_text()
        head, _, body = text.partition(");\n")
        assert fx.FINGERPRINT in body and fx.FINGERPRINT not in head
    assert fx.FINGERPRINT in report.rtl_fingerprints(proj)

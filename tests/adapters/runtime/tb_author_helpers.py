"""Shared by the M2-06 tests: the acceptance fixture (`docs/tb-claude-code/fixture.py`,
loaded under a unique module name), RTL markers, and short calls of the runtime tools."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from chipgraph.adapters.runtime.claude_code import service
from chipgraph.app.context import AppContext
from chipgraph.core.runtime import AgentTaskRecord, TaskQueue
from chipgraph.core.state.layout import StateLayout

REPO = Path(__file__).resolve().parents[3]
TB_DOCS = REPO / "docs" / "tb-claude-code"


def load_module(name: str, path: Path) -> ModuleType:
    """Import `path` as module `name` (once)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fx = load_module("tb_acceptance_fixture", TB_DOCS / "fixture.py")

GPIO = fx.TASKS["gpio"]
TIMER = fx.TASKS["timer"]

# Markers planted in the body of tiny_gpio: in a comment, as a signal name, and as a
# parameter used only inside. None may ever reach the testbench Author.
BODY_MARKERS = ("ZQXBODYCOMMENT", "zqx_body_signal", "ZQX_BODY_PARAM")
# A port named with a marker: a declaration, which the Author may see.
PORT_MARKER = "zqx_decl_port"


def plant_markers(root: Path, *, port: bool = True) -> None:
    """Plant the body markers (and, with `port`, an extra declared port) in tiny_gpio."""
    path = root / "rtl" / "tiny_gpio.sv"
    text = path.read_text()
    if port:
        text = text.replace(
            "    output logic [7:0]   pin_dir\n);",
            f"    output logic [7:0]   pin_dir,\n    input  logic         {PORT_MARKER}\n);",
        )
    body = (
        f"\n  // {BODY_MARKERS[0]}: a comment in the body\n"
        f"  localparam int {BODY_MARKERS[2]} = 3;\n"
        f"  logic [{BODY_MARKERS[2]}:0] {BODY_MARKERS[1]};\n"
        f"  assign {BODY_MARKERS[1]} = '0;\n"
    )
    text = text.replace("  logic [7:0] out_q;\n", "  logic [7:0] out_q;\n" + body, 1)
    path.write_text(text)


def ctx(root: Path) -> AppContext:
    return AppContext.load(root)


def next_task(root: Path, target: str = fx.RULE) -> dict[str, Any]:
    return asyncio.run(service.next_task(ctx(root), target))


def get_context(root: Path, task_id: str) -> dict[str, Any]:
    return asyncio.run(service.get_context(ctx(root), task_id))


def submit(root: Path, task_id: str, **report: Any) -> dict[str, Any]:
    return asyncio.run(service.submit(ctx(root), task_id, service.SubmitReport(**report)))


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def record(root: Path, task_id: str) -> AgentTaskRecord:
    return TaskQueue(StateLayout(root)).require(task_id)


def leaks(answer: object, markers: tuple[str, ...] = BODY_MARKERS) -> list[str]:
    """The markers found anywhere in `answer` (as JSON)."""
    text = json.dumps(answer, default=str)
    return [m for m in markers if m in text]


GOOD_GPIO_TEST = '''\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

DATA_OUT = 0x0
DATA_IN = 0x1
DIR = 0x2


async def reset(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.addr.value = 0
    dut.wr_en.value = 0
    dut.wdata.value = 0
    dut.pin_in.value = 0
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
async def test_data_out(dut) -> None:
    """REQ-GPIO-001: DATA_OUT drives pin_out."""
    # verifies: REQ-GPIO-001
    await reset(dut)
    await write(dut, DATA_OUT, 0xA5)
    await ReadOnly()
    got = dut.pin_out.value.to_unsigned()
    assert got == 0xA5, f"REQ-GPIO-001: pin_out expected 0xa5, got {got:#x}"


@cocotb.test()
async def test_data_in_and_dir(dut) -> None:
    """DATA_IN and DIR."""
    # verifies: REQ-GPIO-002, REQ-GPIO-003
    await reset(dut)
    dut.pin_in.value = 0x3C
    got = await read(dut, DATA_IN)
    assert got == 0x3C, f"REQ-GPIO-002: DATA_IN expected 0x3c, got {got:#x}"
    await write(dut, DIR, 0x0F)
    assert await read(dut, DIR) == 0x0F, "REQ-GPIO-003: DIR expected 0x0f"
    assert await read(dut, 0x3) == 0, "REQ-GPIO-003: offset 0x3 reads 0"
'''
"""A gpio test that passes `tb_static` (and the simulation, on the real RTL)."""

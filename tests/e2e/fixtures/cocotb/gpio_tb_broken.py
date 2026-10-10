# SPDX-License-Identifier: Apache-2.0
# A deliberately failing cocotb testbench for tiny_gpio, used only by chipgraph's tests
# (tests/e2e/test_sim_edalize.py). Each test fails in a different way, so the e2e tests
# can select one with the adapter's `testcase` argument:
#
#   test_passes          passes (so a selection can mix pass and fail)
#   test_wrong_readback  an `assert` with the wrong expected value -> AssertionError
#   test_missing_signal  touches a signal the DUT does not have -> AttributeError
#
# Not a pytest module: cocotb loads it by name (COCOTB_TEST_MODULES=gpio_tb_broken).

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge


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


async def write_then_read(dut, addr: int, data: int) -> int:
    dut.addr.value = addr
    dut.wdata.value = data
    dut.wr_en.value = 1
    await RisingEdge(dut.clk)
    dut.wr_en.value = 0
    await RisingEdge(dut.clk)
    await ReadOnly()
    value = dut.rdata.value.to_unsigned()
    await RisingEdge(dut.clk)
    return value


@cocotb.test()
async def test_passes(dut) -> None:
    await reset(dut)
    assert await write_then_read(dut, 0x0, 0x11) == 0x11


@cocotb.test()
async def test_wrong_readback(dut) -> None:
    await reset(dut)
    got = await write_then_read(dut, 0x0, 0xA5)
    assert got == 0x5A, f"DATA_OUT readback: got {got:#04x}, expected 0x5a"  # CG_ASSERT_LINE


@cocotb.test()
async def test_missing_signal(dut) -> None:
    await reset(dut)
    dut.no_such_signal.value = 1  # CG_CRASH_LINE

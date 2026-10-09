"""S3 spike: a cocotb 2.x testbench for examples/tinysoc/rtl/tiny_gpio.sv.

Each test resets the block, writes a register through its bus (`addr`, `wr_en`, `wdata`)
and reads it back on `rdata`. `S3_BREAK` makes a run fail on purpose, so the results
parser can be drafted against real failures:

    S3_BREAK=assert   test_data_out_readback expects the wrong value (an `assert`)
    S3_BREAK=crash    test_dir_readback touches a signal that does not exist (an exception)
    S3_BREAK=import   the module itself fails to import (no test runs)
    S3_BREAK=build    (s3_common) an extra RTL file with a syntax error: the build fails

This module is not collected by chipgraph's pytest (testpaths = tests); cocotb loads it
through COCOTB_TEST_MODULES=gpio_tb with this directory on PYTHONPATH.
"""

from __future__ import annotations

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

DATA_OUT = 0x0
DATA_IN = 0x1
DIR = 0x2

BREAK = os.environ.get("S3_BREAK", "")
if BREAK == "import":
    raise ImportError("S3_BREAK=import: the test module fails to import")


async def reset(dut) -> None:
    """Start a 10 ns clock and hold `rst_n` low for two cycles."""
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
    """One bus write: drive addr/wdata with wr_en high for one rising edge."""
    dut.addr.value = addr
    dut.wdata.value = data
    dut.wr_en.value = 1
    await RisingEdge(dut.clk)
    dut.wr_en.value = 0


async def read(dut, addr: int) -> int:
    """One bus read: `rdata` is combinational from `addr`, sampled after it settles."""
    dut.addr.value = addr
    await RisingEdge(dut.clk)
    await ReadOnly()
    value = dut.rdata.value.to_unsigned()
    await RisingEdge(dut.clk)
    return value


@cocotb.test()
async def test_data_out_readback(dut) -> None:
    """DATA_OUT written through the bus reads back and drives pin_out."""
    await reset(dut)
    await write(dut, DATA_OUT, 0xA5)
    got = await read(dut, DATA_OUT)
    expected = 0x5A if BREAK == "assert" else 0xA5
    assert got == expected, f"DATA_OUT readback: got {got:#04x}, expected {expected:#04x}"
    assert dut.pin_out.value.to_unsigned() == 0xA5


@cocotb.test()
async def test_dir_readback(dut) -> None:
    """DIR written through the bus reads back and drives pin_dir."""
    await reset(dut)
    await write(dut, DIR, 0x0F)
    if BREAK == "crash":
        dut.no_such_signal.value = 1
    assert await read(dut, DIR) == 0x0F
    assert dut.pin_dir.value.to_unsigned() == 0x0F


@cocotb.test()
async def test_data_in(dut) -> None:
    """DATA_IN reads pin_in; a write to it is ignored."""
    await reset(dut)
    dut.pin_in.value = 0x3C
    await write(dut, DATA_IN, 0xFF)
    assert await read(dut, DATA_IN) == 0x3C

# SPDX-License-Identifier: Apache-2.0
# chipgraph example: a cocotb 2.x testbench for tiny_gpio, run by the `edalize` sim adapter.
#
# Each test resets the block, writes a register through its bus (`addr`, `wr_en`, `wdata`)
# and reads it back on `rdata` (combinational from `addr`). cocotb loads this module by
# name (`COCOTB_TEST_MODULES=tb_tiny_gpio`) with this directory on PYTHONPATH; it is not a
# pytest module. Run it with chipgraph:
#
#   adapters:
#     sim: { use: edalize, simulator: verilator, top: tiny_gpio,
#            filelist: filelists/gpio.f, test_module: dv/tb_tiny_gpio.py }

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

DATA_OUT = 0x0
DATA_IN = 0x1
DIR = 0x2


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
    """One bus write: `addr`/`wdata` with `wr_en` high for one rising edge."""
    dut.addr.value = addr
    dut.wdata.value = data
    dut.wr_en.value = 1
    await RisingEdge(dut.clk)
    dut.wr_en.value = 0


async def read(dut, addr: int) -> int:
    """One bus read: drive `addr`, sample `rdata` once it has settled."""
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
    assert got == 0xA5, f"DATA_OUT readback: got {got:#04x}, expected 0xa5"
    assert dut.pin_out.value.to_unsigned() == 0xA5


@cocotb.test()
async def test_data_in(dut) -> None:
    """DATA_IN reads pin_in in bits 7:0; a write to it is ignored."""
    await reset(dut)
    dut.pin_in.value = 0x3C
    await write(dut, DATA_IN, 0xFF)
    assert await read(dut, DATA_IN) == 0x3C


@cocotb.test()
async def test_dir_readback(dut) -> None:
    """DIR written through the bus reads back and drives pin_dir; offset 0x3 reads 0."""
    await reset(dut)
    await write(dut, DIR, 0x0F)
    assert await read(dut, DIR) == 0x0F
    assert dut.pin_dir.value.to_unsigned() == 0x0F
    assert await read(dut, 0x3) == 0

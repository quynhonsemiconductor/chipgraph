# chipgraph eval reference test (evals/rtl, problem syscsr_dbg_en): the fake testbench
# author's answer, written by hand from QNSC_SYSCSR_MAS V3.2 section 7.5 and the module's
# interface only, never from its RTL. Apache-2.0, as chipgraph.
"""cocotb tests of DBG_BOOT.dbg_en: the DBG_EN pin through two flip-flops, and its mask."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

LATENCY = 2
"""dbg_en follows the pin two clock edges later, rising and falling (MAS 7.5)."""


async def start(dut) -> None:
    """Clock, pin and mask 0, power-on reset held for three cycles, then released."""
    cocotb.start_soon(Clock(dut.i_clk_cpu, 50, unit="ns").start())
    dut.i_dbg_en.value = 0
    dut.i_dbg_en_mask.value = 0
    dut.i_rst_n_por.value = 0
    for _ in range(3):
        await FallingEdge(dut.i_clk_cpu)
        assert int(dut.o_dbg_en_masked.value) == 0, "dbg_en is 0 in reset"
    dut.i_rst_n_por.value = 1


class Pin:
    """Drives the pin one cycle at a time and knows what dbg_en must be after each edge."""

    def __init__(self, dut) -> None:
        self.dut = dut
        self.sampled = [0] * LATENCY  # the flip-flops hold 0 after reset

    async def drive(self, value: int) -> int:
        """Set the pin, let one rising edge pass; returns the pin seen `LATENCY` edges ago."""
        self.dut.i_dbg_en.value = value
        await FallingEdge(self.dut.i_clk_cpu)
        self.sampled.append(value)
        return self.sampled[-LATENCY]


@cocotb.test()
async def test_reset_reads_zero(dut):
    """SYSCSR_011: after power-on the flip-flops read 0 until the pin has been sampled
    twice: with the pin low, dbg_en stays 0."""
    await start(dut)
    pin = Pin(dut)
    for _ in range(6):
        await pin.drive(0)
        assert int(dut.o_dbg_en_masked.value) == 0, "dbg_en must stay 0 while the pin is 0"


@cocotb.test()
async def test_follows_pin(dut):
    """SYSCSR_011: with dbg_en_mask = 0, dbg_en follows the DBG_EN pin two clock edges
    later, rising and falling (not latched)."""
    # verifies: SYSCSR_011
    await start(dut)
    pin = Pin(dut)
    pattern = [1, 1, 1, 0, 0, 1, 0, 1, 1, 0, 1, 0, 0, 0, 1, 1, 0]
    for cycle, value in enumerate(pattern):
        expected = await pin.drive(value)
        got = int(dut.o_dbg_en_masked.value)
        assert got == expected, f"cycle {cycle}: dbg_en {got}, expected {expected}"


@cocotb.test()
async def test_mask(dut):
    """SYSCSR_012: with dbg_en_mask = 1, dbg_en reads 0 whatever the pin; writing the
    mask back to 0 shows the pin again."""
    # verifies: SYSCSR_012
    await start(dut)
    pin = Pin(dut)
    for _ in range(4):
        await pin.drive(1)
    assert int(dut.o_dbg_en_masked.value) == 1, "the pin is high: dbg_en reads 1"
    dut.i_dbg_en_mask.value = 1
    for value in (1, 1, 0, 1, 0, 0, 1, 1):
        await pin.drive(value)
        assert int(dut.o_dbg_en_masked.value) == 0, "masked: dbg_en must read 0"
    dut.i_dbg_en_mask.value = 0
    for _ in range(3):
        expected = await pin.drive(1)
        assert int(dut.o_dbg_en_masked.value) == expected, "unmasked: dbg_en shows the pin"
    assert int(dut.o_dbg_en_masked.value) == 1

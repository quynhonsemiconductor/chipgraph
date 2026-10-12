# chipgraph eval reference test (evals/rtl, problem rom_wr_resp): the fake testbench
# author's answer, written by hand from QNSC_ROM_MAS V3.2 sections 7.3 and 7.4 and the
# module's interface only, never from its RTL. Apache-2.0, as chipgraph.
"""cocotb tests of the ROM's write-error responder (QNSC_ROM_MAS 7.3, 7.4)."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

SLVERR = 0b10
"""BRESP of every write (MAS 7.3)."""


async def start(dut) -> None:
    """Clock, every input 0, reset held for three cycles, then released."""
    cocotb.start_soon(Clock(dut.i_clk_mem, 10, unit="ns").start())
    dut.i_bus_axi_aw_addr.value = 0
    dut.i_bus_axi_aw_valid.value = 0
    dut.i_bus_axi_aw_burst.value = 0
    dut.i_bus_axi_aw_len.value = 0
    dut.i_bus_axi_aw_id.value = 0
    dut.i_bus_axi_w_data.value = 0
    dut.i_bus_axi_w_strb.value = 0
    dut.i_bus_axi_w_valid.value = 0
    dut.i_bus_axi_w_last.value = 0
    dut.i_bus_axi_b_ready.value = 0
    dut.i_rst_n_mem.value = 0
    for _ in range(3):
        await FallingEdge(dut.i_clk_mem)
    dut.i_rst_n_mem.value = 1
    await FallingEdge(dut.i_clk_mem)


async def cycle(dut) -> None:
    """One clock: inputs set now are sampled at the rising edge; outputs read after it."""
    await FallingEdge(dut.i_clk_mem)


def state(dut) -> tuple[int, int, int]:
    return (
        int(dut.o_bus_axi_aw_ready.value),
        int(dut.o_bus_axi_w_ready.value),
        int(dut.o_bus_axi_b_valid.value),
    )


IDLE = (1, 0, 0)
DATA = (0, 1, 0)
RESP = (0, 0, 1)


async def address(dut, awid: int) -> None:
    """The AW handshake: AWVALID for one cycle with AWREADY high."""
    assert state(dut) == IDLE, f"AWREADY must be 1 before the write, got {state(dut)}"
    dut.i_bus_axi_aw_valid.value = 1
    dut.i_bus_axi_aw_id.value = awid
    dut.i_bus_axi_aw_addr.value = 0x0000_0100
    dut.i_bus_axi_aw_len.value = 0
    dut.i_bus_axi_aw_burst.value = 1
    await cycle(dut)
    dut.i_bus_axi_aw_valid.value = 0
    dut.i_bus_axi_aw_id.value = 0


async def beat(dut, last: int) -> None:
    dut.i_bus_axi_w_valid.value = 1
    dut.i_bus_axi_w_last.value = last
    dut.i_bus_axi_w_data.value = 0xDEAD_BEEF
    dut.i_bus_axi_w_strb.value = 0xF
    await cycle(dut)
    dut.i_bus_axi_w_valid.value = 0
    dut.i_bus_axi_w_last.value = 0


async def response(dut, awid: int, wait: int) -> None:
    """BVALID with BID = AWID and BRESP = SLVERR, held `wait` cycles, then BREADY."""
    for _ in range(wait):
        assert state(dut) == RESP, f"BVALID must hold until BREADY, got {state(dut)}"
        assert int(dut.o_bus_axi_b_id.value) == awid, "BID must be the write's AWID"
        assert int(dut.o_bus_axi_b_resp.value) == SLVERR, "BRESP must be SLVERR"
        await cycle(dut)
    assert state(dut) == RESP
    assert int(dut.o_bus_axi_b_id.value) == awid, "BID must be the write's AWID"
    assert int(dut.o_bus_axi_b_resp.value) == SLVERR, "BRESP must be SLVERR"
    dut.i_bus_axi_b_ready.value = 1
    await cycle(dut)
    dut.i_bus_axi_b_ready.value = 0
    assert state(dut) == IDLE, f"after the B handshake the responder is idle, got {state(dut)}"


@cocotb.test()
async def test_idle_after_reset(dut):
    """ROM_004: after reset the responder is idle: AWREADY 1, WREADY 0, BVALID 0 (MAS 7.4),
    and it stays idle while no AWVALID comes."""
    await start(dut)
    for _ in range(5):
        assert state(dut) == IDLE, f"idle expected, got {state(dut)}"
        dut.i_bus_axi_w_valid.value = 1  # W without AW is not accepted (MAS 7.3)
        dut.i_bus_axi_w_last.value = 1
        dut.i_bus_axi_b_ready.value = 1
        await cycle(dut)
    dut.i_bus_axi_w_valid.value = 0
    dut.i_bus_axi_w_last.value = 0
    dut.i_bus_axi_b_ready.value = 0
    await cycle(dut)
    assert state(dut) == IDLE


@cocotb.test()
async def test_single_beat_write(dut):
    """ROM_004: a one-beat write is answered with BRESP = SLVERR and BID = AWID."""
    # verifies: ROM_004
    await start(dut)
    await address(dut, 0x5A)
    assert state(dut) == DATA, f"WREADY after the AW handshake, got {state(dut)}"
    await beat(dut, last=1)
    await response(dut, 0x5A, wait=3)


@cocotb.test()
async def test_burst_write(dut):
    """ROM_004: beats are dropped up to the one with WLAST; WLAST without WVALID is no
    beat; then the response, held until BREADY."""
    await start(dut)
    await address(dut, 0x23)
    for _ in range(3):
        await beat(dut, last=0)
        assert state(dut) == DATA, f"still taking beats, got {state(dut)}"
    dut.i_bus_axi_w_last.value = 1  # WLAST with WVALID low: not a beat
    await cycle(dut)
    dut.i_bus_axi_w_last.value = 0
    assert state(dut) == DATA, f"WLAST without WVALID is not a beat, got {state(dut)}"
    await cycle(dut)  # a cycle with no beat
    assert state(dut) == DATA
    await beat(dut, last=1)
    await response(dut, 0x23, wait=2)


@cocotb.test()
async def test_back_to_back_writes(dut):
    """ROM_004: each write gets its own AWID back; BREADY held high answers at once."""
    await start(dut)
    for awid in (0x01, 0x7F, 0x40):
        await address(dut, awid)
        await beat(dut, last=1)
        await response(dut, awid, wait=0)

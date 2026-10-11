"""The `dv/cocotb` skill's cocotb 2.x API table: the rows are checked against the cocotb
that is installed (the `sim` extra), so the table cannot drift from the library.
"""

from __future__ import annotations

from importlib import resources

import pytest

SKILL = (
    resources.files("chipgraph.packs.dv")
    .joinpath("skills", "dv", "cocotb.md")
    .read_text(encoding="utf-8")
)


def test_the_skill_does_not_tell_the_author_to_call_to_unsigned_on_any_port() -> None:
    assert "sample outputs with `int(dut.<name>.value)`" in SKILL
    assert "sample outputs with `dut.<name>.value.to_unsigned()`" not in SKILL


def test_the_table_has_the_rows_that_matter() -> None:
    for row in (
        "`int(dut.x.value)`",
        "`dut.irq.value.to_unsigned()`: `Logic` has no such method",
        'Clock(dut.clk, 10, unit="ns")',
        "`cocotb.fork`",
        "`BinaryValue` / `cocotb.binary` (removed)",
        '`@cocotb.test(timeout_time=50, timeout_unit="us")`',
    ):
        assert row in SKILL, row


def test_the_table_rows_hold_for_the_installed_cocotb() -> None:
    cocotb = pytest.importorskip("cocotb")
    from cocotb.clock import Clock
    from cocotb.triggers import ClockCycles, Timer
    from cocotb.types import Logic, LogicArray, Range

    vector = LogicArray(0xA5, Range(7, 0))
    assert int(vector) == vector.to_unsigned() == 0xA5
    assert vector == 0xA5
    assert isinstance(vector[0], Logic) and isinstance(vector[7:4], LogicArray)
    assert vector.to_signed() == -91
    assert LogicArray.from_unsigned(5, 8) == LogicArray("00000101")

    bit = Logic(1)
    assert int(bit) == 1 and bit == 1
    assert not hasattr(bit, "to_unsigned")
    assert Logic(1).is_resolvable and not LogicArray("1X10").is_resolvable
    with pytest.raises(ValueError):
        int(LogicArray("1X10"))

    Timer(10, unit="ns")
    assert hasattr(cocotb, "start_soon") and hasattr(cocotb, "start")
    assert not hasattr(cocotb, "fork") and not hasattr(cocotb, "coroutine")
    with pytest.raises(ModuleNotFoundError):
        __import__("cocotb.binary")
    assert "unit" in Clock.__init__.__annotations__ and ClockCycles is not None

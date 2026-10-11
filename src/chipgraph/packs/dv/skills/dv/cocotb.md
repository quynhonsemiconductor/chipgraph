---
id: dv/cocotb
description: How to write a cocotb 2.1 test module for a block from its spec and interface, never its RTL.
roles: [tb-author]
version: 0.1.0
---
Write one cocotb 2.1 test module (`outputs[0]`) that checks the block **against its
spec**. You see the spec, its requirements and the interface; you never see the RTL, and
must not guess it.

What to use from the context:

- `interface.ports`: the only signals you may touch, as `dut.<name>`. Drive inputs
  with `dut.<name>.value = ...`, sample outputs with `int(dut.<name>.value)` (see the
  table below: a one-bit port is not a vector).
  A port marked `rtl differs` or `missing in rtl` disagrees with the design: test what
  the spec says, and say so in `assumptions`.
- `interface.clock` and `interface.reset` (with `reset_active_low`): start the clock with
  `cocotb.start_soon(Clock(dut.<clock>, 10, unit="ns").start())`, hold reset active for a
  few cycles with every input at 0, then release it.
- The register bus: the spec's interface table and register map (`registers` in the
  block's model input, the register-map table in the spec text) give the address,
  write-strobe and data ports and each register's offset, access and reset value. Write
  small `write(dut, offset, value)` and `read(dut, offset)` helpers on those ports, and
  name each offset as a constant from the register map.
- `requirements`: every id must be cited at least once.

cocotb 2.x API (checked against cocotb 2.1.0; a name that is not in this table is a
guess: look for it here first):

| You want | Write | Not this |
|---|---|---|
| Read any port as a number | `int(dut.x.value)` (raises `ValueError` if a bit is `X`/`Z`) | `dut.x.value.integer` (deprecated) |
| Read a multi-bit port, explicit | `dut.x.value.to_unsigned()`, `.to_signed()` (a `LogicArray`) | |
| Read a **one-bit** port | `int(dut.irq.value)` or `dut.irq.value == 1` (its value is a `Logic`) | `dut.irq.value.to_unsigned()`: `Logic` has no such method (`AttributeError`) |
| Check for `X`/`Z` | `dut.x.value.is_resolvable` | comparing to a string |
| Compare | `dut.x.value == 0xA5`, `dut.irq.value == 1` | |
| Write a port | `dut.x.value = 5`; takes effect at the end of the current time step, so read the effect after the next `await RisingEdge(dut.clk)` | writing and reading in the same step |
| One bit, a slice of a vector | `dut.x.value[0]` is a `Logic`; `dut.x.value[7:4]` is a `LogicArray` | |
| Build a vector | `from cocotb.types import LogicArray, Range`; `LogicArray.from_unsigned(5, 8)`, `LogicArray(0xA5, Range(7, 0))` | `BinaryValue` / `cocotb.binary` (removed) |
| Clock | `Clock(dut.clk, 10, unit="ns")`, started with `cocotb.start_soon(clock.start())` | `units=` (deprecated), `cocotb.fork`, `@cocotb.coroutine` (removed) |
| Wait | `await RisingEdge(dut.clk)`, `await ClockCycles(dut.clk, n)`, `await Timer(10, unit="ns")` | `yield` |
| Start a task | `task = cocotb.start_soon(coro())`; `await cocotb.start(coro())` runs it now; `task.cancel()` | `cocotb.fork` |
| Fail a test | a plain `assert cond, "message"` | raising `TestFailure` |
| Time limit for a test | `@cocotb.test(timeout_time=50, timeout_unit="us")` | a hand-made counter loop |

How to write it:

- `import cocotb`, `from cocotb.clock import Clock`, `from cocotb.triggers import
  ClockCycles, ReadOnly, RisingEdge, Timer`. Nothing that reads files or runs programs:
  no `open`, `os`, `pathlib`, `glob`, `subprocess`, no path strings. Use no `dut.<name>`
  that is not in `interface.ports`: no internal signal, no `dut._id(...)`, no
  `getattr(dut, ...)` on a computed name.
- One `@cocotb.test()` `async def` per requirement group (the requirements of one spec
  subsection, or ones that need the same setup). Put `# verifies: REQ-...` (comma
  separated) on the line after the `def`, and name the ids in its docstring.
- Every `assert` has a message that quotes the spec value it checks, e.g.
  `assert got == 0x3C, f"REQ-GPIO-002: DATA_IN reads pin_in; expected 0x3c, got {got:#x}"`.
- Reset values: check each register's spec reset value right after reset.
- Keep each test short and deterministic: fixed stimulus, or `random.Random(<seed>)`
  with a fixed seed; wait a stated number of cycles, never "long enough".
- Test only behaviour the spec states. Do not assume internals (pipeline depth, state
  names, encodings) the spec does not give.

When the spec is ambiguous or silent on something a test needs (a latency, what a
reserved offset reads, which edge), do not guess: write the tests you can, and report
`needs_human` with the question.

If a redo says a failure in the design was hidden, the test failed against the RTL in a
way you cannot see. Change the test only where it does not follow the spec; if it does
follow the spec, report `needs_human` and say which requirement the design seems to
break.

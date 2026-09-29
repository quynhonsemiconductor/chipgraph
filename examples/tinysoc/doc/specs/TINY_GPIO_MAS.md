---
title: "TINY_GPIO"
subtitle: "MICRO-ARCHITECTURE SPECIFICATION -- V0.1"
author: "chipgraph example"
---

# Revision history

| Version | Date | Author | Reviewer | Description of change |
|---|---|---|---|---|
| V0.1 | 2026-01-01 | chipgraph | -- | First issue |

# 1. Overview

`tiny_gpio` is a minimal 8-bit GPIO block: an output data register, an input data
readback, and a per-bit direction register. The pad tri-state is left to the pad ring, so
this block only drives `pin_out` and `pin_dir` and reads `pin_in`. It is word-addressed
through a 2-bit address bus.

# 2. Features

- An 8-bit output data register driven onto `pin_out`.
- An 8-bit input readback of `pin_in`.
- An 8-bit per-bit direction register driven onto `pin_dir` (1 = output).

# 3. Block diagram

The block is in the `peri` clock and reset domain and sits behind a 4-bit word address bus.

# 4. IP used

: Upstream IP used

| From | Module | Commit | Licence |
|---|---|---|---|
| -- | -- | -- | -- |

# 5. Interface

: TINY_GPIO interface

| Signal | Dir | Width | Description |
|---|---|---:|---|
| `clk` | in | 1 | register clock |
| `rst_n` | in | 1 | asynchronous active-low reset |
| `addr` | in | 2 | word register address |
| `wr_en` | in | 1 | write strobe for `addr` |
| `wdata` | in | 32 | write data; only `[7:0]` is meaningful |
| `rdata` | out | 32 | read data for `addr` |
| `pin_in` | in | 8 | input pin values |
| `pin_out` | out | 8 | output pin values, from `DATA_OUT` |
| `pin_dir` | out | 8 | per-bit direction, from `DIR` |

# 6. Register map

All registers reset to 0. Addresses are word offsets of the 2-bit `addr`.

: Register map

| Offset | Register | Field | Bits | Access | Reset | Description |
|---|---|---|---|---|---|---|
| `0x0` | `DATA_OUT` | `DATA_OUT` | 7:0 | RW | 0 | driven onto `pin_out` |
| `0x1` | `DATA_IN` | `DATA_IN` | 7:0 | RO | 0 | current value of `pin_in` |
| `0x2` | `DIR` | `DIR` | 7:0 | RW | 0 | per-bit direction, 1 = output |

# 7. Functional behaviour

## 7.1 Output

`REQ-GPIO-001` A write to `DATA_OUT` appears on `pin_out` from the next clock and holds
until the next write.

## 7.2 Input

`REQ-GPIO-002` Reading `DATA_IN` returns the current `pin_in` value in bits 7:0 and 0 in
the upper bits.

## 7.3 Direction

`REQ-GPIO-003` A write to `DIR` appears on `pin_dir`; every other offset reads 0.

# 8. Instances

One instance in `tiny_top`.

# 9. What is not provided here, and who provides it

: Functions this block does not provide

| Function | Where it lives |
|---|---|
| pad tri-state | pad ring |

# 10. Tie-offs

: Tie-offs

| Port | Tied to | Why |
|---|---|---|
| `wdata[31:8]` | ignored | only the low 8 bits are used |

# 11. Requirements on others, and open items

: Requirements on other owners

| Item | Owner | What it blocks |
|---|---|---|
| `pin_dir` honoured by the pad ring | pad ring owner | direction control |

Open: pad ring pull configuration, from the pad ring owner.

# 12. Verification

1. Output (`REQ-GPIO-001`): a write to `DATA_OUT` drives `pin_out` and holds it.
2. Input (`REQ-GPIO-002`): `DATA_IN` reads back `pin_in` in bits 7:0; upper bits read 0.
3. Direction (`REQ-GPIO-003`): a write to `DIR` drives `pin_dir`; other offsets read 0.

# Appendix A. Acronyms

: Acronyms

| Acronym | Description |
|---|---|
| GPIO | General-purpose input/output |

---
title: "TINY_TIMER"
subtitle: "MICRO-ARCHITECTURE SPECIFICATION -- V0.1"
author: "chipgraph example"
---

# Revision history

| Version | Date | Author | Reviewer | Description of change |
|---|---|---|---|---|
| V0.1 | 2026-01-01 | chipgraph | -- | First issue |

# 1. Overview

`tiny_timer` is a free-running 32-bit counter with a compare register and a level
interrupt. It does not divide the clock and it has no prescaler: it counts one step per
enabled clock cycle. It is word-addressed through a 2-bit address bus.

# 2. Features

- A 32-bit counter that runs while enabled.
- A compare register that raises a level interrupt when the counter reaches it.
- A control register to enable counting and to clear a pending interrupt.

# 3. Block diagram

The block is in the `peri` clock and reset domain and sits behind a 4-bit word address bus.

# 4. IP used

: Upstream IP used

| From | Module | Commit | Licence |
|---|---|---|---|
| -- | -- | -- | -- |

# 5. Interface

: TINY_TIMER interface

| Signal | Dir | Width | Description |
|---|---|---:|---|
| `clk` | in | 1 | counter clock |
| `rst_n` | in | 1 | asynchronous active-low reset |
| `addr` | in | 2 | word register address |
| `wr_en` | in | 1 | write strobe for `addr` |
| `wdata` | in | 32 | write data |
| `rdata` | out | 32 | read data for `addr` |
| `irq` | out | 1 | level interrupt, high while pending |

# 6. Register map

All registers reset to 0. Addresses are word offsets of the 2-bit `addr`.

: Register map

| Offset | Register | Field | Bits | Access | Reset | Description |
|---|---|---|---|---|---|---|
| `0x0` | `COUNT` | `COUNT` | 31:0 | RW | 0 | free-running counter; a write loads it |
| `0x1` | `COMPARE` | `COMPARE` | 31:0 | RW | 0 | interrupt asserts once `COUNT` reaches this while enabled |
| `0x2` | `CTRL` | `EN` | 0 | RW | 0 | 1 = counter runs |
| | | `IRQ_CLR` | 1 | WO | 0 | write 1 to clear a pending interrupt; reads the pending flag |

# 7. Functional behaviour

## 7.1 Counting

`REQ-TIM-001` While `CTRL.EN` = 1, `COUNT` increments by one every clock; while
`CTRL.EN` = 0, `COUNT` holds.

`REQ-TIM-002` A write to `COUNT` loads it on the next clock, replacing the count.

## 7.2 Compare and interrupt

`REQ-TIM-003` `irq` goes high the cycle after `COUNT` equals `COMPARE` while enabled,
and stays high until cleared.

`REQ-TIM-004` Writing 1 to `CTRL.IRQ_CLR` clears a pending interrupt; `irq` returns to 0.

## 7.3 Read path

`REQ-TIM-005` Reading `CTRL` returns the enable bit in bit 0 and the pending-interrupt
flag in bit 1; every other offset reads 0.

# 8. Instances

One instance in `tiny_top`.

# 9. What is not provided here, and who provides it

: Functions this block does not provide

| Function | Where it lives |
|---|---|
| clock gating | top |

# 10. Tie-offs

: Tie-offs

| Port | Tied to | Why |
|---|---|---|
| -- | -- | -- |

# 11. Requirements on others, and open items

: Requirements on other owners

| Item | Owner | What it blocks |
|---|---|---|
| `addr` decoded from the low 2 bits of the word offset | bus owner | register decode |

Open: interrupt vector assignment, from the interrupt map owner.

# 12. Verification

1. Counting rate (`REQ-TIM-001`): `COUNT` increments once per cycle while enabled and
   holds while disabled.
2. Count load (`REQ-TIM-002`): a write to `COUNT` replaces the counter value.
3. Interrupt (`REQ-TIM-003`): `irq` rises the cycle after `COUNT` reaches `COMPARE`
   while enabled.
4. Interrupt clear (`REQ-TIM-004`): a write of 1 to `CTRL.IRQ_CLR` drops `irq`.
5. Read path (`REQ-TIM-005`): `CTRL` reads back enable and pending; other offsets read 0.

# Appendix A. Acronyms

: Acronyms

| Acronym | Description |
|---|---|
| IRQ | Interrupt request |

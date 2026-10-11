---
title: "TINY_PWM"
subtitle: "MICRO-ARCHITECTURE SPECIFICATION -- V0.1"
author: "chipgraph example"
---

# Revision history

| Version | Date | Author | Reviewer | Description of change |
|---|---|---|---|---|
| V0.1 | 2026-10-01 | chipgraph | -- | First issue (M2-04 acceptance fixture) |

# 1. Overview

`tiny_pwm` is a single-channel pulse-width modulator. An 8-bit counter runs from 0 to
`PERIOD` and wraps; the output is high while the counter is below `DUTY`. It is
word-addressed through a 2-bit address bus, like the other tinysoc blocks.

# 2. Features

- An 8-bit period and an 8-bit duty register.
- One PWM output, low while the block is disabled.
- A read path for every register.

# 3. Block diagram

The block is in tinysoc's single `clk`/`rst_n` domain, behind the 4-bit word address bus.

# 4. IP used

: Upstream IP used

| From | Module | Commit | Licence |
|---|---|---|---|
| -- | -- | -- | -- |

# 5. Interface

: TINY_PWM interface

| Signal | Dir | Width | Description |
|---|---|---:|---|
| `clk` | in | 1 | clock |
| `rst_n` | in | 1 | active-low reset |
| `addr` | in | 2 | word register address |
| `wr_en` | in | 1 | write strobe for `addr` |
| `wdata` | in | 32 | write data; only bits 7:0 are used |
| `rdata` | out | 32 | read data for `addr` |
| `pwm_out` | out | 1 | the PWM output |

# 6. Register map

All registers reset to 0. Addresses are word offsets of the 2-bit `addr`.

: Register map

| Offset | Register | Field | Bits | Access | Reset | Description |
|---|---|---|---|---|---|---|
| `0x0` | `PERIOD` | `PERIOD` | 7:0 | RW | 0 | the counter counts 0 to `PERIOD`, then wraps to 0 |
| `0x1` | `DUTY` | `DUTY` | 7:0 | RW | 0 | `pwm_out` is high while the counter is below `DUTY` |
| `0x2` | `CTRL` | `EN` | 0 | RW | 0 | 1 = the counter runs and `pwm_out` is driven |

# 7. Functional behaviour

## 7.1 Counter

`REQ-PWM-001` While `CTRL.EN` = 1 the counter increments every clock and wraps from
`PERIOD` to 0; while `CTRL.EN` = 0 it is held at 0.

## 7.2 Output

`REQ-PWM-002` `pwm_out` is 1 while `CTRL.EN` = 1 and the counter is below `DUTY`, and 0
otherwise.

## 7.3 Registers

`REQ-PWM-003` A write loads `PERIOD`, `DUTY` or `CTRL` from `wdata` on the next clock.

`REQ-PWM-004` Reading `PERIOD`, `DUTY` or `CTRL` returns its value in the low bits, zero
extended; offset `0x3` reads 0.

# 8. Instances

None yet: the block is not wired into `tiny_top`.

# 9. What is not provided here, and who provides it

: Functions this block does not provide

| Function | Where it lives |
|---|---|
| more channels | not provided |

# 10. Tie-offs

: Tie-offs

| Port | Tied to | Why |
|---|---|---|
| -- | -- | -- |

# 11. Requirements on others, and open items

: Requirements on other owners

| Item | Owner | What it blocks |
|---|---|---|
| -- | -- | -- |

# 12. Verification

1. Counter (`REQ-PWM-001`): the counter wraps after `PERIOD` and holds at 0 while disabled.
2. Output (`REQ-PWM-002`): `pwm_out` is high for `DUTY` cycles of every period.
3. Registers (`REQ-PWM-003`): each register loads from `wdata`.
4. Read path (`REQ-PWM-004`): each register reads back; offset `0x3` reads 0.

# Appendix A. Acronyms

: Acronyms

| Acronym | Description |
|---|---|
| PWM | Pulse-width modulation |

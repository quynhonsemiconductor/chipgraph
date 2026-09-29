<!-- Fixture copied from quynhonsemiconductor/vlsi_deep_training
     path: doc/specs/QNSC_Interrupt_Map_MAS.md  commit: 7a917d1
     Public, Apache-2.0. Verbatim; used to test the mas-markdown extractor. -->
---
title: "Interrupt Map"
subtitle: "MICRO-ARCHITECTURE SPECIFICATION -- V2.2"
author: "QUY NHON SEMICONDUCTORS -- QNSC"
---

# Revision history

The reasoning behind each change, and the versions before `V2.0`, are in
[`QNSC_Interrupt_Map_DECISIONS.md`](QNSC_Interrupt_Map_DECISIONS.md).

| Version | Date | Author | Reviewer | Description of change |
|---|---|---|---|---|
| V2.0 | 2026-09-23 | Nghia VT | -- | Rewritten as specification only. History and reasoning moved to `_DECISIONS`; tables in 7.2 and 10 generated from `util/qsoc_contract.yml` |
| V2.1 | 2026-09-24 | Nghia VT | -- | NMI stated as a wire through the block; source ports, widths and timer shape added; figure redrawn; reasoning moved to `_DECISIONS` |
| V2.2 | 2026-09-24 | Nghia VT | -- | GPIO3 dropped with the 40-pin package: 26 sources, `i_int_gpio` 3 bits |

# 1. Overview

`INTMAP` is a combinational block that connects 26 interrupt sources from 8 blocks
to 12 interrupt inputs of the Ibex core. The 25 maskable sources form **11 source
groups**; each group drives one fast line `irq_fast_i[n]`, through an OR gate when
the group has several sources and a wire when it has one. The watchdog bark is a
wire to the non-maskable input `irq_nm_i`.

**It does not**: hold state, decode an address, appear on any bus, prioritise
(the core does that), or latch a pulse. It has no clock and no reset.

Block directory `design/intmap`, module `m_qnsc_intmap`, owner Nghia Van Trong.

# 2. Features

- 26 sources onto **11 fast lines plus one NMI**; 4 fast lines spare, tied 0
- **One source group per line**: no line carries sources from two groups
- **Combinational**: no clock, no reset, no flip-flop, no bus port, no register on
  any path from input to output
- **Priority is the line index**, resolved by the core, lowest index first
- **The watchdog bark is a wire** from `i_int_wdt_bark` to `o_int_nm`, never ORed

# 3. Block diagram

![INTMAP: 5 OR gates and 6 wires onto irq_fast_i[10:0], one wire onto irq_nm_i](../figures/img/fig_intr_map.png)

One combinational layer, no clock, reset or bus port. The tie-offs are in `design/top`.

# 4. IP used

: Upstream IP used

| From | Module | Commit | Licence |
|---|---|---|---|
| -- | -- | -- | -- |

**Designed in house.** The block instantiates nothing; it is `assign` statements.

# 5. Interface

Every port, named per `QNSC_RTL_Design_Naming_Rule` V1.0 section 3.6. The vendor
port behind each input is in the table in 7.2.

: Interrupt map interface

| Signal | Dir | Width | Description |
|---|---|---:|---|
| `i_int_dma` | in | 1 | from DMA |
| `i_int_spi_device` | in | 8 | from SPI device, bits in port declaration order |
| `i_int_spi_host` | in | 2 | from SPI host, bit 0 error, bit 1 event |
| `i_int_i2c` | in | 1 | from I2C |
| `i_int_uart_0` | in | 1 | from UART0 |
| `i_int_uart_1` | in | 1 | from UART1 |
| `i_int_timer_1` | in | 2 | from TIMER1, bit 0 `irq_lo_o`, bit 1 `irq_hi_o` |
| `i_int_pwm` | in | 4 | from PWM, bit *n* = `events_o[n]` |
| `i_int_wdt_wakeup` | in | 1 | from WDT, wake-up timer |
| `i_int_gpio` | in | 3 | from GPIO0-2, bit *n* = GPIO*n* |
| `i_int_timer_0` | in | 1 | from TIMER0, 64-bit mode |
| `i_int_wdt_bark` | in | 1 | from WDT, bark |
| `o_int_fast` | out | 11 | to Ibex `irq_fast_i[10:0]` |
| `o_int_nm` | out | 1 | to Ibex `irq_nm_i` |

**26 input bits, 12 output bits, and no other ports.** No `i_clk_*`, no `i_rst_n_*`.

# 6. Register map

None. The block has no address and is not a bus slave. Enabling is in the core
(`mie` bits 16-26, `mstatus.MIE`); clearing is at each source (7.4).

# 7. Functional behaviour

## 7.1 One OR gate per multi-source group

```systemverilog
assign o_int_fast[0]  =  i_int_dma;
assign o_int_fast[1]  = |i_int_spi_device;
assign o_int_fast[2]  = |i_int_spi_host;
assign o_int_fast[3]  =  i_int_i2c;
assign o_int_fast[4]  =  i_int_uart_0;
assign o_int_fast[5]  =  i_int_uart_1;
assign o_int_fast[6]  = |i_int_timer_1;
assign o_int_fast[7]  = |i_int_pwm;
assign o_int_fast[8]  =  i_int_wdt_wakeup;
assign o_int_fast[9]  = |i_int_gpio;
assign o_int_fast[10] =  i_int_timer_0;
assign o_int_nm       =  i_int_wdt_bark;
```

Five outputs are OR reductions and seven are wires. **Zero flip-flops**: every
output follows its inputs combinationally, with no cycle of delay.

## 7.2 Line assignment

**Line** *n* is the wire `irq_fast_i[n]`. Ibex takes the lowest pending index
first, so the line order is the priority order; changing it is a re-synthesis.
For line *n*, `mcause` = `16 + n`, equal to the `mie` bit that enables it
(`ibex_pkg.sv`, `CSR_MFIX_BIT_LOW = 16`). `mtvec` is always in vectored mode, so
the core jumps to `mtvec + 4 * mcause`. No register is read and no bus
transaction occurs on the interrupt path.

<!-- gen:interrupt_lines -->
: Interrupt line assignment

| Line | CPU port | mcause | Vector | INTMAP input | Source port | Width | Shape |
|---:|---|---:|---|---|---|---:|---|
| 0 | `irq_fast_i[0]` | 16 | `mtvec + 0x40` | `i_int_dma` | `o_int_dma` | 1 | level |
| 1 | `irq_fast_i[1]` | 17 | `mtvec + 0x44` | `i_int_spi_device` | `intr_*_o` ×8 | 8 | level |
| 2 | `irq_fast_i[2]` | 18 | `mtvec + 0x48` | `i_int_spi_host` | `intr_error_o`, `intr_spi_event_o` | 2 | level |
| 3 | `irq_fast_i[3]` | 19 | `mtvec + 0x4C` | `i_int_i2c` | `interrupt_o` | 1 | level |
| 4 | `irq_fast_i[4]` | 20 | `mtvec + 0x50` | `i_int_uart_0` | `INT` | 1 | level |
| 5 | `irq_fast_i[5]` | 21 | `mtvec + 0x54` | `i_int_uart_1` | `INT` | 1 | level |
| 6 | `irq_fast_i[6]` | 22 | `mtvec + 0x58` | `i_int_timer_1` | `irq_lo_o`, `irq_hi_o` | 2 | pulse; level in one-shot with prescaler or ref clock |
| 7 | `irq_fast_i[7]` | 23 | `mtvec + 0x5C` | `i_int_pwm` | `events_o[3:0]` | 4 | pulse |
| 8 | `irq_fast_i[8]` | 24 | `mtvec + 0x60` | `i_int_wdt_wakeup` | `intr_wkup_timer_expired_o` | 1 | level |
| 9 | `irq_fast_i[9]` | 25 | `mtvec + 0x64` | `i_int_gpio` | `interrupt` ×3 | 3 | pulse |
| 10 | `irq_fast_i[10]` | 26 | `mtvec + 0x68` | `i_int_timer_0` | `irq_lo_o` | 1 | pulse; level in one-shot with prescaler or ref clock |
| 11-14 | `irq_fast_i[14:11]` | 27-30 | -- | tied 0 in `design/top` | -- | 0 | -- |
| -- | `irq_nm_i` | **31** | `mtvec + 0x7C` | **`i_int_wdt_bark`** | `nmi_wdog_timer_bark_o` | 1 | level |
<!-- /gen -->

<!-- gen:interrupt_totals -->
: Interrupt source totals

|  | Count |
|---|---:|
| Sources onto fast lines | 25 |
| Sources on the non-maskable input | 1 |
| **Total interrupt sources** | **26** |
| Source blocks (DMA, SPI, I2C, UART, TIMER, PWM, WDT, GPIO) | 8 |
| Sources that pulse | 10 |
| Fast lines driven | 11 |
| Fast lines Ibex provides | 15 |
| Fast lines spare | 4 |
<!-- /gen -->

## 7.3 The NMI is a wire through this block

`o_int_nm` equals `i_int_wdt_bark` and depends on no other input; no fast line
depends on `i_int_wdt_bark`. `irq_nm_i` is not masked by `mstatus.MIE` or `mie`.
Ibex ignores `irq_nm_i` in Debug Mode; the bark is a held level, so it traps
after the core resumes.

## 7.4 A missed pulse, and how each source is cleared

Ibex's `mip` is combinational (`assign mip.irq_fast = irq_fast_i`) and this block
latches nothing, so a pulse that ends while `mstatus.MIE` (clear inside any
handler) or the `mie` bit is clear, or in Debug Mode, raises no trap.

: How each source is cleared, and what a missed pulse costs

| Source | Shape | Cleared by | A missed pulse |
|---|---|---|---|
| DMA | level | W1C `DMA_ISR` | -- |
| SPI device, SPI host | level | W1C `INTR_STATE` | -- |
| I2C | level | `IACK`, bit 0 of `CMD` | -- |
| UART0, UART1 | level | the 16550 register read that `IIR` names | -- |
| WDT wake-up, WDT bark | level | W1C `INTR_STATE` | -- |
| GPIO0-2 | pulse | nothing; `INTSTATUS` clears on read | lost; the pin stays recorded in `INTSTATUS` |
| TIMER0, TIMER1 | pulse; level in one-shot with prescaler or ref clock | a held level ends on the writes in `QNSC_TIMER_MAS` 7.5 | lost; periodic mode raises it again next period, one-shot does not |
| PWM | pulse | nothing | lost; the next period raises it again |

## 7.5 A gated peripheral holds its line

All interrupt sources are in the `peri` cluster, whose clocks `SCRC` stops
through `CLK_EN`. A stopped clock retains flip-flop state, so a peripheral gated
with its interrupt asserted keeps its line high, and this block passes it on.
Clearing it needs a register write to the gated peripheral, which cannot
complete, so the core re-enters that handler while the `mie` bit is set.
**Firmware clears a peripheral's interrupt before gating it** (11).

# 8. Instances

One. It is instantiated in `design/top` and has no parameters.

# 9. What is not provided here, and who provides it

: Functions this block does not provide

| Function | Where it lives |
|---|---|
| Pending latch | the source's status register, or none (7.4) |
| Which event fired | the source's status register; `TIMER1` (lo/hi) and `PWM` have none, see their MAS |
| Which group fired | `mcause`, via the vectored trap address |
| Priority resolution | Ibex, by fast-line index |
| Per-line enable | `mie` bits 16-26, in the core |
| Acknowledge | the source's own clear (7.4) |

# 10. Every interrupt input on the core

<!-- gen:core_tie_offs -->
: Every interrupt input on the core

| Core input | Width | Driven by | Note |
|---|---:|---|---|
| `irq_fast_i[10:0]` | 11 | **`o_int_fast`, this block** | 11 source groups, one per line, `mcause` 16-26 |
| `irq_fast_i[14:11]` | 4 | tied `4'b0` in `design/top` | spare; `mcause` 27-30 never raised |
| `irq_nm_i` | 1 | **`o_int_nm`, this block** | wdt_bark wire, `mcause` 31, outside `mie` and `mstatus.MIE` |
| `irq_external_i` | 1 | tied `0` in `design/top` | no PLIC; `mcause` 11 never raised |
| `irq_timer_i` | 1 | tied `0` in `design/top` | no CLINT `mtime`/`mtimecmp`; `mip.MTIP` stays 0 |
| `irq_software_i` | 1 | tied `0` in `design/top` | no CLINT `msip`; `mip.MSIP` stays 0 |
<!-- /gen -->

# 11. Requirements on others, and open items

: Requirements on other owners

| Item | Owner | What it blocks |
|---|---|---|
| Vector table entries at `mtvec + 0x40` to `+0x68` and `+0x7C`; `mtvec` 256-byte aligned, because Ibex ignores `mtvec[7:0]` | firmware owner | every interrupt |
| Handlers installed before `mstatus.MIE` is set, and the NMI entry at `mtvec + 0x7C` before the watchdog is enabled | firmware owner | every source is live from the first cycle out of reset; the NMI ignores `mstatus.MIE` |
| Firmware clears a peripheral's interrupt before closing its `CLK_EN` gate (7.5); written in the programming guide. `SCRC` needs no change | firmware owner | a handler that cannot clear its own source |
| Align HAS lines 69 and 155 with 7.3: the bark passes through `INTMAP` as a wire, so `INTMAP` takes 26 sources (25 maskable + the NMI). The HAS also still counts four GPIO instances; QSOC has three | HAS owner | consistency between HAS and MAS |

**Open on this block**, blocking nobody:

1. **Timing and area.** Deepest path: the 8-input OR on `spi_device`. The numbers
   come from the first synthesis run.
2. **X on an input.** An X passes the OR; `ibex_top.sv` `IbexIrqX` checks only the
   bundle. Open: whether this block asserts each `i_int_*` known.

# 12. Verification

Ten checks, none needing a bus model:

1. Each single-source input raises exactly its own line.
2. Each OR group raises its line for every member, individually.
3. No input raises a line other than its own.
4. `irq_fast_i[14:11]`, `irq_external_i`, `irq_timer_i`, `irq_software_i` are
   always 0 at the core boundary.
5. `i_int_wdt_bark` reaches `o_int_nm` and no fast line; no other input reaches
   `o_int_nm`.
6. Simultaneous inputs raise each affected line once.
7. Output follows input combinationally, with no cycle of delay.
8. `mcause` and vector mapping match the table in 7.2 against `qnsc_pkg`.
9. A held input keeps its line asserted for as long as it is held (7.5).
10. **Lint check**: the module contains no `i_clk_`, no `i_rst_n_`, and no
    `always_ff`.

# Appendix A. Acronyms

: Acronyms

| Acronym | Description |
|---|---|
| CLINT | Core Local Interruptor -- RISC-V timer and software interrupt block. Not present in QSOC |
| INTMAP | Interrupt Map -- this block |
| `mcause` | Machine Cause register: why the core trapped |
| `mie` | Machine Interrupt Enable register: per-line mask |
| `mip` | Machine Interrupt Pending register |
| `mstatus.MIE` | The global interrupt enable bit |
| `mtvec` | Machine Trap Vector: base of the trap table |
| NMI | Non-Maskable Interrupt -- `irq_nm_i` on Ibex |
| PLIC | Platform Level Interrupt Controller. Not present in QSOC |
| SCRC | System Clock Reset Control |
| W1C | Write-1-to-Clear |
| WDT | Watchdog Timer (`aon_timer`) |

# Appendix B. First review

: First review

| Item | Reviewer | Response |
|---|---|---|
| GPIO: one line for the three instances, or three lines? | Day005, 2026-09-18 | One line. Which pin fired is in the instance's `INTSTATUS` (7.4) |
| Is the DMA interrupt a pulse or a level? | DMA owner | Level, held by W1C `DMA_ISR` (7.4) |
| Does this block need to latch pending? | -- | No. Every level source holds its line; a missed pulse is recorded or repeats (7.4) |
| Is the non-maskable interrupt in the totals and the core-input table? | Teacher, 2026-09-23 | Yes: it is counted in the 26 (7.2) and listed in section 10 |
| What happens to a line whose peripheral is clock-gated? | -- | It stays asserted (7.5). Firmware clears the source before gating it; `SCRC` needs no change (11) |

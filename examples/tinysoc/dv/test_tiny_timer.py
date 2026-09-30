# SPDX-License-Identifier: Apache-2.0
# chipgraph example: tiny_timer testbench stubs.
#
# These are NOT run by anything yet: there is no simulator wired to tinysoc's dv/. They
# exist so the `trace` cross check (M1-07) has a test that references each declared
# REQ-ID of the timer block. Each function names, in a `# verifies:` comment, the
# requirement it stands in for; the `trace` check does a word-boundary search for those
# IDs across the files under its `tests` globs.


def test_counting():
    # verifies: REQ-TIM-001
    # While CTRL.EN = 1, COUNT increments by one every clock; while 0 it holds.
    raise NotImplementedError("no simulator wired to tinysoc dv yet")


def test_count_load():
    # verifies: REQ-TIM-002
    # A write to COUNT loads it on the next clock, replacing the count.
    raise NotImplementedError("no simulator wired to tinysoc dv yet")


def test_compare_interrupt():
    # verifies: REQ-TIM-003
    # irq goes high the cycle after COUNT equals COMPARE while enabled.
    raise NotImplementedError("no simulator wired to tinysoc dv yet")


def test_interrupt_clear():
    # verifies: REQ-TIM-004
    # Writing 1 to CTRL.IRQ_CLR clears a pending interrupt.
    raise NotImplementedError("no simulator wired to tinysoc dv yet")


def test_read_path():
    # verifies: REQ-TIM-005
    # Reading CTRL returns enable in bit 0 and pending in bit 1; offset 0x3 reads 0.
    raise NotImplementedError("no simulator wired to tinysoc dv yet")

# SPDX-License-Identifier: Apache-2.0
# chipgraph example: tiny_gpio testbench stubs.
#
# These are NOT run by anything yet: there is no simulator wired to tinysoc's dv/. They
# exist so the `trace` cross check (M1-07) has a test that references each declared
# REQ-ID of the gpio block. Each function names, in a `# verifies:` comment, the
# requirement it stands in for; the `trace` check searches for those IDs across the files
# under its `tests` globs.


def test_output():
    # verifies: REQ-GPIO-001
    # A write to DATA_OUT appears on pin_out from the next clock and holds.
    raise NotImplementedError("no simulator wired to tinysoc dv yet")


def test_input():
    # verifies: REQ-GPIO-002
    # Reading DATA_IN returns pin_in in bits 7:0 and 0 in the upper bits.
    raise NotImplementedError("no simulator wired to tinysoc dv yet")


def test_direction():
    # verifies: REQ-GPIO-003
    # A write to DIR appears on pin_dir from the next clock; offset 0x3 reads 0.
    raise NotImplementedError("no simulator wired to tinysoc dv yet")

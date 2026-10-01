"""/triage's question for a failing simulation of a top-level design: the failure lines of
a testbench that reports with assertions (`$error`), and the spec lines they get.

The logs here are synthetic, written for these tests: a chip-level testbench driving
tinysoc's register bus, reporting a failed check as an assertion, not as a `FAIL` line.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ask_helpers import copy_tinysoc, ingested_tinysoc

from chipgraph.app.context import AppContext
from chipgraph.packs.assist.triage import LABELS
from chipgraph.packs.assist.triage.contract import SpecLine
from chipgraph.packs.assist.triage.parse import ParsedLog, parse_log
from chipgraph.packs.assist.triage.question import (
    MAX_MAP_LINES,
    MAX_SPEC_LINES,
    build_question,
    failure_lines,
    spec_lines,
    spec_sources,
)
from chipgraph.packs.assist.triage.rules import TriageFacts, tb_patterns

# A Verilator run of a chip-level testbench: an immediate assertion (`$error`) on a
# register read back through the bus, then the testbench's own summary assertion.
VERILATOR_TOP = (
    "[20000] reset released\n"
    "[33000] bus write 0x1 <= 0x00000010\n"
    "[45000] bus read  0x1 => 0x00000000\n"
    "[45000] %Error: tb_chip.sv:58: Assertion failed in tb_chip.check_read: COMPARE read "
    "back through the bus: got 0x00000000, want 0x00000010\n"
    "-Info: tb/tb_chip.sv:58: Verilog $stop, ignored due to +verilator+error+limit\n"
    "[90000] %Fatal: tb_chip.sv:99: Assertion failed in tb_chip: tb_chip: 1 check(s) failed\n"
    "%Error: tb/tb_chip.sv:99: Verilog $stop\n"
    "Aborting...\n"
)

# The same kind of test on another simulator: `** Error:` with the time on the line, and
# a failure that names the design only by a top-level port and an instance path.
QUESTA_TOP = (
    "[40 ns] bus write 0x5 <= 0x000000ff\n"
    "** Error: tb_chip.sv(71): dut.u_gpio.dir_q stays 0x00 after the bus write, "
    "gpio_pin_dir low   Time: 60 ns  Scope: tb_chip File: tb_chip.sv Line: 71\n"
    "** Note: $finish    : tb_chip.sv(90)\n"
)

FAIL_STYLE = (
    "tb: read  addr=0x2 data=0x00\n"
    "FAIL CTRL after enable: expected 0x1 got 0x0 (rst_n=1)\n"
    "[61000] %Fatal: tb_timer.sv:80: Assertion failed in tb_timer: tb_timer failed\n"
    "%Error: tb/tb_timer.sv:80: Verilog $stop\n"
)


@pytest.fixture(scope="module")
def tinysoc(tmp_path_factory: pytest.TempPathFactory) -> AppContext:
    return ingested_tinysoc(tmp_path_factory.mktemp("triage_q") / "tinysoc")


def _facts(ctx: AppContext, parsed: ParsedLog) -> TriageFacts:
    profile = ctx.require_profile().profile
    return TriageFacts(
        parsed=parsed, check_id=None, check_kinds=(), tb_patterns=tb_patterns(profile.layout)
    )


def _text(lines: tuple[SpecLine, ...]) -> str:
    return "\n".join(f"{line.citation} {line.defined_at} {line.text}" for line in lines)


# --- parse: assertion messages are failure lines ---------------------------------------


def test_assertion_messages_are_read_and_summaries_dropped() -> None:
    parsed = parse_log(VERILATOR_TOP)
    assert parsed.sim_assertions == (
        "COMPARE read back through the bus: got 0x00000000, want 0x00000010",
    )
    # what the triage rules read is unchanged: no FAIL-style line in this log
    assert parsed.sim_failures == ()
    assert failure_lines(parsed) == parsed.sim_assertions


def test_a_questa_error_line_is_a_failure_line() -> None:
    parsed = parse_log(QUESTA_TOP)
    assert parsed.sim_assertions == (
        "dut.u_gpio.dir_q stays 0x00 after the bus write, gpio_pin_dir low",
    )


def test_fail_lines_come_first_and_a_bare_assertion_adds_nothing() -> None:
    parsed = parse_log(FAIL_STYLE)
    # 'tb_timer failed' says only that the testbench failed
    assert parsed.sim_assertions == ()
    assert failure_lines(parsed) == parsed.sim_failures


# --- spec lines for a top-level failure --------------------------------------------------


def test_a_top_level_assertion_gets_the_register_and_the_address_map(
    tinysoc: AppContext,
) -> None:
    parsed = parse_log(VERILATOR_TOP)
    specs, labels = spec_lines(tinysoc, parsed, _facts(tinysoc, parsed))
    text = _text(specs)
    citations = [s.citation for s in specs]
    # the register the failure names, with its offset
    assert "model:register:timer.COMPARE" in citations
    assert "register COMPARE: block=block:timer; offset=0x1" in text
    # the chip-level address map of the bus it sits on
    assert "model:memory_region:timer" in citations
    assert "model:memory_region:gpio" in citations
    assert "base=0x0; size=4" in text and "base=0x4; size=4" in text
    assert len(specs) <= MAX_SPEC_LINES + MAX_MAP_LINES
    for line in specs:
        assert not line.location.startswith(("rtl/", "tb/")), line
    assert labels == ("internal",)


def test_an_instance_path_and_a_top_port_name_their_block(tinysoc: AppContext) -> None:
    parsed = parse_log(QUESTA_TOP)
    specs, _ = spec_lines(tinysoc, parsed, _facts(tinysoc, parsed))
    citations = [s.citation for s in specs]
    assert "model:memory_region:gpio" in citations
    assert "model:memory_region:timer" in citations
    assert citations.index("model:memory_region:gpio") < citations.index(
        "model:memory_region:timer"
    )


def test_the_question_shows_the_assertion_and_the_spec_lines(tinysoc: AppContext) -> None:
    parsed = parse_log(VERILATOR_TOP)
    facts = _facts(tinysoc, parsed)
    specs, labels = spec_lines(tinysoc, parsed, facts)
    question = build_question(parsed, facts, specs=specs, labels=labels)
    assert question.choices == LABELS
    assert "Simulation self-check failures:\n- COMPARE read back through the bus" in (
        question.context
    )
    assert "Spec lines (from the project's Design Model" in question.context
    assert "memory_region gpio: auto=false; base=0x4" in question.context


def test_map_entries_from_an_nda_file_are_never_shown(tmp_path: Path) -> None:
    ctx = ingested_tinysoc(tmp_path / "t", profile_extra="data:\n  nda_paths: ['chip.yml']\n")
    parsed = parse_log(VERILATOR_TOP)
    specs, labels = spec_lines(ctx, parsed, _facts(ctx, parsed))
    assert specs
    assert not any(s.location.startswith("chip.yml") for s in specs)
    assert "nda" not in labels


def test_no_design_model_says_none_found(tmp_path: Path) -> None:
    ctx = AppContext.load(copy_tinysoc(tmp_path / "t"))
    parsed = parse_log(VERILATOR_TOP)
    facts = _facts(ctx, parsed)
    assert spec_lines(ctx, parsed, facts) == ((), ())
    question = build_question(parsed, facts)
    assert "Spec lines: none found" in question.context


# --- FAIL-style failures keep their retrieval ---------------------------------------------


def test_a_fail_line_keeps_its_retrieval_then_adds_the_map(tinysoc: AppContext) -> None:
    parsed = parse_log(FAIL_STYLE)
    facts = _facts(tinysoc, parsed)
    specs, _ = spec_lines(tinysoc, parsed, facts)
    retrieved, _ = spec_sources(tinysoc, "CTRL after enable: expected 0x1 got 0x0 (rst_n=1)", facts)
    assert retrieved
    assert specs[: len(retrieved)] == retrieved
    added = [s.citation for s in specs[len(retrieved) :]]
    assert "model:memory_region:timer" in added
    assert len(added) <= MAX_MAP_LINES

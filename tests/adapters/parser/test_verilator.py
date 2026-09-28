"""Tests for `VerilatorParser`, against real (5.052) and hand-written (5.020) fixtures."""

from __future__ import annotations

from pathlib import Path

from chipgraph.adapters.parser.verilator import VerilatorParser
from chipgraph.core.plugin_api.protocols import LogParser

_LOGS = Path(__file__).parent / "logs"


def test_is_a_log_parser() -> None:
    parser = VerilatorParser()
    assert isinstance(parser, LogParser)
    assert parser.name == "verilator"


def test_parses_real_verilator_5052_log() -> None:
    # Captured with `verilator --lint-only -Wall` (Verilator 5.052, /opt/homebrew/bin/verilator).
    log = (_LOGS / "verilator_5052.log").read_text()
    issues = VerilatorParser().parse(log)

    assert len(issues) == 4

    width_trunc, unused_bits, unused_signal, pin_not_found = issues

    assert width_trunc.file == "combo2.sv"
    assert width_trunc.line == 15
    assert width_trunc.severity == "warning"
    assert width_trunc.rule == "WIDTHTRUNC"
    assert "expects 4 bits" in width_trunc.msg

    assert unused_bits.file == "combo2.sv"
    assert unused_bits.line == 2
    assert unused_bits.severity == "warning"
    assert unused_bits.rule == "UNUSEDSIGNAL"
    assert "not used" in unused_bits.msg

    assert unused_signal.file == "combo2.sv"
    assert unused_signal.line == 8
    assert unused_signal.severity == "warning"
    assert unused_signal.rule == "UNUSEDSIGNAL"
    assert "w_x" in unused_signal.msg

    assert pin_not_found.file == "combo.sv"
    assert pin_not_found.line == 23
    assert pin_not_found.severity == "error"
    assert pin_not_found.rule == "PINNOTFOUND"
    assert "Pin not found: 'x'" in pin_not_found.msg

    # Summary lines ("%Error: Exiting due to N ...") and indented continuation/context
    # lines must not turn into issues.
    assert all(issue.rule != "" for issue in issues)
    assert not any("Exiting due to" in issue.msg for issue in issues)


def test_parses_hand_written_5020_log() -> None:
    # Hand-written in the Verilator 5.020 line shape (same file:line[:col]: msg shape,
    # but %Warning-UNUSED has no column and predates the %Warning-UNUSEDSIGNAL rename).
    log = (_LOGS / "verilator_5020.log").read_text()
    issues = VerilatorParser().parse(log)

    assert len(issues) == 2

    unused, pin_not_found = issues

    assert unused.file == "design/a.sv"
    assert unused.line == 7
    assert unused.severity == "warning"
    assert unused.rule == "UNUSED"
    assert "w_x" in unused.msg

    assert pin_not_found.file == "file.sv"
    assert pin_not_found.line == 224
    assert pin_not_found.severity == "error"
    assert pin_not_found.rule == "PINNOTFOUND"
    assert "Pin not found: 'x'" in pin_not_found.msg


def test_ignores_continuation_and_summary_lines() -> None:
    log = "\n".join(
        [
            "%Warning-WIDTH: a.sv:7: bits truncated",
            "                 ... For warning description see https://verilator.org/warn/WIDTH",
            "    7 | assign y = a;",
            "      |          ^",
            "%Error: Exiting due to 1 warning(s)",
        ]
    )
    issues = VerilatorParser().parse(log)
    assert len(issues) == 1
    assert issues[0].file == "a.sv"
    assert issues[0].line == 7
    assert issues[0].rule == "WIDTH"
    assert issues[0].severity == "warning"

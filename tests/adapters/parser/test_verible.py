"""Tests for `VeribleParser`, against a real `verible-verilog-lint` fixture."""

from __future__ import annotations

from pathlib import Path

from chipgraph.adapters.parser.verible import VeribleParser
from chipgraph.core.plugin_api.protocols import LogParser

_LOGS = Path(__file__).parent / "logs"


def test_is_a_log_parser() -> None:
    parser = VeribleParser()
    assert isinstance(parser, LogParser)
    assert parser.name == "verible"


def test_parses_real_verible_log() -> None:
    # Captured with `verible-verilog-lint --ruleset=all` (v0.0-4296-g0f262651).
    log = (_LOGS / "verible.log").read_text()
    issues = VeribleParser().parse(log)

    assert len(issues) == 4

    input_suffix, output_suffix, signal_style, syntax = issues

    assert input_suffix.file == "verible_lint.sv"
    assert input_suffix.line == 1
    assert input_suffix.severity == "warning"
    assert input_suffix.rule == "port-name-suffix"
    assert "input port names" in input_suffix.msg
    assert "[port-name-suffix]" not in input_suffix.msg

    assert output_suffix.rule == "port-name-suffix"
    assert output_suffix.severity == "warning"

    assert signal_style.file == "verible_lint.sv"
    assert signal_style.line == 2
    assert signal_style.severity == "warning"
    assert signal_style.rule == "signal-name-style"
    assert "naming convention" in signal_style.msg

    assert syntax.file == "verible_syntax.sv"
    assert syntax.line == 3
    assert syntax.severity == "error"
    assert syntax.rule == "syntax"
    assert "syntax error" in syntax.msg


def test_single_column_form_without_range() -> None:
    log = "file.sv:10:3: message here [rule-name]"
    issues = VeribleParser().parse(log)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.file == "file.sv"
    assert issue.line == 10
    assert issue.rule == "rule-name"
    assert issue.severity == "warning"
    assert issue.msg == "message here"


def test_range_form_with_style_and_rule_brackets() -> None:
    log = "path/file.sv:10:3-8: message [Style: some-style] [some-rule]"
    issues = VeribleParser().parse(log)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.file == "path/file.sv"
    assert issue.line == 10
    assert issue.rule == "some-rule"
    assert issue.severity == "warning"


def test_syntax_error_form() -> None:
    log = "path:12:1: syntax error at token 'endmodule'"
    issues = VeribleParser().parse(log)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "error"
    assert issue.rule == "syntax"

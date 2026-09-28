"""Tests for `GenericRegexParser`."""

from __future__ import annotations

import pytest

from chipgraph.adapters.parser.generic_regex import GenericRegexParser
from chipgraph.core.plugin_api.protocols import LogParser


def test_is_a_log_parser() -> None:
    parser = GenericRegexParser(pattern=r"(?P<file>\S+):(?P<line>\d+): (?P<msg>.*)")
    assert isinstance(parser, LogParser)
    assert parser.name == "generic-regex"


def test_happy_path_default_severity() -> None:
    parser = GenericRegexParser(
        pattern=r"(?P<file>\S+):(?P<line>\d+): (?P<rule>\w+): (?P<msg>.*)",
        default_severity="error",
    )
    log = "a.sv:12: bad_rule: something went wrong\nb.sv:3: other_rule: also wrong"
    issues = parser.parse(log)
    assert len(issues) == 2
    assert issues[0].file == "a.sv"
    assert issues[0].line == 12
    assert issues[0].rule == "bad_rule"
    assert issues[0].severity == "error"
    assert issues[0].msg == "something went wrong"


def test_severity_group_maps_prefixes() -> None:
    parser = GenericRegexParser(
        pattern=r"(?P<severity>\w+): (?P<file>\S+):(?P<line>\d+): (?P<msg>.*)",
        severity_group="severity",
    )
    log = "\n".join(
        [
            "ERROR: a.sv:1: bad thing",
            "WARNING: a.sv:2: iffy thing",
            "INFO: a.sv:3: fyi",
        ]
    )
    issues = parser.parse(log)
    assert [i.severity for i in issues] == ["error", "warning", "info"]


def test_default_severity_group_named_severity() -> None:
    parser = GenericRegexParser(
        pattern=r"(?P<severity>\w+): (?P<file>\S+):(?P<line>\d+): (?P<msg>.*)",
    )
    log = "warning: a.sv:1: iffy thing"
    issues = parser.parse(log)
    assert issues[0].severity == "warning"


def test_missing_msg_group_raises_value_error() -> None:
    with pytest.raises(ValueError, match="msg"):
        GenericRegexParser(pattern=r"(?P<file>\S+):(?P<line>\d+): .*")


def test_missing_file_or_line_group_raises_value_error() -> None:
    with pytest.raises(ValueError):
        GenericRegexParser(pattern=r"(?P<line>\d+): (?P<msg>.*)")


def test_invalid_regex_raises_value_error() -> None:
    with pytest.raises(ValueError):
        GenericRegexParser(pattern=r"(?P<file>\S+):(?P<line>\d+): (?P<msg>.*")


def test_unknown_severity_group_raises_value_error() -> None:
    with pytest.raises(ValueError):
        GenericRegexParser(
            pattern=r"(?P<file>\S+):(?P<line>\d+): (?P<msg>.*)",
            severity_group="nope",
        )


def test_no_matches_returns_empty_tuple() -> None:
    parser = GenericRegexParser(pattern=r"(?P<file>\S+):(?P<line>\d+): (?P<msg>.*)")
    assert parser.parse("nothing matches here") == ()

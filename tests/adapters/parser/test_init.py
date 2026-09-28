"""Tests for the `PARSERS` registry of built-in, argument-free log parsers."""

from __future__ import annotations

from chipgraph.adapters.parser import PARSERS
from chipgraph.core.plugin_api.protocols import LogParser


def test_parsers_registry_has_verilator_and_verible() -> None:
    assert set(PARSERS) == {"verilator", "verible"}
    for factory in PARSERS.values():
        parser = factory()
        assert isinstance(parser, LogParser)


def test_generic_regex_is_not_in_registry() -> None:
    # GenericRegexParser always needs a `pattern`, so it is constructed directly
    # rather than looked up in PARSERS (see adapters/parser/__init__.py docstring).
    assert "generic-regex" not in PARSERS
    assert "generic_regex" not in PARSERS

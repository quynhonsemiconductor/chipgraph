"""Log parser adapters: turn a tool's raw log text into structured `Issue`s."""

from __future__ import annotations

from collections.abc import Callable

from chipgraph.adapters.parser.generic_regex import GenericRegexParser
from chipgraph.adapters.parser.verible import VeribleParser
from chipgraph.adapters.parser.verilator import VerilatorParser
from chipgraph.core.plugin_api.protocols import LogParser

__all__ = ["PARSERS", "GenericRegexParser", "VeribleParser", "VerilatorParser"]

#: Built-in parsers that need no arguments, keyed by `LogParser.name`.
#:
#: `generic-regex` (`GenericRegexParser`) is deliberately not listed here: it always
#: needs a `pattern` (and optionally `severity_group`/`default_severity`), so callers
#: construct it directly, e.g. `GenericRegexParser(pattern=spec.args["regex"])`.
PARSERS: dict[str, Callable[[], LogParser]] = {
    "verilator": VerilatorParser,
    "verible": VeribleParser,
}

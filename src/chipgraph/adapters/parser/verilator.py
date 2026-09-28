"""Log parser for Verilator (`--lint-only` and simulation build) output.

Handles both the 5.0xx line shape with a column (``%Error-CODE: file:line:col: msg``)
and the older shape without one (``%Warning-CODE: file:line: msg``), across the
Verilator versions this project has fixtures for (5.020 and 5.052). Continuation
lines (indented source context, ``... For warning description ...`` hints) and the
final summary line (``%Error: Exiting due to N error(s)``, which names no file) are
ignored.
"""

from __future__ import annotations

import re

from chipgraph.core.contracts import Issue
from chipgraph.core.contracts.types import Severity

_LINE_RE = re.compile(
    r"^\s*%(?P<kind>Error|Warning)(-(?P<rule>[A-Za-z0-9_]+))?:\s+"
    r"(?P<file>[^\s:]+):(?P<line>\d+)(:(?P<col>\d+))?:\s*(?P<msg>.*)$"
)


class VerilatorParser:
    """Parses Verilator lint/build logs into `Issue`s."""

    name = "verilator"

    def parse(self, log: str) -> tuple[Issue, ...]:
        issues: list[Issue] = []
        for raw_line in log.splitlines():
            match = _LINE_RE.match(raw_line)
            if match is None:
                continue  # continuation line, source context, or summary line
            kind = match.group("kind")
            severity: Severity = "error" if kind == "Error" else "warning"
            issues.append(
                Issue(
                    file=match.group("file"),
                    line=int(match.group("line")),
                    rule=match.group("rule") or "",
                    severity=severity,
                    msg=match.group("msg").strip(),
                )
            )
        return tuple(issues)

"""Log parser for `verible-verilog-lint` output.

Handles both the column-range shape (``file:line:col1-col2: msg [rule]``) and the
single-column shape (``file:line:col: msg [rule]``). A lint finding is always a style
warning; a `syntax error at token ...` line (verible reports these the same way, with
no trailing `[rule]` bracket) is treated as an error with rule ``syntax``.
"""

from __future__ import annotations

import re

from chipgraph.core.contracts import Issue
from chipgraph.core.contracts.types import Severity

_LINE_RE = re.compile(
    r"^(?P<file>[^\s:]+):(?P<line>\d+):(?P<col1>\d+)(-(?P<col2>\d+))?:\s*(?P<msg>.*)$"
)
_TRAILING_RULE_RE = re.compile(r"\s*\[(?P<rule>[^\[\]]+)\]\s*$")


class VeribleParser:
    """Parses `verible-verilog-lint` logs into `Issue`s."""

    name = "verible"

    def parse(self, log: str) -> tuple[Issue, ...]:
        issues: list[Issue] = []
        for raw_line in log.splitlines():
            match = _LINE_RE.match(raw_line)
            if match is None:
                continue
            msg = match.group("msg").strip()
            severity: Severity
            rule_match = _TRAILING_RULE_RE.search(msg)
            if rule_match is not None:
                rule = rule_match.group("rule")
                msg = msg[: rule_match.start()].rstrip()
                severity = "warning"
            elif "syntax error" in msg:
                rule = "syntax"
                severity = "error"
            else:
                rule = ""
                severity = "warning"
            issues.append(
                Issue(
                    file=match.group("file"),
                    line=int(match.group("line")),
                    rule=rule,
                    severity=severity,
                    msg=msg,
                )
            )
        return tuple(issues)

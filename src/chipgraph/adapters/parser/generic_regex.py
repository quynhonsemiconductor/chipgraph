"""A configurable regex-based log parser, for tools with no dedicated parser.

The pattern must define named groups ``file``, ``line`` and ``msg``; ``rule`` and
``severity`` are optional. One `Issue` is produced per match, found with
`re.finditer` over the whole log text (so `pattern` may itself be multi-line).
"""

from __future__ import annotations

import re

from chipgraph.core.contracts import Issue
from chipgraph.core.contracts.types import Severity

_REQUIRED_GROUPS = ("file", "line", "msg")
_SEVERITY_PREFIXES: tuple[tuple[str, Severity], ...] = (
    ("err", "error"),
    ("warn", "warning"),
    ("info", "info"),
)


class GenericRegexParser:
    """A `LogParser` driven entirely by a user-supplied regex pattern."""

    name = "generic-regex"

    def __init__(
        self,
        pattern: str,
        severity_group: str | None = None,
        default_severity: Severity = "error",
    ) -> None:
        try:
            self._regex = re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            raise ValueError(f"invalid regex pattern {pattern!r}: {exc}") from exc

        group_names = set(self._regex.groupindex)
        missing = [name for name in _REQUIRED_GROUPS if name not in group_names]
        if missing:
            raise ValueError(f"pattern {pattern!r} is missing required named group(s): {missing}")
        if severity_group is not None and severity_group not in group_names:
            raise ValueError(
                f"severity_group {severity_group!r} is not a named group in {pattern!r}"
            )

        self._severity_group = severity_group
        self._default_severity = default_severity

    def parse(self, log: str) -> tuple[Issue, ...]:
        issues: list[Issue] = []
        for match in self._regex.finditer(log):
            groups = match.groupdict()
            issues.append(
                Issue(
                    file=groups.get("file") or None,
                    line=int(groups["line"]) if groups.get("line") else None,
                    rule=groups.get("rule") or "",
                    severity=self._severity(groups),
                    msg=(groups.get("msg") or "").strip(),
                )
            )
        return tuple(issues)

    def _severity(self, groups: dict[str, str | None]) -> Severity:
        group_name = self._severity_group or "severity"
        raw = groups.get(group_name)
        if not raw:
            return self._default_severity
        lowered = raw.strip().lower()
        for prefix, severity in _SEVERITY_PREFIXES:
            if lowered.startswith(prefix):
                return severity
        return self._default_severity

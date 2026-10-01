"""Read a failing log into structured facts: issues, check results, simulation failures.

Three kinds of input are understood, and may be mixed:

- a tool log (Verilator and other HDL tools, `make`, a shell): issues come from the
  existing log parsers (`VerilatorParser`, and a `GenericRegexParser` for the common
  `file:line: [error:] message` shape), plus located-less `%Error:` lines;
- the text output of `chipgraph check` (`FAIL   <check> <block> N issues` and indented
  `file:line  [rule] message` lines), or its `--json` output (`CheckResult` objects);
- a simulation run: runtime messages (`[time] %Error`, `Verilog $stop`, `Assertion
  failed`, `UVM_ERROR`, ...), a self-checking testbench's failure lines (`FAIL ...`,
  `... expected X got Y`), and the messages of its failed assertions (a testbench that
  reports with `$error` or `assert` rather than a `FAIL` line).

Nothing here decides a class; `rules` does, from these facts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from chipgraph.adapters.parser import GenericRegexParser, VerilatorParser
from chipgraph.core.contracts import CheckResult, Issue
from chipgraph.core.contracts.types import CheckStatus

MAX_EXCERPT_LINES = 60
MAX_EXCERPT_CHARS = 4000
FIRST_ERROR_LINES = 8

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_CHECK_LINE = re.compile(
    r"^(?P<status>PASS|FAIL|ERROR|SKIPPED)\s+(?P<check>\S+)\s+(?P<block>\S+)\s+\d+ issues?\s*$"
)
_CHECK_ISSUE = re.compile(
    r"^\s{2,}(?P<file>\S+?):(?P<line>\d+|-)\s{2}\[(?P<rule>[^\]]*)\]\s(?P<msg>.*)$"
)
_STATUS: dict[str, CheckStatus] = {
    "PASS": "pass",
    "FAIL": "fail",
    "ERROR": "error",
    "SKIPPED": "skipped",
}

_HDL_LINE = (
    r"^(?P<file>[^\s:'\"]+\.(?:sv|svh|v|vh|vhd|vhdl)):(?P<line>\d+):(?:\d+:)?\s*"
    r"(?:(?P<severity>[Ee]rror|[Ww]arning)\s*:?\s*)?(?P<msg>\S.*)$"
)
"""`file:line[:col]: [error|warning:] message`, as iverilog, slang, verible print it."""

_UNLOCATED = re.compile(r"^\s*%(?P<kind>Error|Fatal)(?:-(?P<rule>[A-Za-z0-9_]+))?:\s+(?P<msg>.*)$")
_LOCATED = re.compile(r"^[^\s:]+:\d+(?::\d+)?:")
_SUMMARY = re.compile(r"^Exiting due to\b")

_RUNTIME = re.compile(
    r"^\s*\[\s*\d+(?:\.\d+)?\s*(?:[a-z]?s)?\]\s*%(?:Error|Fatal|Warning)"
    r"|Verilog \$(?:stop|fatal)\b"
    r"|\bAssertion failed\b"
    r"|^\s*- S i m u l a t i o n\b"
    r"|\bUVM_(?:ERROR|FATAL)\b"
    r"|^\s*\*\* (?:Error|Fatal):.*\bTime:"
    r"|\$finish called\b"
)
"""Messages only a running simulation prints (not a compiler or a linter)."""

_SIM_FAILURE = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)?(?:FAIL(?:ED|URE)?|MISMATCH)\b"
    r"|(?i:\bexpected\b.*\b(?:got|actual|but (?:got|was|read))\b)"
    r"|\bUVM_(?:ERROR|FATAL)\b(?!\s*:\s*0\b)"
)
"""A self-checking testbench's failure line."""

_ASSERTION_FAILED = re.compile(r"\bAssertion failed in (?P<scope>[\w.$]+)\s*:\s*(?P<msg>.*\S)")
_RUNTIME_ERROR = re.compile(
    r"^\s*\[[^\]]*\]\s*%(?:Error|Fatal)(?:-[A-Za-z0-9_]+)?:\s*(?:[^\s:]+:\d+(?::\d+)?:\s*)?"
    r"(?P<msg>.*\S)"
    r"|^\s*\*\* (?:Error|Fatal):\s*(?:[^\s:()]+\(\d+\):\s*)?(?P<msg2>.*?\S)\s*(?:\bTime:.*)?$"
)
"""A running simulation's assertion or `$error`/`$fatal` message: Verilator's `Assertion
failed in <scope>: <message>`, a time-stamped `%Error:`, a `** Error: ... Time:` (not the
`Verilog $stop` line after it)."""

_NO_FACT = frozenset(
    {"assert", "assertion", "check", "checks", "error", "fail", "failed", "failure", "test"}
)
"""Words that say a check failed, not what failed."""

_WORDS = re.compile(r"\w+")


def _assertion_message(line: str) -> str | None:
    """The message of a simulation assertion line, or None for none or one that only
    says that something failed (`<tb>: 1 check(s) failed`, `'assert' failed`)."""
    if re.search(r"Verilog \$(?:stop|fatal)\b|\$finish called\b", line):
        return None
    scope = ""
    match = _ASSERTION_FAILED.search(line)
    if match is not None:
        scope, msg = match.group("scope"), match.group("msg")
    else:
        match = _RUNTIME_ERROR.search(line)
        if match is None:
            return None
        msg = match.group("msg") or match.group("msg2") or ""
    msg = msg.strip()
    scope_words = {w.lower() for w in _WORDS.findall(scope)}
    facts = [
        w
        for w in _WORDS.findall(msg)
        if len(w) > 1
        and not w.isdigit()
        and w.lower() not in _NO_FACT
        and w.lower() not in scope_words
    ]
    return msg if facts else None


_ERRORISH = re.compile(
    r"%(?:Error|Fatal|Warning)|\berror\b|\bFAIL|\bERROR\b|make: \*\*\*|not found"
    r"|No such file|timed out|Killed|Permission denied|expected\b.*\bgot\b",
    re.IGNORECASE,
)

_NOISE = re.compile(
    r"^\s*(?::\s*)?\.\.\. (?:For (?:warning|error) description see|Use \"/\* verilator"
    r"|See the manual at)"
)
"""Verilator's hint lines: links and how to waive, never what failed."""

_HDL_PARSER = GenericRegexParser(pattern=_HDL_LINE, severity_group="severity")
_VERILATOR_PARSER = VerilatorParser()


@dataclass(frozen=True)
class CheckRun:
    """One `chipgraph check` result found in the log."""

    check_id: str
    block: str | None
    status: CheckStatus
    issues: tuple[Issue, ...] = ()


@dataclass(frozen=True)
class ParsedLog:
    """What `parse_log` found in a log."""

    text: str
    """The log as read (ANSI colours removed)."""
    tool_issues: tuple[Issue, ...] = ()
    """Located compile/lint issues printed by the tools (not by a running simulation)."""
    unlocated_errors: tuple[str, ...] = ()
    """`%Error:` messages that name no file:line (summary lines left out)."""
    runtime_issues: tuple[Issue, ...] = ()
    """Located messages of a running simulation (`Verilog $stop`, assertion failures)."""
    checks: tuple[CheckRun, ...] = ()
    """`chipgraph check` results found in the log (text or JSON)."""
    sim_failures: tuple[str, ...] = ()
    """A self-checking testbench's failure lines."""
    sim_assertions: tuple[str, ...] = ()
    """The messages of a running simulation's failed assertions and `$error`/`$fatal`
    calls (`Assertion failed in <scope>: <message>` gives `<message>`), without the
    ones that only say that something failed."""
    runtime_lines: tuple[str, ...] = ()
    """Lines only a running simulation prints."""
    first_error: str = ""
    """The first error line and the lines after it."""
    excerpt: str = ""
    """The log shortened for a model: hint lines dropped, long logs cut around errors."""
    lines: tuple[str, ...] = field(default=(), repr=False)

    @property
    def sim_ran(self) -> bool:
        """True when the log shows a simulation that ran (and failed a check)."""
        return bool(self.runtime_lines or self.sim_failures)

    @property
    def failing_checks(self) -> tuple[CheckRun, ...]:
        """The check results that did not pass: `fail` or `error`."""
        return tuple(c for c in self.checks if c.status in ("fail", "error"))

    @property
    def issues(self) -> tuple[Issue, ...]:
        """Every located issue: the tools', then the failing checks', then the runtime's."""
        out = list(self.tool_issues)
        for check in self.failing_checks:
            out.extend(check.issues)
        out.extend(self.runtime_issues)
        return tuple(out)


def _json_checks(text: str) -> tuple[tuple[CheckRun, ...], str] | None:
    """`CheckResult` JSON (one object or a list): the runs and their log tails."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    # One CheckResult, a list of them (`chipgraph check --json`), or the MCP `check`
    # tool's `{"ok": ..., "results": [...]}`.
    pending: list[object] = list(data) if isinstance(data, list) else [data]
    runs: list[CheckRun] = []
    tails: list[str] = []
    while pending:
        item = pending.pop(0)
        if isinstance(item, dict) and isinstance(item.get("results"), list):
            pending[:0] = item["results"]
            continue
        try:
            result = CheckResult.model_validate(item)
        except ValueError:
            continue
        runs.append(CheckRun(result.check_id, None, result.status, result.issues))
        if result.log_tail:
            tails.append(result.log_tail)
    if not runs:
        return None
    return tuple(runs), "\n".join(tails)


def _check_runs(lines: list[str]) -> tuple[tuple[CheckRun, ...], set[int]]:
    """`chipgraph check` text results, and the indexes of the lines they used."""
    runs: list[CheckRun] = []
    used: set[int] = set()
    current: tuple[str, str | None, CheckStatus] | None = None
    issues: list[Issue] = []

    def close() -> None:
        if current is not None:
            runs.append(CheckRun(current[0], current[1], current[2], tuple(issues)))

    for index, line in enumerate(lines):
        head = _CHECK_LINE.match(line)
        if head is not None:
            close()
            block = head.group("block")
            current = (
                head.group("check"),
                None if block == "-" else block,
                _STATUS[head["status"]],
            )
            issues = []
            used.add(index)
            continue
        item = _CHECK_ISSUE.match(line) if current is not None else None
        if item is not None:
            file = item.group("file")
            line_no = item.group("line")
            issues.append(
                Issue(
                    file=None if file == "-" else file,
                    line=None if line_no == "-" else int(line_no),
                    rule=item.group("rule"),
                    msg=item.group("msg").strip(),
                )
            )
            used.add(index)
            continue
        if current is not None and line.strip():
            close()
            current = None
            issues = []
    close()
    return tuple(runs), used


def _excerpt(lines: list[str]) -> str:
    kept = [line for line in lines if not _NOISE.match(line)]
    while kept and not kept[-1].strip():
        kept.pop()
    if len(kept) > MAX_EXCERPT_LINES:
        marks = [i for i, line in enumerate(kept) if _ERRORISH.search(line)]
        keep: set[int] = set(range(10)) | set(range(len(kept) - 10, len(kept)))
        for i in marks:
            keep.update(range(max(0, i - 2), min(len(kept), i + 3)))
        out: list[str] = []
        last = -1
        for i in sorted(keep):
            if i != last + 1:
                out.append("…")
            out.append(kept[i])
            last = i
            if len(out) >= MAX_EXCERPT_LINES:
                out.append("…")
                break
        kept = out
    text = "\n".join(kept)
    if len(text) > MAX_EXCERPT_CHARS:
        text = text[: MAX_EXCERPT_CHARS - 1] + "…"
    return text


def _first_error(lines: list[str]) -> str:
    for index, line in enumerate(lines):
        if _NOISE.match(line):
            continue
        if _ERRORISH.search(line) and not line.lstrip().startswith("ok "):
            block = [ln for ln in lines[index : index + FIRST_ERROR_LINES] if not _NOISE.match(ln)]
            return "\n".join(block).rstrip()
    return ""


def _dedupe(issues: list[Issue]) -> tuple[Issue, ...]:
    seen: set[tuple[str | None, int | None, str]] = set()
    out: list[Issue] = []
    for issue in issues:
        key = (issue.file, issue.line, issue.msg)
        if key not in seen:
            seen.add(key)
            out.append(issue)
    return tuple(out)


def parse_log(text: str) -> ParsedLog:
    """Read `text` (a tool log, `chipgraph check` output, or its JSON) into a `ParsedLog`."""
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    json_runs = _json_checks(text)
    checks: tuple[CheckRun, ...] = ()
    if json_runs is not None:
        checks, text = json_runs
    lines = text.split("\n")
    text_runs, used = _check_runs(lines)
    checks = (*checks, *text_runs)

    tool: list[Issue] = []
    runtime: list[Issue] = []
    runtime_lines: list[str] = []
    sim_failures: list[str] = []
    sim_assertions: list[str] = []
    unlocated: list[str] = []
    for index, line in enumerate(lines):
        if index in used:
            continue
        if _RUNTIME.search(line):
            runtime_lines.append(line.strip())
            message = _assertion_message(line)
            if message is not None and message not in sim_assertions:
                sim_assertions.append(message)
        if _SIM_FAILURE.search(line):
            sim_failures.append(line.strip())
        bare = _UNLOCATED.match(line)
        if bare is not None:
            msg = bare.group("msg").strip()
            if not _LOCATED.match(msg) and not _SUMMARY.match(msg):
                unlocated.append(line.strip())
        for issue in (*_VERILATOR_PARSER.parse(line), *_HDL_PARSER.parse(line)):
            if _RUNTIME.search(line) or _RUNTIME.search(issue.msg):
                runtime.append(issue)
            else:
                tool.append(issue)

    return ParsedLog(
        text=text,
        tool_issues=_dedupe(tool),
        unlocated_errors=tuple(unlocated),
        runtime_issues=_dedupe(runtime),
        checks=checks,
        sim_failures=tuple(sim_failures),
        sim_assertions=tuple(sim_assertions),
        runtime_lines=tuple(runtime_lines),
        first_error=_first_error(lines),
        excerpt=_excerpt(lines),
        lines=tuple(lines),
    )


__all__ = ["CheckRun", "ParsedLog", "parse_log"]

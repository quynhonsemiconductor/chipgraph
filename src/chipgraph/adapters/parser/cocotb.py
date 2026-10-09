"""Parser for cocotb 2.x results: `results.xml` (+ the build and run logs) into `Issue`s.

cocotb writes a JUnit-style `results.xml` at `COCOTB_RESULTS_FILE`. It is the **only**
signal of what the tests did (docs/spikes/S3.md): a failing test, an exception in a
test, a test module that fails to import and a GPI that never loaded Python all leave
the simulator's exit code at 0. So:

- each `<testcase>` with a `<failure>` (or `<error>`) is one `Issue` with rule
  ``cocotb/<ExceptionType>`` (``cocotb/AssertionError`` usually means the DUT is wrong,
  any other type that the testbench is), its file and line taken from the **last
  traceback frame in the test's own file** (the testcase's `file` property; its `line`
  property, the line of the test's `def`, is only a fallback), and the message
  ``<module>.<test>: <message>``; a passing test produces no issue;
- a `<skipped>` test is an `info` issue (``cocotb/skipped``); it never turns "nothing
  ran" into a pass;
- a missing, empty or unparseable `results.xml`, one with zero testcases, or one whose
  every test was skipped is ``status="error"`` with rule ``cocotb/no_results``, never
  ``pass``; the reason is the last Python exception or ``ERROR gpi`` line of the run log;
- a failed build is ``error`` with the compiler's messages: Verilator's through
  `VerilatorParser` (rule ``verilator/<code>``, ``verilator/error`` for a plain
  ``%Error``) and Icarus's ``file:line: error: ...``/``file:line: syntax error`` lines
  (rule ``icarus/error``).

`CocotbParser.parse(text)` is the `LogParser` view (the text of a `results.xml` in, its
issues out; a bad or empty one yields the ``cocotb/no_results`` error issue), and
`CocotbParser.parse_run(...)` classifies a whole build + run into a `CocotbOutcome` with
a `CheckStatus`, which is what `EdalizeTool` uses.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from chipgraph.adapters.parser.verilator import VerilatorParser
from chipgraph.core.contracts import Issue
from chipgraph.core.contracts.types import CheckStatus, Severity

__all__ = [
    "NO_RESULTS_RULE",
    "CocotbOutcome",
    "CocotbParser",
    "CocotbTest",
    "build_issues",
    "parse_icarus_log",
]

NO_RESULTS_RULE = "cocotb/no_results"

_FRAME_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)', re.MULTILINE)
_PY_EXC_RE = re.compile(
    r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt)): (?P<msg>.*)$", re.MULTILINE
)
_GPI_ERR_RE = re.compile(r"\bERROR\s+gpi\b.*?\bin \S+\s+(?P<msg>\S.*)$", re.MULTILINE)
_ICARUS_RE = re.compile(
    r"^(?P<file>[^\s:][^:\n]*):(?P<line>\d+):\s+"
    r"(?:(?P<kind>error|warning|sorry):\s*(?P<msg>.*)|(?P<syntax>syntax error.*)"
    r"|vvp\.tgt sorry:\s*(?P<tgt>.*))$",
    re.MULTILINE,
)

Simulator = Literal["verilator", "icarus"]


@dataclass(frozen=True, slots=True)
class CocotbTest:
    """One `<testcase>` of a `results.xml`: its full name and what happened to it."""

    name: str
    outcome: Literal["pass", "fail", "skipped"]


@dataclass(frozen=True, slots=True)
class CocotbOutcome:
    """A classified build + run: the check status, its issues, and the tests seen."""

    status: CheckStatus
    issues: tuple[Issue, ...]
    tests: tuple[CocotbTest, ...] = ()

    @property
    def summary(self) -> str:
        """``tests=N pass=P fail=F skipped=S``, for a log line."""
        count = {k: sum(t.outcome == k for t in self.tests) for k in ("pass", "fail", "skipped")}
        return (
            f"tests={len(self.tests)} pass={count['pass']} fail={count['fail']} "
            f"skipped={count['skipped']}"
        )


class CocotbParser:
    """Turns cocotb's `results.xml` (and the simulator's logs) into `Issue`s."""

    name = "cocotb"

    def parse(self, log: str) -> tuple[Issue, ...]:
        """`LogParser` view: `log` is the text of a `results.xml`.

        A passing run yields no error issue (skipped tests are `info`). An empty or
        unparseable text, or one with no test that actually ran, yields one
        ``cocotb/no_results`` error issue, so a caller that only looks at issue
        severities can never read "nothing ran" as a pass.
        """
        return self.parse_results(log).issues

    def parse_results(self, xml_text: str | None, *, run_log: str = "") -> CocotbOutcome:
        """Classify the text of a `results.xml` (None: the file does not exist)."""
        if xml_text is None:
            return _no_results("cocotb wrote no results.xml: no test ran", run_log)
        if not xml_text.strip():
            return _no_results("results.xml is empty: no test ran", run_log)
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            return _no_results(f"results.xml is not valid XML ({exc})", run_log)

        tests: list[CocotbTest] = []
        issues: list[Issue] = []
        for case in root.iter("testcase"):
            name = ".".join(p for p in (case.get("classname", ""), case.get("name", "")) if p)
            props = {p.get("name"): p.get("value") for p in case.iter("property")}
            bad = case.find("failure")
            if bad is None:
                bad = case.find("error")
            if bad is not None:
                tests.append(CocotbTest(name, "fail"))
                issues.append(_failure_issue(name, bad, props))
            elif case.find("skipped") is not None:
                tests.append(CocotbTest(name, "skipped"))
                issues.append(Issue(msg=f"{name}: skipped", rule="cocotb/skipped", severity="info"))
            else:
                tests.append(CocotbTest(name, "pass"))

        if not tests:
            return _no_results("results.xml has no testcase: no test ran", run_log)
        if all(t.outcome == "skipped" for t in tests):
            why = f"every test was skipped ({len(tests)}): no test ran"
            no_run = _no_results(why, run_log).issues
            return CocotbOutcome("error", (*issues, *no_run), tuple(tests))
        status: CheckStatus = "fail" if any(t.outcome == "fail" for t in tests) else "pass"
        return CocotbOutcome(status, tuple(issues), tuple(tests))

    def parse_run(
        self,
        results: Path | None,
        *,
        simulator: Simulator,
        build_rc: int,
        build_log: str = "",
        build_timed_out: bool = False,
        run_rc: int | None = None,
        run_log: str = "",
        run_timed_out: bool = False,
        relativize: Callable[[str], str] | None = None,
    ) -> CocotbOutcome:
        """Classify one build + run of a cocotb testbench.

        `build_rc` is the build's exit code; `run_rc` is the run's (None when the run did
        not start). `results` is the path cocotb was told to write (it may not exist, and
        the caller must have removed any stale one before the run). `relativize` maps a
        file path in an issue to the form the caller wants (e.g. repo-relative).

        The outcome is `pass` only with a `results.xml` that has at least one test that
        ran, none that failed, a run that exited 0 and no timeout. Failing tests are
        `fail`; everything else (build failure, timeout, no or bad results) is `error`.
        """
        rel = relativize or (lambda path: path)
        if build_rc != 0 or build_timed_out:
            issues = list(build_issues(build_log, simulator))
            if build_timed_out:
                issues.append(Issue(msg="the build timed out", rule="sim/timeout"))
            return CocotbOutcome("error", _relativize(issues, rel))
        if run_rc is None:
            msg = "the simulation did not start"
            return CocotbOutcome("error", (Issue(msg=msg, rule=NO_RESULTS_RULE),))

        text = _read_text(results) if results is not None else None
        outcome = self.parse_results(text, run_log=run_log)
        issues = list(outcome.issues)
        status = outcome.status
        if run_timed_out:
            issues.append(Issue(msg="the simulation timed out", rule="sim/timeout"))
            status = "fail" if status == "fail" else "error"
        elif status == "pass" and run_rc != 0:
            msg = f"the simulator exited with {run_rc} after the tests"
            issues.append(Issue(msg=msg, rule="sim/exit"))
            status = "error"
        return CocotbOutcome(status, _relativize(issues, rel), outcome.tests)


def build_issues(log: str, simulator: Simulator) -> tuple[Issue, ...]:
    """Compiler messages from a failed build; one generic issue if none is recognised."""
    found: list[Issue] = []
    for issue in VerilatorParser().parse(log):
        code = issue.rule or ("error" if issue.severity == "error" else "warning")
        found.append(issue.model_copy(update={"rule": f"verilator/{code}"}))
    found.extend(parse_icarus_log(log))
    if not any(i.severity == "error" for i in found):
        last = _last_meaningful_line(log)
        detail = f": {last}" if last else ""
        found.append(Issue(msg=f"the {simulator} build failed{detail}", rule="sim/build"))
    return tuple(found)


def parse_icarus_log(log: str) -> tuple[Issue, ...]:
    """Icarus Verilog's ``file:line: error: msg`` / ``file:line: syntax error`` lines.

    Errors get rule ``icarus/error``; ``warning:`` lines ``icarus/warning`` and
    ``sorry:`` lines (unsupported constructs Icarus ignores) ``icarus/sorry``, both
    warnings.
    """
    issues: list[Issue] = []
    for m in _ICARUS_RE.finditer(log):
        kind = m["kind"] or ("error" if m["syntax"] else "sorry")
        msg = (m["msg"] or m["syntax"] or m["tgt"] or "").strip()
        severity: Severity = "error" if kind == "error" else "warning"
        issues.append(
            Issue(
                file=m["file"],
                line=int(m["line"]),
                rule=f"icarus/{kind}",
                severity=severity,
                msg=msg,
            )
        )
    return tuple(issues)


def _failure_issue(name: str, bad: ET.Element, props: dict[str | None, str | None]) -> Issue:
    kind = bad.get("type") or bad.tag
    message = (bad.get("message") or "").strip() or kind
    test_file = props.get("file")
    file, line = _failure_location(bad.text or "", test_file)
    if line is None and props.get("line"):
        line = _int_or_none(props.get("line"))
    return Issue(file=file, line=line, rule=f"cocotb/{kind}", msg=f"{name}: {message}")


def _failure_location(traceback: str, test_file: str | None) -> tuple[str | None, int | None]:
    """The last traceback frame in `test_file`; else the test's own file, no line yet."""
    frames = [(m["file"], int(m["line"])) for m in _FRAME_RE.finditer(traceback)]
    if test_file:
        own = [f for f in frames if _same_file(f[0], test_file)]
        if own:
            return own[-1]
        return test_file, None
    return frames[-1] if frames else (None, None)


def _same_file(a: str, b: str) -> bool:
    return a == b or os.path.realpath(a) == os.path.realpath(b)


def _no_results(why: str, run_log: str) -> CocotbOutcome:
    reason = _run_log_reason(run_log)
    msg = f"{why} ({reason})" if reason else why
    return CocotbOutcome("error", (Issue(msg=msg, rule=NO_RESULTS_RULE),))


def _run_log_reason(run_log: str) -> str:
    """The last Python exception, else the last ``ERROR gpi`` message, of a run log."""
    exc = list(_PY_EXC_RE.finditer(run_log))
    if exc:
        return f"{exc[-1]['type']}: {exc[-1]['msg'].strip()}"
    gpi = list(_GPI_ERR_RE.finditer(run_log))
    if gpi:
        return f"gpi: {gpi[-1]['msg'].strip()}"
    return ""


def _last_meaningful_line(log: str) -> str:
    for line in reversed(log.splitlines()):
        text = line.strip()
        if text and not text.startswith("make") and not text.startswith("%Error: Exiting"):
            return text[:200]
    return ""


def _relativize(issues: list[Issue], rel: Callable[[str], str]) -> tuple[Issue, ...]:
    return tuple(
        i.model_copy(update={"file": rel(i.file)}) if i.file is not None else i for i in issues
    )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError:
        return ""


def _int_or_none(raw: str | None) -> int | None:
    try:
        value = int(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and value >= 1 else None

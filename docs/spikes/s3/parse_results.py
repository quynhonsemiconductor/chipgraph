"""S3 spike: draft of the M2-05 parser, cocotb `results.xml` (+ logs) -> a CheckResult shape.

    python parse_results.py WORK_ROOT [--build-rc N] [--run-rc N]

Reads `WORK_ROOT/results.xml` (JUnit-style, written by cocotb 2.x at COCOTB_RESULTS_FILE)
and `build.log`/`run.log`, and prints a dict shaped like chipgraph's `CheckResult`:
`status` (pass/fail/error), `tests` (DESIGN.md 8: `{name, pass, log_tail}`) and `issues`
(`{file, line, rule, severity, msg}`). Rules learned from real runs (docs/spikes/S3.md):

- A failing test does NOT change the simulator's exit code (Verilator and Icarus both
  exit 0): results.xml is the only signal.
- A missing results.xml after a run that exited 0 means cocotb never ran the tests
  (GPI_USERS unset, module import error, libpython missing or of another version): status
  `error`, never `pass`. The reason is the last Python exception or `ERROR gpi` line.
- An `assert` and any other exception are both `<failure>` elements, told apart by the
  `type` attribute (AssertionError vs AttributeError ...); `<error>` is not used by
  cocotb 2.1 for test exceptions but is handled the same way.
- The failing line is the last traceback frame in the test's own file (the `file`
  testcase property); the `line` property is the line of the test function's `def`.
- A build failure is a non-zero build exit code; its log is Verilator's `%Error: f:l:c:`
  or Icarus's `f:l: error`/`syntax error` format.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

_LOG_TAIL = 4000
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)', re.MULTILINE)
_VERILATOR = re.compile(
    r"^%(?P<sev>Error|Warning)(?:-(?P<rule>[A-Z0-9_]+))?: (?P<file>[^:\s]+):(?P<line>\d+):"
    r"(?:\d+:)? ?(?P<msg>.*)$",
    re.MULTILINE,
)
_ICARUS = re.compile(
    r"^(?P<file>[^:\s]+):(?P<line>\d+): (?P<msg>(?:error: |syntax error).*)$", re.M
)
_GPI_ERR = re.compile(r"ERROR\s+gpi\s+\S+\s+in \S+\s+(?P<msg>.*)$", re.MULTILINE)
_PY_EXC = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception)): (?P<msg>.*)$", re.MULTILINE)


def issue(
    msg: str, *, file: str | None = None, line: int | None = None, rule: str = "", sev="error"
) -> dict[str, Any]:
    return {"file": file, "line": line, "rule": rule, "severity": sev, "msg": msg}


def parse_build_log(log: str) -> list[dict[str, Any]]:
    """Compiler errors from a failed build (in M2-05: chipgraph's VerilatorParser + one for
    Icarus)."""
    found = [
        issue(
            m["msg"].strip(),
            file=m["file"],
            line=int(m["line"]),
            rule=f"verilator/{m['rule'] or m['sev'].lower()}",
            sev="error" if m["sev"] == "Error" else "warning",
        )
        for m in _VERILATOR.finditer(log)
    ]
    found += [
        issue(m["msg"].strip(), file=m["file"], line=int(m["line"]), rule="icarus/error")
        for m in _ICARUS.finditer(log)
    ]
    return found or [issue("build failed (no compiler message recognised)", rule="sim/build")]


def _failure_location(text: str, test_file: str | None) -> tuple[str | None, int | None]:
    frames = [(m["file"], int(m["line"])) for m in _FRAME.finditer(text)]
    own = [f for f in frames if test_file and f[0] == test_file]
    if own:
        return own[-1]
    return frames[-1] if frames else (test_file, None)


def parse_results_xml(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(tests, issues) from a cocotb results.xml."""
    root = ET.parse(path).getroot()
    tests: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for case in root.iter("testcase"):
        name = f"{case.get('classname', '')}.{case.get('name', '')}".strip(".")
        props = {p.get("name"): p.get("value") for p in case.iter("property")}
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        skipped = case.find("skipped") is not None
        tests.append({"name": name, "pass": bad is None and not skipped, "skipped": skipped})
        if skipped:
            issues.append(issue(f"{name}: skipped", rule="cocotb/skipped", sev="info"))
            continue
        if bad is None:
            continue
        kind = bad.get("type") or bad.tag
        message = (bad.get("message") or "").strip() or kind
        file, line = _failure_location(bad.text or "", props.get("file"))
        if line is None and props.get("line"):
            line = int(props["line"])
        tests[-1]["log_tail"] = (bad.text or "")[-_LOG_TAIL:]
        issues.append(issue(f"{name}: {message}", file=file, line=line, rule=f"cocotb/{kind}"))
    return tests, issues


def parse(
    results: Path, *, build_rc: int, run_rc: int | None, build_log: str, run_log: str
) -> dict[str, Any]:
    """Classify one build+run; `run_rc` is None when the run step did not start."""
    if build_rc != 0:
        return _result("error", [], parse_build_log(build_log), build_log)
    if not results.is_file():
        exc = list(_PY_EXC.finditer(run_log))
        gpi = list(_GPI_ERR.finditer(run_log))
        why = ""
        if exc:
            why = f" ({exc[-1]['type']}: {exc[-1]['msg']})"
        elif gpi:
            why = f" (gpi: {gpi[-1]['msg'].strip()})"
        msg = f"cocotb wrote no results.xml: no test ran{why}"
        return _result("error", [], [issue(msg, rule="cocotb/no_results")], run_log)
    try:
        tests, issues = parse_results_xml(results)
    except ET.ParseError as exc:
        bad_xml = issue(f"results.xml is not valid XML: {exc}", rule="cocotb/bad_results")
        return _result("error", [], [bad_xml], run_log)
    if not tests:
        no_tests = issue("results.xml has no testcase", rule="cocotb/no_tests")
        return _result("error", [], [no_tests], run_log)
    if any(i["severity"] == "error" for i in issues):
        return _result("fail", tests, issues, run_log)
    if run_rc:
        sim = issue(f"simulator exited with {run_rc} after the tests", rule="sim/exit")
        return _result("error", tests, [*issues, sim], run_log)
    return _result("pass", tests, issues, run_log)


def _result(status: str, tests: list, issues: list, log: str) -> dict[str, Any]:
    return {"status": status, "tests": tests, "issues": issues, "log_tail": log[-_LOG_TAIL:]}


def _read(path: Path) -> str:
    return path.read_text(errors="replace") if path.is_file() else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work_root", type=Path)
    ap.add_argument("--build-rc", type=int, default=0)
    ap.add_argument("--run-rc", type=int, default=0)
    args = ap.parse_args()
    work = args.work_root
    result = parse(
        work / "results.xml",
        build_rc=args.build_rc,
        run_rc=args.run_rc,
        build_log=_read(work / "build.log"),
        run_log=_read(work / "run.log"),
    )
    result.pop("log_tail")
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

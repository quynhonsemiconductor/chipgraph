"""S3 spike: run every flow x case, classify each with parse_results, print a summary.

    python run_all.py --out DIR [--only SUBSTRING]

Run with the Python of a throwaway venv that has edalize and cocotb installed (see
`setup_venv.sh`); verilator, iverilog/vvp, make and a C++ compiler must be on PATH.
Writes `DIR/logs/<case>.log`, `DIR/work/<case>/` (the work roots), `DIR/summary.md` and
`DIR/summary.json`. Exits 1 if any case's outcome differs from what is expected.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from parse_results import parse
from s3_common import HERE, verilator_cxx_std_args, verilator_fst_args


@dataclass(frozen=True)
class Case:
    name: str
    argv: list[str]
    expect_status: str
    expect_rule: str | None = None  # rule of the first issue, if any
    env: dict[str, str] = field(default_factory=dict)
    expect_files: tuple[str, ...] = ()  # files that must exist in the work root


def cases() -> list[Case]:
    out: list[Case] = []
    breaks = {
        "pass": ("pass", None),
        "assert": ("fail", "cocotb/AssertionError"),
        "crash": ("fail", "cocotb/AttributeError"),
        "import": ("error", "cocotb/no_results"),
    }
    for tool in ("verilator", "icarus"):
        build_rule = f"{tool}/error"
        # a: cocotb's own runner
        for brk in ("pass", "assert"):
            status, rule = breaks[brk]
            out.append(
                Case(
                    f"a-runner-{tool}-{brk}",
                    ["run_runner.py", "--sim", tool],
                    status,
                    rule,
                    {"S3_BREAK": brk},
                )
            )
        out.append(
            Case(
                f"a-runner-{tool}-build",
                ["run_runner.py", "--sim", tool],
                "error",
                build_rule,
                {"S3_BREAK": "build"},
            )
        )
        # b/c: Edalize configure in-process, build/run through a Runner (the M2-05 shape)
        for brk, (status, rule) in breaks.items():
            out.append(
                Case(
                    f"bc-edalize-{tool}-{brk}",
                    ["run_edalize.py", "--tool", tool],
                    status,
                    rule,
                    {"S3_BREAK": brk},
                )
            )
        out.append(
            Case(
                f"bc-edalize-{tool}-build",
                ["run_edalize.py", "--tool", tool],
                "error",
                build_rule,
                {"S3_BREAK": "build"},
            )
        )
        out.append(
            Case(
                f"bc-edalize-{tool}-waves",
                ["run_edalize.py", "--tool", tool, "--waves"],
                "pass",
                None,
                {"S3_BREAK": "pass"},
                ("dump.fst",),
            )
        )
        # Edalize 0.6.8 running cocotb 2.1 by itself: a green exit, but no test runs.
        out.append(
            Case(
                f"bc-edalize-{tool}-own-run",
                ["run_edalize.py", "--tool", tool, "--driver", "edalize"],
                "error",
                "cocotb/no_results",
                {"S3_BREAK": "pass"},
            )
        )
    return out


def tool_version(cmd: list[str]) -> str:
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError:
        return "not found"
    text = (cp.stdout or cp.stderr).strip().splitlines()
    return text[0] if text else f"exit {cp.returncode}"


def environment() -> dict[str, str]:
    import cocotb
    import edalize.version
    import find_libpython

    return {
        "os": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": f"{platform.python_version()} {sys.executable}",
        "python_build": "uv-managed"
        if "uv/python" in os.path.realpath(sys.executable) or "/uv/" in sys.base_prefix
        else sys.base_prefix,
        "libpython": str(find_libpython.find_libpython()),
        "cocotb": cocotb.__version__,
        "edalize": edalize.version.__version__,
        "verilator": f"{tool_version(['verilator', '--version'])} ({shutil.which('verilator')})",
        "iverilog": f"{tool_version(['iverilog', '-V'])} ({shutil.which('vvp')})",
        "make": tool_version(["make", "--version"]),
        "c++": tool_version([os.environ.get("CXX", "c++"), "--version"]),
        "cxx_std_fix": " ".join(verilator_cxx_std_args()) or "none needed",
        "fst_lz4_fix": " ".join(verilator_fst_args()) or "none needed",
        "cocotb-config on PATH": shutil.which("cocotb-config") or "not found",
    }


def run_case(case: Case, out: Path) -> dict[str, Any]:
    work = out / "work" / case.name
    log_path = out / "logs" / f"{case.name}.log"
    argv = [sys.executable, str(HERE / case.argv[0]), *case.argv[1:], "--out", str(work)]
    env = {**os.environ, **case.env}
    t0 = time.monotonic()
    cp = subprocess.run(argv, cwd=HERE, env=env, capture_output=True, text=True, check=False)
    wall = time.monotonic() - t0
    log = cp.stdout + cp.stderr
    log_path.write_text(log)

    line = next((ln for ln in log.splitlines() if ln.startswith("S3RESULT ")), None)
    meta = json.loads(line.removeprefix("S3RESULT ")) if line else {}
    rc, step = meta.get("rc", cp.returncode), meta.get("failed_step", "script")
    build_rc = rc if step in ("build", "script") else 0
    run_rc = rc if step == "run" else (None if step else 0)
    build_log = _read(work / "build.log") or log
    run_log = _read(work / "run.log") or log
    parsed = parse(
        work / "results.xml",
        build_rc=build_rc,
        run_rc=run_rc,
        build_log=build_log,
        run_log=run_log,
    )
    parsed.pop("log_tail")
    (work / "parsed.json").write_text(json.dumps(parsed, indent=2) + "\n")

    first_rule = parsed["issues"][0]["rule"] if parsed["issues"] else None
    missing = [f for f in case.expect_files if not (work / f).is_file()]
    ok = parsed["status"] == case.expect_status and first_rule == case.expect_rule and not missing
    first = parsed["issues"][0] if parsed["issues"] else None
    where = ""
    if first and first["file"]:
        where = f"{Path(first['file']).name}:{first['line']} "
    return {
        "case": case.name,
        "build": "fail" if build_rc else "ok",
        "run_rc": "-" if run_rc is None else str(run_rc),
        "results_xml": "yes" if (work / "results.xml").is_file() else "no",
        "status": parsed["status"],
        "tests": f"{sum(t['pass'] for t in parsed['tests'])}/{len(parsed['tests'])}",
        "first_issue": f"{first_rule} {where}".strip() if first_rule else "",
        "files": ",".join(f for f in case.expect_files if f not in missing),
        "wall_s": f"{wall:.1f}",
        "times": meta.get("times", {}),
        "ok": "OK" if ok else f"UNEXPECTED (want {case.expect_status} {case.expect_rule})",
    }


def _read(path: Path) -> str:
    return path.read_text(errors="replace") if path.is_file() else ""


COLUMNS = ["case", "build", "run_rc", "results_xml", "status", "tests", "first_issue", "files"]
COLUMNS += ["wall_s", "ok"]


def table(rows: list[dict[str, Any]]) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    lines += ["| " + " | ".join(str(r[c]) for c in COLUMNS) + " |" for r in rows]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", default="", help="run only cases whose name contains this")
    args = ap.parse_args()
    out: Path = args.out.resolve()
    # the venv's bin first: Edalize's Makefile calls `cocotb-config` through the shell
    venv_bin = str(Path(sys.executable).parent)
    os.environ["PATH"] = os.pathsep.join([venv_bin, os.environ.get("PATH", "")])
    (out / "logs").mkdir(parents=True, exist_ok=True)
    (out / "work").mkdir(parents=True, exist_ok=True)

    env = environment()
    rows = []
    for case in cases():
        if args.only in case.name:
            rows.append(run_case(case, out))
            print(f"{rows[-1]['case']:32} {rows[-1]['status']:6} {rows[-1]['ok']}", flush=True)

    header = "\n".join(f"- {k}: {v}" for k, v in env.items())
    summary = f"# S3 run\n\n{header}\n\n{table(rows)}\n"
    (out / "summary.md").write_text(summary)
    (out / "summary.json").write_text(json.dumps({"env": env, "rows": rows}, indent=2) + "\n")
    print("\n" + summary)
    bad = [r["case"] for r in rows if r["ok"] != "OK"]
    if bad or not rows:
        print(f"S3: {len(bad)} unexpected outcome(s): {', '.join(bad) or 'no case ran'}")
        return 1
    print(f"S3: all {len(rows)} cases as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())

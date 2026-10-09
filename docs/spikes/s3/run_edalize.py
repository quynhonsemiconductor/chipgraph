"""S3 spike: flows b and c, Edalize's `sim` flow with cocotb (Verilator or Icarus).

    python run_edalize.py --tool verilator|icarus --out DIR [--driver runner|edalize] [--waves]

`--driver runner` (default) is the shape proposed for M2-05: Edalize is used in-process
only to *configure* (write the Makefile and the tool's config files into the work root;
for these two tools that starts no subprocess). The build (`make`) and the run (the
command Edalize's tool node reports) then go through a `Runner`-like async function, with
the cocotb environment computed in-process from `cocotb_tools.config`.

`--driver edalize` lets Edalize do everything (`build()` and `run()`, which call
`subprocess.run` themselves) with only PYTHONPATH and COCOTB_RESULTS_FILE added, to show
what Edalize 0.6.8's own cocotb support does with cocotb 2.1.

Exit code: 0 if the build and the run both exit 0, else the failing step's code; a JSON
line `S3RESULT {...}` on stdout says which step failed and how long each took.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from s3_common import HERE, TEST_MODULE, TOPLEVEL, edam, sources_for_run


async def run_cmd(
    cmd: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None
) -> tuple[int, str]:
    """Same contract as chipgraph's LocalRunner: argv only, env merged on os.environ."""
    full_env = {**os.environ, **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode if proc.returncode is not None else -9, out.decode(errors="replace")


def cocotb_env(tool: str, work_root: Path) -> dict[str, str]:
    """The environment cocotb 2.1 needs at simulation time, computed without a subprocess.

    This is what `cocotb_tools.runner.Runner._set_env_common`/`_set_env_test` set. The
    key one is GPI_USERS: cocotb 2.x's GPI loads libpython and the Python entry point from
    it and exits with "No GPI_USERS specified" otherwise. LIBPYTHON_LOC, which Edalize
    0.6.8 sets, is only read by cocotb's own Makefiles, not by the GPI.
    """
    import cocotb_tools.config
    import find_libpython

    libpython = find_libpython.find_libpython()
    if libpython is None:
        raise SystemExit("find_libpython found no libpython for " + sys.executable)
    env = {
        "COCOTB_TEST_MODULES": TEST_MODULE,
        "COCOTB_TOPLEVEL": TOPLEVEL,
        "TOPLEVEL_LANG": "verilog",
        "GPI_USERS": f"{libpython};{cocotb_tools.config.pygpi_entry_point()}",
        "PYGPI_PYTHON_BIN": sys.executable,
        "PYTHONPATH": os.pathsep.join([str(HERE), *sys.path[1:]]),
        "COCOTB_RESULTS_FILE": str(work_root / "results.xml"),
        # Edalize's Makefile and .vc call `cocotb-config` in backticks: it must be the
        # venv's, so the venv's bin goes first on PATH.
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
    }
    if tool == "verilator":
        env["COCOTB_TRUST_INERTIAL_WRITES"] = "1"  # what cocotb's own Verilator runner sets
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", choices=["verilator", "icarus"], required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--driver", choices=["runner", "edalize"], default="runner")
    ap.add_argument("--waves", action="store_true")
    args = ap.parse_args()

    from edalize.flows.sim import Sim

    work_root: Path = args.out.resolve()
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True)
    sources = sources_for_run(work_root / "src")
    the_edam = edam(args.tool, sources, waves=args.waves)
    (work_root / "edam.json").write_text(json.dumps(the_edam, indent=2) + "\n")

    times: dict[str, float] = {}
    t0 = time.monotonic()
    flow = Sim(the_edam, work_root)
    flow.configure()
    times["configure"] = time.monotonic() - t0

    if args.driver == "edalize":
        os.environ["PYTHONPATH"] = str(HERE)
        os.environ["COCOTB_RESULTS_FILE"] = str(work_root / "results.xml")
        step = "build"
        try:
            t0 = time.monotonic()
            flow.build()
            times["build"] = time.monotonic() - t0
            step = "run"
            t0 = time.monotonic()
            flow.run()
            times["run"] = time.monotonic() - t0
            rc, step = 0, ""
        except RuntimeError as exc:
            print(f"edalize: {exc}", flush=True)
            times[step] = time.monotonic() - t0
            rc = 1
        _report(args, rc, step, times, work_root)
        return rc

    # driver == "runner": the build and run commands as data, executed by our runner.
    build_cmd, build_args = flow.build_runner.get_build_command()
    run_cmd_, run_args, run_cwd = flow.flow.get_node(args.tool).inst.run()
    env = cocotb_env(args.tool, work_root)
    (work_root / "commands.json").write_text(
        json.dumps(
            {
                "build": [build_cmd, *build_args],
                "run": [run_cmd_, *run_args],
                "run_cwd": str(run_cwd),
                "env": env,
            },
            indent=2,
        )
        + "\n"
    )

    t0 = time.monotonic()
    rc, log = asyncio.run(run_cmd([build_cmd, *build_args], cwd=work_root, env=env))
    times["build"] = time.monotonic() - t0
    (work_root / "build.log").write_text(log)
    print(log, end="", flush=True)
    if rc != 0:
        _report(args, rc, "build", times, work_root)
        return rc

    t0 = time.monotonic()
    rc, log = asyncio.run(run_cmd([run_cmd_, *run_args], cwd=Path(run_cwd), env=env))
    times["run"] = time.monotonic() - t0
    (work_root / "run.log").write_text(log)
    print(log, end="", flush=True)
    _report(args, rc, "run" if rc else "", times, work_root)
    return rc


def _report(
    args: argparse.Namespace, rc: int, failed_step: str, times: dict[str, float], work: Path
) -> None:
    files = sorted(
        str(p.relative_to(work)) for p in work.rglob("*") if p.is_file() and p.parent == work
    )
    print(
        "S3RESULT "
        + json.dumps(
            {
                "flow": f"edalize-{args.tool}",
                "driver": args.driver,
                "rc": rc,
                "failed_step": failed_step,
                "times": {k: round(v, 2) for k, v in times.items()},
                "work_root_files": files,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    sys.exit(main())

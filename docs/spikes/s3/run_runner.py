"""S3 spike: flow a, cocotb's own runner (`cocotb_tools.runner`, cocotb 2.x).

    python run_runner.py --sim verilator|icarus --out DIR [--waves]

`build()` and `test()` run the simulator with `subprocess` themselves. Outside pytest,
`test()` returns the results file and does not raise for failed tests; it exits with the
simulator's code only if the simulator itself failed. The runner puts its own
`sys.path` (this directory first) into PYTHONPATH, so `gpio_tb` is found. Prints a
`S3RESULT {...}` JSON line.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from s3_common import TEST_MODULE, TOPLEVEL, sources_for_run, verilator_cxx_std_args


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", choices=["verilator", "icarus"], required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--waves", action="store_true")
    args = ap.parse_args()

    from cocotb_tools.runner import get_runner

    out: Path = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    sources = sources_for_run(out / "src")

    runner = get_runner(args.sim)
    build_args = verilator_cxx_std_args() if args.sim == "verilator" else ["-g2012"]
    times: dict[str, float] = {}
    step = "build"
    t0 = time.monotonic()
    try:
        runner.build(
            sources=sources,
            hdl_toplevel=TOPLEVEL,
            build_dir=out / "sim_build",
            build_args=build_args,
            timescale=("1ns", "1ps"),
            waves=args.waves,
            always=True,
        )
        times["build"] = time.monotonic() - t0
        step = "run"
        t0 = time.monotonic()
        results = runner.test(
            test_module=TEST_MODULE,
            hdl_toplevel=TOPLEVEL,
            test_dir=out,
            build_dir=out / "sim_build",
            waves=args.waves,
        )
        times["run"] = time.monotonic() - t0
        rc, step = 0, ""
        print(f"results: {results}")
    except SystemExit as exc:  # the runner exits on a simulator failure
        times[step] = time.monotonic() - t0
        rc = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # a build failure raises CalledProcessError
        times[step] = time.monotonic() - t0
        print(f"{type(exc).__name__}: {exc}")
        rc = getattr(exc, "returncode", 1) or 1
    print(
        "S3RESULT "
        + json.dumps(
            {
                "flow": f"cocotb-runner-{args.sim}",
                "driver": "cocotb_tools.runner",
                "rc": rc,
                "failed_step": step,
                "times": {k: round(v, 2) for k, v in times.items()},
            }
        ),
        flush=True,
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())

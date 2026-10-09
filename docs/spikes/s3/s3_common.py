"""S3 spike: shared helpers for the three flows (paths, filelist, EDAM).

The design comes from examples/tinysoc: its `filelists/gpio.f` lists the sources relative
to examples/tinysoc, the way chipgraph's filelist adapter reads them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TINYSOC = REPO / "examples" / "tinysoc"
FILELIST = TINYSOC / "filelists" / "gpio.f"
TOPLEVEL = "tiny_gpio"
TEST_MODULE = "gpio_tb"


def read_filelist(filelist: Path = FILELIST, base: Path = TINYSOC) -> list[Path]:
    """Source files of a plain `.f` filelist (one path per line, `//` and `#` comments)."""
    sources: list[Path] = []
    for raw in filelist.read_text().splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        sources.append((base / line).resolve())
    return sources


def broken_source(out: Path) -> Path:
    """An extra source with a syntax error, for the build-failure case (S3_BREAK=build)."""
    path = out / "broken.sv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("module broken (input logic a);\n  assign b = ;\nendmodule\n")
    return path


def sources_for_run(out: Path) -> list[Path]:
    """The filelist's sources, plus a broken file when S3_BREAK=build."""
    sources = read_filelist()
    if os.environ.get("S3_BREAK") == "build":
        sources.append(broken_source(out))
    return sources


def verilator_cxx_std_args() -> list[str]:
    """`-CFLAGS -std=gnu++17` when the C++ compiler defaults to a standard below C++14.

    Verilator 5.052 needs C++14+. Its `verilated.mk` adds a `-std=` flag only if the
    compiler Verilator was *built* with needed one: the Homebrew bottle was built with
    Apple clang 21 and has an empty `CFG_CXXFLAGS_STD`, but Command Line Tools 16.x on the
    user's machine default to C++98 and the model fails to compile ("Verilator requires a
    C++14 or newer compiler"). Debian's g++ 12 defaults to gnu++17.
    """
    cxx = os.environ.get("CXX", "c++")
    try:
        out = subprocess.run(
            [cxx, "-dM", "-E", "-x", "c++", os.devnull],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    for line in out.splitlines():
        if line.startswith("#define __cplusplus ") and int(line.split()[2].rstrip("L")) < 201402:
            return ["-CFLAGS", "-std=gnu++17"]
    return []


def verilator_fst_args() -> list[str]:
    """Where Verilator 5.052's FST writer finds lz4 (and zlib) when the model is compiled.

    5.052's `verilated_fst_c.cpp` includes `<lz4.h>` and links `-llz4 -lz`. Homebrew
    installs lz4 under its prefix, which Apple clang does not search, so `--trace-fst`
    fails with "'lz4.h' file not found" unless the prefix is passed. On Linux the headers
    come from the distribution (`liblz4-dev`, `zlib1g-dev`) on the default paths.
    """
    if sys.platform != "darwin":
        return []
    for prefix in (os.environ.get("HOMEBREW_PREFIX", ""), "/opt/homebrew", "/usr/local"):
        if prefix and (Path(prefix) / "include" / "lz4.h").is_file():
            return ["-CFLAGS", f"-I{prefix}/include", "-LDFLAGS", f"-L{prefix}/lib"]
    return []


def edam(
    tool: str,
    sources: list[Path],
    *,
    parameters: dict[str, Any] | None = None,
    tool_options: dict[str, Any] | None = None,
    waves: bool = False,
) -> dict[str, Any]:
    """An EDAM dict for Edalize's `sim` flow with cocotb, built the way chipgraph would.

    `files` come from the filelist, `toplevel` from the block, `parameters` as
    `vlogparam`, and `flow_options.cocotb_module` turns on cocotb (Edalize 0.6.x has no
    `mode: cocotb` any more; cocotb is a `sim` flow option).
    """
    params = {
        name: {"datatype": _datatype(value), "paramtype": "vlogparam", "default": value}
        for name, value in (parameters or {}).items()
    }
    opts: dict[str, Any] = {"tool": tool, "cocotb_module": TEST_MODULE}
    toplevel = TOPLEVEL
    if tool == "verilator":
        # --trace-fst makes verilator set VM_TRACE/VM_TRACE_FST in the generated makefile;
        # cocotb's verilator.cpp then writes dump.fst when the model gets `--trace`.
        opts["verilator_options"] = verilator_cxx_std_args()
        if waves:
            opts["verilator_options"] += ["--trace-fst", *verilator_fst_args()]
        opts["run_options"] = ["--trace"] if waves else []
        opts["make_options"] = ["-j4"]
    if tool == "icarus":
        # tinysoc is SystemVerilog (logic, always_ff, unique case): Icarus needs -g2012,
        # and a timescale so cocotb's 10 ns clock is representable (Icarus defaults to 1s).
        opts["iverilog_options"] = ["-g2012"]
        opts["timescale"] = "1ns/1ps"
        if waves:
            # Edalize already runs `vvp ... -fst`; a second top with $dumpvars does the rest.
            sources = [*sources, HERE / "icarus_dump.v"]
            toplevel = f"{TOPLEVEL} s3_dump"
    opts.update(tool_options or {})
    files = [{"name": str(src), "file_type": "systemVerilogSource"} for src in sources]
    return {
        "name": f"s3_{tool}",
        "toplevel": toplevel,
        "files": files,
        "parameters": params,
        "flow_options": opts,
    }


def _datatype(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    return "str"

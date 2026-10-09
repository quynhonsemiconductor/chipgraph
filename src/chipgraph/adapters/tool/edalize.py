"""`EdalizeTool`: cocotb simulation on Verilator or Icarus, configured by Edalize.

Shape (docs/spikes/S3.md): Edalize 0.6's `sim` flow is used **in process, to configure
only** (it writes the Makefile and the tool files into a work root and starts no
subprocess for these two simulators). The build (`make`) and the run (the model, or
``make run`` for Icarus) then go through `ctx.runner`, with a cocotb 2.x environment
chipgraph computes itself (`GPI_USERS`, `COCOTB_TEST_MODULES`, `COCOTB_TOPLEVEL`,
`COCOTB_RESULTS_FILE`, the virtualenv's `bin` first on `PATH`, ...). Edalize's own
`build()`/`run()` are never called: they call `subprocess` directly and, with cocotb 2.1,
exit 0 without running a test. The result comes from cocotb's `results.xml` only
(`CocotbParser`): failing tests leave every exit code at 0, and **no `results.xml` is an
`error`, never a `pass`**.

Edalize and cocotb are the optional `sim` extra (``uv sync --extra sim``) and are imported
lazily: without them the adapter still loads, and a run returns an `error` saying how to
install them. A missing simulator, `make` or C++ compiler is an `error` that names it.

Profile example (`.chipgraph.yml`)::

    adapters:
      sim:
        use: edalize
        simulator: verilator          # or icarus; `tool:` is accepted too
        top: "tiny_{block}"
        filelist: "filelists/{block}.f"
        test_module: "dv/{block}_tb.py"   # a file, or dotted names found in `test_dir`
        waves: false
        timeout_s: 600

`spec.args` (every string may use `ctx.params` placeholders such as ``{block}``):

- ``top`` (required): the HDL toplevel, also `COCOTB_TOPLEVEL`.
- ``filelist`` and/or ``files``: a `.f` filelist (read by
  `chipgraph.adapters.tool.filelist.read_filelist`: sources, ``+incdir+``,
  ``+define+``) and/or a list of source files, repo-relative. ``filelist_relative_to``:
  ``auto`` (default: filelist-relative, else repo-root-relative, whichever resolves),
  ``filelist`` or ``cwd`` (the repo root).
- ``parameters`` (HDL parameters), ``defines``, ``plusargs``: mappings.
- ``simulator`` (or ``tool``): ``verilator`` (default) or ``icarus``.
- ``test_module`` (or ``test_modules``): one or more cocotb test modules, each a ``.py``
  path (its directory goes on `PYTHONPATH`) or a dotted module name found in
  ``test_dir`` (default: the repo root).
- ``testcase``: a test name or a list of names to run (default: all).
- ``waves`` (bool): write ``dump.fst`` in the work root.
- ``seed`` (int, default 0; ``null`` for cocotb's random seed): `COCOTB_RANDOM_SEED`.
- ``timeout_s``: wall-time budget for the build and the run together.
- ``work_root``: ``state`` (default, ``.chipgraph/state/sim/<check>/<params>/<simulator>``,
  machine-local and not tracked), ``temp`` (a new temporary directory) or a path outside
  the project's source tree.
- ``env``: extra environment variables for the build and the run (the cocotb variables
  above win over them).
- ``tool_options``: extra Edalize tool options (e.g. ``verilator_options``,
  ``iverilog_options``, ``run_options``, ``timescale``), merged into the defaults.
- ``jobs``: parallel jobs for the Verilator model's `make` (default: CPU count, max 8).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from chipgraph.adapters.parser.cocotb import CocotbOutcome, CocotbParser, Simulator
from chipgraph.adapters.tool.filelist import Filelist, FilelistError, read_filelist
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.plugin_api.types import RunResult, ToolContext
from chipgraph.core.state.layout import StateLayout

__all__ = ["EdalizeTool", "SimArgs", "SimModules", "cocotb_env", "load_sim_modules"]

SIM_EXTRA_HINT = "uv sync --extra sim"
_SIMULATORS: tuple[Simulator, ...] = ("verilator", "icarus")
_EDAM_NAME = "chipgraph_sim"
_DUMP_TOP = "chipgraph_dump"
_RESULTS_FILE = "results.xml"
_WAVES_FILE = "dump.fst"
_DEFAULT_TIMESCALE = "1ns/1ps"
_CXX_CANDIDATES = ("g++", "c++", "clang++")
_LOG_TAIL_MAX_CHARS = 4000
_SLUG_RE = re.compile(r"[^A-Za-z0-9_.=-]+")


class _ArgError(Exception):
    """A malformed `spec.args`, turned into an `error` `CheckResult`."""


class _SetupError(Exception):
    """The environment cannot run the simulation (missing extra, tool, libpython ...)."""


@dataclass(frozen=True, slots=True)
class SimModules:
    """What the adapter needs from the `sim` extra, loaded lazily (see `load_sim_modules`)."""

    sim_flow: Callable[[dict[str, Any], Path], Any]
    """`edalize.flows.sim.Sim`: ``Sim(edam, work_root)``."""
    pygpi_entry_point: Callable[[], str]
    """`cocotb_tools.config.pygpi_entry_point`: the ``lib,func`` cocotb's GPI loads."""
    find_libpython: Callable[[], str | None]
    """`find_libpython.find_libpython`: the shared libpython of this interpreter."""
    versions: Mapping[str, str]
    """Installed versions of edalize and cocotb (part of the idempotency key)."""


def load_sim_modules() -> SimModules:
    """Import Edalize and cocotb (the `sim` extra); `_SetupError` if they are missing."""
    try:
        sim = importlib.import_module("edalize.flows.sim")
        cocotb_config = importlib.import_module("cocotb_tools.config")
        find_libpython = importlib.import_module("find_libpython")
    except ImportError as exc:
        raise _SetupError(
            f"the `sim` extra is not installed ({exc.name or exc}): run `{SIM_EXTRA_HINT}` "
            "(or `pip install 'chipgraph[sim]'`) to install edalize and cocotb"
        ) from exc
    return SimModules(
        sim_flow=sim.Sim,
        pygpi_entry_point=cocotb_config.pygpi_entry_point,
        find_libpython=find_libpython.find_libpython,
        versions=_package_versions(),
    )


@dataclass(frozen=True, slots=True)
class SimArgs:
    """`spec.args`, validated and resolved against the repo root and `ctx.params`."""

    simulator: Simulator
    top: str
    sources: tuple[Path, ...]
    incdirs: tuple[Path, ...]
    defines: dict[str, Any]
    parameters: dict[str, Any]
    plusargs: dict[str, Any]
    test_modules: tuple[str, ...]
    test_dirs: tuple[Path, ...]
    testcase: tuple[str, ...]
    waves: bool
    seed: int | None
    timeout_s: float | None
    work_root: str
    env: dict[str, str]
    tool_options: dict[str, Any]
    jobs: int
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_spec(cls, spec: CheckSpec, ctx: ToolContext) -> SimArgs:
        """Validate `spec.args`; raises `_ArgError` with a message naming the bad key."""
        args = spec.args
        root = ctx.repo_root.resolve()

        def fmt(value: object, key: str) -> str:
            if not isinstance(value, str):
                raise _ArgError(f"args[{key!r}] must be a string, got {value!r}")
            try:
                return value.format(**ctx.params)
            except (KeyError, IndexError, ValueError) as exc:
                raise _ArgError(f"args[{key!r}] = {value!r}: cannot substitute {exc}") from exc

        def str_list(key: str) -> list[str]:
            raw = args.get(key)
            if raw is None:
                return []
            items = [raw] if isinstance(raw, str) else raw
            if not isinstance(items, list):
                raise _ArgError(f"args[{key!r}] must be a string or a list of strings")
            return [fmt(item, key) for item in items]

        def mapping(key: str) -> dict[str, Any]:
            raw = args.get(key) or {}
            if not isinstance(raw, dict):
                raise _ArgError(f"args[{key!r}] must be a mapping")
            return {str(k): v for k, v in raw.items()}

        simulator = args.get("simulator", args.get("tool", "verilator"))
        if simulator not in _SIMULATORS:
            raise _ArgError(f"args['simulator'] must be one of {list(_SIMULATORS)}: {simulator!r}")
        if "top" not in args:
            raise _ArgError("args['top'] (the HDL toplevel) is required")
        top = fmt(args["top"], "top")

        sources: list[Path] = []
        incdirs: list[Path] = []
        defines: dict[str, Any] = {}
        warnings: list[str] = []
        if args.get("filelist") is not None:
            style = args.get("filelist_relative_to", "auto")
            if style not in ("auto", "filelist", "cwd"):
                raise _ArgError("args['filelist_relative_to'] must be auto, filelist or cwd")
            filelist = _read_filelist(root / fmt(args["filelist"], "filelist"), root, style)
            sources += filelist.sources
            incdirs += filelist.incdirs
            defines.update({k: "" if v is None else v for k, v in filelist.defines.items()})
            warnings += filelist.warnings
        sources += [(root / f).resolve() for f in str_list("files")]
        if not sources:
            raise _ArgError("no HDL source: set args['filelist'] and/or args['files']")
        missing = [str(p) for p in sources if not p.is_file()]
        if missing:
            raise _ArgError(f"HDL source(s) not found: {', '.join(missing)}")
        defines.update(mapping("defines"))

        modules: list[str] = []
        test_dirs: list[Path] = []
        default_dir = (root / fmt(args.get("test_dir", "."), "test_dir")).resolve()
        for raw in str_list("test_module") + str_list("test_modules"):
            name, directory = _resolve_test_module(raw, root, default_dir)
            modules.append(name)
            if directory not in test_dirs:
                test_dirs.append(directory)
        if not modules:
            raise _ArgError("args['test_module'] (the cocotb test module) is required")

        seed = args.get("seed", 0)
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise _ArgError("args['seed'] must be an integer or null")
        timeout_s = args.get("timeout_s")
        if timeout_s is not None and (
            isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float) or timeout_s <= 0
        ):
            raise _ArgError("args['timeout_s'] must be a positive number")
        jobs = args.get("jobs", min(os.cpu_count() or 1, 8))
        if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
            raise _ArgError("args['jobs'] must be a positive integer")
        env = {str(k): str(v) for k, v in mapping("env").items()}

        return cls(
            simulator=cast(Simulator, simulator),
            top=top,
            sources=tuple(dict.fromkeys(sources)),
            incdirs=tuple(dict.fromkeys(incdirs)),
            defines=defines,
            parameters=mapping("parameters"),
            plusargs=mapping("plusargs"),
            test_modules=tuple(dict.fromkeys(modules)),
            test_dirs=tuple(test_dirs),
            testcase=tuple(str_list("testcase")),
            waves=bool(args.get("waves", False)),
            seed=seed,
            timeout_s=float(timeout_s) if timeout_s is not None else None,
            work_root=fmt(args.get("work_root", "state"), "work_root"),
            env=env,
            tool_options=mapping("tool_options"),
            jobs=jobs,
            warnings=tuple(warnings),
        )


def cocotb_env(
    *,
    simulator: Simulator,
    top: str,
    test_modules: tuple[str, ...],
    test_dirs: tuple[Path, ...],
    results_file: Path,
    python: str,
    libpython: str,
    pygpi_entry_point: str,
    path: str,
    pythonpath: str = "",
    seed: int | None = 0,
    testcase: tuple[str, ...] = (),
) -> dict[str, str]:
    """The environment cocotb 2.x needs at build and run time, computed in process.

    It mirrors what `cocotb_tools.runner` sets (S3, pitfall 1): `GPI_USERS` is the one
    cocotb's GPI reads (without it the model prints "No GPI_USERS specified" and exits 0);
    `PYGPI_PYTHON_BIN` is this interpreter; the test directories, then `pythonpath`, then
    this interpreter's `sys.path` make up `PYTHONPATH`. `PATH` gets the interpreter's
    `bin` first, because Edalize's Makefile calls `cocotb-config` through the shell and
    another one (OSS CAD Suite has its own) must not shadow it.
    """
    py_path = [str(d) for d in test_dirs]
    py_path += [p for p in pythonpath.split(os.pathsep) if p]
    py_path += [p for p in sys.path[1:] if p]
    env = {
        "PATH": os.pathsep.join([str(Path(python).parent), path]),
        "PYTHONPATH": os.pathsep.join(dict.fromkeys(py_path)),
        "PYGPI_PYTHON_BIN": python,
        "GPI_USERS": f"{libpython};{pygpi_entry_point}",
        "COCOTB_TEST_MODULES": ",".join(test_modules),
        "COCOTB_TOPLEVEL": top,
        "TOPLEVEL_LANG": "verilog",
        "COCOTB_RESULTS_FILE": str(results_file),
    }
    if seed is not None:
        env["COCOTB_RANDOM_SEED"] = str(seed)
    if testcase:
        names = "|".join(re.escape(name) for name in testcase)
        env["COCOTB_TEST_FILTER"] = rf"(^|\.)({names})$"
    if simulator == "verilator":
        env["COCOTB_TRUST_INERTIAL_WRITES"] = "1"  # what cocotb's own Verilator runner sets
    return env


class EdalizeTool:
    """Runs a cocotb testbench on Verilator or Icarus (`capability="sim"`)."""

    name = "edalize"
    capability = "sim"

    def __init__(self, *, modules: Callable[[], SimModules] | None = None) -> None:
        self._load_modules = modules or load_sim_modules
        self._parser = CocotbParser()

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        key = self.key_for(spec, ctx)

        def error(msg: str, rule: str, log: str = "") -> CheckResult:
            return CheckResult(
                check_id=spec.id,
                status="error",
                issues=(Issue(msg=msg, rule=rule, severity="error"),),
                log_tail=log[-_LOG_TAIL_MAX_CHARS:],
                duration_s=time.monotonic() - start,
                idempotency_key=key,
            )

        try:
            args = SimArgs.from_spec(spec, ctx)
        except _ArgError as exc:
            return error(f"check {spec.id!r}: {exc}", "sim/args")
        try:
            modules = self._load_modules()
        except _SetupError as exc:
            return error(str(exc), "sim/setup")

        base_env = {**os.environ, **ctx.env, **args.env}
        path = base_env.get("PATH", "")
        python = sys.executable
        tools_path = os.pathsep.join([str(Path(python).parent), path])
        try:
            cxx = _check_tools(args.simulator, tools_path, base_env)
            libpython = modules.find_libpython()
            if not libpython:
                raise _SetupError(
                    f"no shared libpython found for {python} (find_libpython): cocotb "
                    "embeds Python in the simulator and needs a Python built with a "
                    "shared library"
                )
            work_root = _work_root(args, spec, ctx)
        except _SetupError as exc:
            return error(str(exc), "sim/setup")
        except _ArgError as exc:
            return error(f"check {spec.id!r}: {exc}", "sim/args")

        results = work_root / _RESULTS_FILE
        env = {
            **ctx.env,
            **args.env,
            **cocotb_env(
                simulator=args.simulator,
                top=args.top,
                test_modules=args.test_modules,
                test_dirs=args.test_dirs,
                results_file=results,
                python=python,
                libpython=libpython,
                pygpi_entry_point=modules.pygpi_entry_point(),
                path=path,
                pythonpath=base_env.get("PYTHONPATH", ""),
                seed=args.seed,
                testcase=args.testcase,
            ),
        }

        deadline = start + args.timeout_s if args.timeout_s is not None else None

        def remaining() -> float | None:
            return None if deadline is None else max(deadline - time.monotonic(), 1.0)

        options = await _tool_options(args, ctx, cxx, work_root, env)
        edam = _edam(args, work_root, options)
        _reset_work_root(work_root, edam)
        try:
            flow = modules.sim_flow(edam, work_root)
            flow.configure()
            build_cmd, build_args = flow.build_runner.get_build_command()
            run_cmd, run_args, run_cwd = flow.flow.get_node(args.simulator).inst.run()
        except Exception as exc:  # Edalize raises plain RuntimeError/ValueError/KeyError ...
            return error(
                f"Edalize could not configure the {args.simulator} flow: {exc}", "sim/setup"
            )

        build = await ctx.runner.run(
            [build_cmd, *build_args], cwd=work_root, env=env, timeout_s=remaining()
        )
        build_log = build.stdout + build.stderr
        _write(work_root / "build.log", build_log)

        run: RunResult | None = None
        run_log = ""
        if build.returncode == 0 and not build.timed_out:
            run_cwd = Path(run_cwd)
            run_exe = str(run_cwd / run_cmd) if str(run_cmd).startswith("./") else str(run_cmd)
            run = await ctx.runner.run(
                [run_exe, *run_args], cwd=run_cwd, env=env, timeout_s=remaining()
            )
            run_log = run.stdout + run.stderr
            _write(work_root / "run.log", run_log)

        outcome = self._parser.parse_run(
            results,
            simulator=args.simulator,
            build_rc=build.returncode,
            build_log=build_log,
            build_timed_out=build.timed_out,
            run_rc=run.returncode if run is not None else None,
            run_log=run_log,
            run_timed_out=run.timed_out if run is not None else False,
        )
        warnings = tuple(
            Issue(msg=w, rule="sim/filelist", severity="warning") for w in args.warnings
        )
        return CheckResult(
            check_id=spec.id,
            status=outcome.status,
            issues=(*(_in_repo(i, ctx.repo_root) for i in outcome.issues), *warnings),
            log_tail=_log_tail(build_log if run is None else run_log, outcome, args, work_root),
            duration_s=time.monotonic() - start,
            idempotency_key=key,
        )

    @staticmethod
    def key_for(spec: CheckSpec, ctx: ToolContext) -> str:
        """A stable idempotency key: the inputs that decide what the simulation does.

        Hashes the check id, the resolved arguments, the sha256 of every HDL source and of
        every `.py` file in the test directories, `ctx.env`/`ctx.params`, the Python
        version, the installed edalize and cocotb versions, and the simulator's
        executables (resolved path, size, mtime). Not the work root, the timeout or any
        output. Never raises: with invalid args, it keys on the raw `spec.args`.
        """
        try:
            args = SimArgs.from_spec(spec, ctx)
        except _ArgError:
            raw = json.dumps(spec.args, sort_keys=True, default=str)
            return _sha256_text(repr((spec.id, "invalid-args", raw)))
        root = ctx.repo_root.resolve()
        test_files = sorted(
            {p.resolve() for d in args.test_dirs if d.is_dir() for p in d.glob("*.py")}
        )
        path = {**os.environ, **ctx.env, **args.env}.get("PATH", "")
        tools = _REQUIRED_TOOLS[args.simulator]
        payload = {
            "check": spec.id,
            "simulator": args.simulator,
            "top": args.top,
            "sources": [[_relative(str(p), root), _sha256_file(p)] for p in args.sources],
            "tests": [[_relative(str(p), root), _sha256_file(p)] for p in test_files],
            "incdirs": [_relative(str(p), root) for p in args.incdirs],
            "test_modules": list(args.test_modules),
            "testcase": list(args.testcase),
            "defines": args.defines,
            "parameters": args.parameters,
            "plusargs": args.plusargs,
            "waves": args.waves,
            "seed": args.seed,
            "env": args.env,
            "tool_options": args.tool_options,
            "ctx_env": ctx.env,
            "ctx_params": ctx.params,
            "python": list(sys.version_info[:3]),
            "packages": dict(_package_versions()),
            "executables": {t: _fingerprint(t, path) for t in tools},
        }
        return _sha256_text(json.dumps(payload, sort_keys=True, default=str))


_REQUIRED_TOOLS: dict[Simulator, tuple[str, ...]] = {
    "verilator": ("verilator", "make"),
    "icarus": ("iverilog", "vvp", "make"),
}


def _check_tools(simulator: Simulator, path: str, env: Mapping[str, str]) -> str | None:
    """Raise `_SetupError` naming what is missing; return the C++ compiler (Verilator)."""
    missing = [t for t in _REQUIRED_TOOLS[simulator] if shutil.which(t, path=path) is None]
    cxx: str | None = None
    if simulator == "verilator":
        wanted = env.get("CXX")
        candidates = (wanted,) if wanted else _CXX_CANDIDATES
        cxx = next((c for c in candidates if shutil.which(c, path=path)), None)
        if cxx is None:
            missing.append(f"a C++ compiler ({wanted or 'g++'})")
    if missing:
        hint = (
            "Verilator compiles the model with a C++ compiler (e.g. `apt-get install g++`)"
            if simulator == "verilator"
            else "install Icarus Verilog from the distribution (e.g. `apt-get install iverilog`)"
        )
        raise _SetupError(f"{simulator} simulation needs {', '.join(missing)} on PATH: {hint}")
    return cxx


async def _tool_options(
    args: SimArgs, ctx: ToolContext, cxx: str | None, work_root: Path, env: Mapping[str, str]
) -> dict[str, Any]:
    """Per-simulator Edalize options, with the S3 fixes as detected on this machine."""
    opts: dict[str, Any]
    if args.simulator == "verilator":
        verilator_options = await _cxx_std_args(ctx, cxx, work_root, env)
        verilator_options += [f"+incdir+{d}" for d in args.incdirs]
        run_options: list[str] = []
        if args.waves:
            verilator_options += ["--trace-fst", *_fst_lib_args()]
            run_options.append("--trace")
        opts = {
            "verilator_options": verilator_options,
            "run_options": run_options,
            "make_options": [f"-j{args.jobs}"],
        }
    else:
        # tinysoc-style SystemVerilog (logic, always_ff) needs -g2012, and a timescale so
        # a 10 ns cocotb clock is representable (Icarus defaults to 1 s): S3, pitfall 8.
        opts = {
            "iverilog_options": ["-g2012", *(f"-I{d}" for d in args.incdirs)],
            "timescale": _DEFAULT_TIMESCALE,
        }
    for key, value in args.tool_options.items():
        if isinstance(value, list) and isinstance(opts.get(key), list):
            opts[key] = [*opts[key], *(str(v) for v in value)]
        else:
            opts[key] = value
    return opts


async def _cxx_std_args(
    ctx: ToolContext, cxx: str | None, work_root: Path, env: Mapping[str, str]
) -> list[str]:
    """``-CFLAGS -std=gnu++17`` when the C++ compiler defaults below C++14 (S3, pitfall 6).

    Verilator 5.05x needs C++14 and adds a ``-std=`` flag only if the compiler it was
    built with needed one; old Apple Command Line Tools default to C++98.
    """
    if cxx is None:
        return []
    probe = await ctx.runner.run(
        [cxx, "-dM", "-E", "-x", "c++", os.devnull], cwd=work_root, env=dict(env), timeout_s=30
    )
    for line in probe.stdout.splitlines():
        if line.startswith("#define __cplusplus "):
            try:
                value = int(line.split()[2].rstrip("L"))
            except (IndexError, ValueError):
                return []
            return ["-CFLAGS", "-std=gnu++17"] if value < 201402 else []
    return []


def _fst_lib_args() -> list[str]:
    """Where Verilator's FST writer finds lz4/zlib on macOS with Homebrew (S3, pitfall 7).

    On Linux the headers come from the distribution (`liblz4-dev`, `zlib1g-dev`) on the
    default paths; Apple clang does not search the Homebrew prefix.
    """
    if sys.platform != "darwin":
        return []
    for prefix in (os.environ.get("HOMEBREW_PREFIX", ""), "/opt/homebrew", "/usr/local"):
        if prefix and (Path(prefix) / "include" / "lz4.h").is_file():
            return ["-CFLAGS", f"-I{prefix}/include", "-LDFLAGS", f"-L{prefix}/lib"]
    return []


def _edam(args: SimArgs, work_root: Path, options: Mapping[str, Any]) -> dict[str, Any]:
    """The EDAM for Edalize's `sim` flow; `cocotb_module` turns its cocotb glue on."""
    files = [{"name": str(p), "file_type": _file_type(p)} for p in args.sources]
    toplevel = args.top
    if args.simulator == "icarus" and args.waves:
        # Edalize already runs `vvp ... -fst`; a second top with $dumpvars does the rest.
        dump = work_root / f"{_DUMP_TOP}.v"
        timescale = str(options.get("timescale", _DEFAULT_TIMESCALE))
        _write(
            dump,
            f"`timescale {timescale.replace('/', ' / ')}\nmodule {_DUMP_TOP};\n"
            f'  initial begin\n    $dumpfile("{_WAVES_FILE}");\n'
            f"    $dumpvars(0, {args.top});\n  end\nendmodule\n",
        )
        files.append({"name": str(dump), "file_type": "verilogSource"})
        toplevel = f"{args.top} {_DUMP_TOP}"
    parameters: dict[str, Any] = {}
    for kind, values in (
        ("vlogparam", args.parameters),
        ("vlogdefine", args.defines),
        ("plusarg", args.plusargs),
    ):
        for name, value in values.items():
            parameters[name] = {"datatype": _datatype(value), "paramtype": kind, "default": value}
    return {
        "name": _EDAM_NAME,
        "toplevel": toplevel,
        "files": files,
        "parameters": parameters,
        "flow_options": {
            "tool": args.simulator,
            "cocotb_module": args.test_modules[0],
            **options,
        },
    }


def _work_root(args: SimArgs, spec: CheckSpec, ctx: ToolContext) -> Path:
    """The work root: under the run state, a new temp dir, or a path outside the sources."""
    repo = ctx.repo_root.resolve()
    state_dir = StateLayout(repo).state_dir
    if args.work_root == "state":
        params = ",".join(f"{k}={v}" for k, v in sorted(ctx.params.items())) or "_"
        path = state_dir / "sim" / _slug(spec.id) / _slug(params) / args.simulator
    elif args.work_root == "temp":
        path = Path(tempfile.mkdtemp(prefix=f"chipgraph-sim-{_slug(spec.id)}-"))
    else:
        path = Path(args.work_root)
        path = (path if path.is_absolute() else repo / path).resolve()
        if path.is_relative_to(repo) and not path.is_relative_to(state_dir.resolve()):
            raise _ArgError(
                f"args['work_root'] {args.work_root!r} is inside the project's source tree; "
                "use `state`, `temp` or a directory outside the repo"
            )
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _reset_work_root(work_root: Path, edam: Mapping[str, Any]) -> None:
    """Remove stale outputs; wipe the build when the EDAM changed since the last run.

    `results.xml` and `dump.fst` are always removed first, so an old result can never be
    read as this run's.
    """
    for name in (_RESULTS_FILE, _WAVES_FILE, "build.log", "run.log"):
        (work_root / name).unlink(missing_ok=True)
    stamp = work_root / "edam.json"
    text = json.dumps(edam, indent=2, sort_keys=True, default=str) + "\n"
    old = stamp.read_text(encoding="utf-8") if stamp.is_file() else None
    if old is not None and old != text:
        for child in work_root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            elif child.name != f"{_DUMP_TOP}.v":
                child.unlink()
    _write(stamp, text)


def _read_filelist(path: Path, root: Path, style: str) -> Filelist:
    try:
        if style in ("filelist", "cwd"):
            return read_filelist(path, relative_to=cast(Any, style), root=root)
        first = read_filelist(path, relative_to="filelist", root=root)
        if first.sources and all(p.is_file() for p in first.sources):
            return first
        alt = read_filelist(path, relative_to="cwd", root=root)
        if sum(p.is_file() for p in alt.sources) > sum(p.is_file() for p in first.sources):
            return alt
        return first
    except FilelistError as exc:
        raise _ArgError(str(exc)) from exc


def _resolve_test_module(raw: str, root: Path, default_dir: Path) -> tuple[str, Path]:
    """(module name, directory for PYTHONPATH) of a `.py` path or a dotted module name."""
    if raw.endswith(".py") or "/" in raw:
        file = (root / raw).resolve()
        if not file.is_file():
            raise _ArgError(f"cocotb test module not found: {raw}")
        return file.stem, file.parent
    if not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", raw):
        raise _ArgError(f"invalid cocotb test module name: {raw!r}")
    rel = Path(*raw.split("."))
    if not ((default_dir / rel).with_suffix(".py").is_file() or (default_dir / rel).is_dir()):
        raise _ArgError(f"cocotb test module {raw!r} not found in {default_dir}")
    return raw, default_dir


def _log_tail(log: str, outcome: CocotbOutcome, args: SimArgs, work_root: Path) -> str:
    footer = (
        f"\n--- chipgraph sim ({args.simulator}, top {args.top}): {outcome.status}, "
        f"{outcome.summary}; work root {work_root}\n"
    )
    return log[-(_LOG_TAIL_MAX_CHARS - len(footer)) :] + footer


def _in_repo(issue: Issue, root: Path) -> Issue:
    """`issue` with a repo-relative `file`; a file outside the repo moves into the message.

    Findings (and their evidence) only take repo-relative paths, and a test module or a
    source may live outside the project (e.g. a shared testbench).
    """
    if issue.file is None:
        return issue
    rel = _relative(issue.file, root)
    if not Path(rel).is_absolute():
        return issue.model_copy(update={"file": rel})
    where = f"{issue.file}:{issue.line}" if issue.line is not None else issue.file
    return issue.model_copy(update={"file": None, "line": None, "msg": f"{issue.msg} (at {where})"})


def _relative(path: str, root: Path) -> str:
    """`path` relative to `root` (a relative `path` is taken from `root`); else absolute."""
    full = Path(path) if Path(path).is_absolute() else root / path
    try:
        return full.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(full.resolve())
    except OSError:
        return str(full)


def _file_type(path: Path) -> str:
    return "verilogSource" if path.suffix in (".v", ".vh") else "systemVerilogSource"


def _datatype(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "real"
    return "str"


def _slug(text: str) -> str:
    return _SLUG_RE.sub("_", text).strip("_") or "_"


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


def _fingerprint(tool: str, path: str) -> list[object]:
    """(resolved path, size, mtime_ns) of `tool` on `path`: changes when it is upgraded."""
    found = shutil.which(tool, path=path)
    if found is None:
        return ["missing"]
    real = Path(found).resolve()
    try:
        st = real.stat()
    except OSError:
        return [str(real)]
    return [str(real), st.st_size, st.st_mtime_ns]


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for dist in ("edalize", "cocotb"):
        try:
            versions[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            versions[dist] = "missing"
    return versions

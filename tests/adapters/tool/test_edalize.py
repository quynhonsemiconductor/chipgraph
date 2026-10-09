"""Unit tests for `EdalizeTool` (no simulator, compiler or `sim` extra needed).

Edalize and cocotb are replaced by a fake `SimModules` (a fake `Sim` flow that records
its EDAM and fails if its own `build()`/`run()` are called) and the runner by a scripted
fake, so these run in the plain `make check` environment. The real thing runs in
`tests/e2e/test_sim_edalize.py`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chipgraph.adapters.tool import edalize as edalize_mod
from chipgraph.adapters.tool.edalize import EdalizeTool, SimModules, cocotb_env, load_sim_modules
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api import Registry
from chipgraph.core.plugin_api.protocols import ToolAdapter
from chipgraph.core.plugin_api.types import RunResult, ToolContext

LIBPYTHON = "/py/lib/libpython3.14.so"
ENTRY = "/venv/site-packages/cocotb/simulator.so,initialize"

PASS_XML = """<testsuites><testsuite name="tb"><testcase classname="tb" name="test_a">
<properties><property name="file" value="{tb}" /><property name="line" value="3" /></properties>
</testcase></testsuite></testsuites>"""
FAIL_XML = """<testsuites><testsuite name="tb"><testcase classname="tb" name="test_a">
<failure type="AssertionError" message="got 1, expected 2">Traceback (most recent call last):
  File "{tb}", line 5, in test_a
    assert 1 == 2
AssertionError: got 1, expected 2
</failure>
<properties><property name="file" value="{tb}" /><property name="line" value="3" /></properties>
</testcase></testsuite></testsuites>"""


# --- fakes ---------------------------------------------------------------------------


@dataclass
class Call:
    cmd: list[str]
    cwd: Path
    env: dict[str, str]
    timeout_s: float | None


@dataclass
class FakeRunner:
    """Scripted `Runner`: the build/run steps return what the test sets."""

    build_rc: int = 0
    build_log: str = ""
    run_rc: int = 0
    run_log: str = ""
    results: str | None = None  # text written to COCOTB_RESULTS_FILE by the "run" step
    cplusplus: str = "201703L"
    calls: list[Call] = field(default_factory=list)
    name: str = "fake"

    async def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> RunResult:
        call = Call(list(cmd), cwd, dict(env or {}), timeout_s)
        self.calls.append(call)
        if len(cmd) > 1 and cmd[1] == "-dM":
            return _rr(0, f"#define __cplusplus {self.cplusplus}\n")
        if self.step(call) == "build":
            return _rr(self.build_rc, self.build_log)
        if self.results is not None:
            Path(call.env["COCOTB_RESULTS_FILE"]).write_text(self.results)
        return _rr(self.run_rc, self.run_log)

    @staticmethod
    def step(call: Call) -> str:
        if call.cmd[:2] == ["make", "run"] or call.cmd[0].endswith("Vtop"):
            return "run"
        if len(call.cmd) > 1 and call.cmd[1] == "-dM":
            return "probe"
        return "build"

    def steps(self) -> list[str]:
        return [self.step(c) for c in self.calls]


def _rr(rc: int, out: str) -> RunResult:
    return RunResult(returncode=rc, stdout=out, stderr="", duration_s=0.01)


@dataclass
class FakeEdalize:
    """A fake `edalize.flows.sim.Sim`; records EDAMs, refuses Edalize's own build/run."""

    edams: list[dict[str, Any]] = field(default_factory=list)
    libpython: str | None = LIBPYTHON

    def modules(self) -> SimModules:
        def sim_flow(edam: dict[str, Any], work_root: Path) -> Any:
            self.edams.append(edam)
            tool = edam["flow_options"]["tool"]
            run = ("./Vtop", [], work_root) if tool == "verilator" else ("make", ["run"], work_root)

            def forbidden() -> None:
                raise AssertionError("Edalize's own build()/run() must never be called")

            return SimpleNamespace(
                configure=lambda: (work_root / "Makefile").write_text("# fake\n"),
                build=forbidden,
                run=forbidden,
                build_runner=SimpleNamespace(get_build_command=lambda: ("make", [])),
                flow=SimpleNamespace(
                    get_node=lambda name: SimpleNamespace(inst=SimpleNamespace(run=lambda: run))
                ),
            )

        return SimModules(
            sim_flow=sim_flow,
            pygpi_entry_point=lambda: ENTRY,
            find_libpython=lambda: self.libpython,
            versions={"edalize": "0.6.8", "cocotb": "2.1.0"},
        )


# --- fixtures ------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "rtl").mkdir(parents=True)
    (root / "dv").mkdir()
    (root / "filelists").mkdir()
    (root / "rtl" / "tiny_gpio.sv").write_text("module tiny_gpio; endmodule\n")
    (root / "rtl" / "inc").mkdir()
    (root / "filelists" / "gpio.f").write_text("+incdir+rtl/inc\n+define+SIM\nrtl/tiny_gpio.sv\n")
    (root / "dv" / "tb_gpio.py").write_text("# cocotb tests\n")
    (root / "dv" / "helpers.py").write_text("# helpers\n")
    return root


@pytest.fixture
def tool_path(tmp_path: Path) -> str:
    """A PATH with fake verilator, iverilog, vvp, make and g++ executables."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("verilator", "iverilog", "vvp", "make", "g++"):
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
    return str(bin_dir)


def _spec(**args: Any) -> CheckSpec:
    base: dict[str, Any] = {
        "top": "tiny_{block}",
        "filelist": "filelists/{block}.f",
        "test_module": "dv/tb_{block}.py",
    }
    base.update(args)
    return CheckSpec(id="sim", capability="sim", adapter="edalize", args=base)


def _ctx(repo: Path, runner: Any, path: str, **env: str) -> ToolContext:
    return ToolContext(
        repo_root=repo, runner=runner, env={"PATH": path, **env}, params={"block": "gpio"}
    )


def _run(
    repo: Path,
    runner: FakeRunner,
    path: str,
    *,
    fake: FakeEdalize | None = None,
    modules: Callable[[], SimModules] | None = None,
    **args: Any,
) -> CheckResult:
    tool = EdalizeTool(modules=modules or (fake or FakeEdalize()).modules)
    return asyncio.run(tool.run(_spec(**args), _ctx(repo, runner, path)))


def _tb(repo: Path) -> str:
    return str((repo / "dv" / "tb_gpio.py").resolve())


# --- registration --------------------------------------------------------------------


def test_is_a_sim_tool_adapter_found_by_its_entry_point() -> None:
    tool = EdalizeTool()
    assert isinstance(tool, ToolAdapter)
    assert (tool.name, tool.capability) == ("edalize", "sim")
    registry = Registry()
    registry.discover()
    assert isinstance(registry.get("tool", "edalize"), EdalizeTool)


# --- the happy path, through the runner ---------------------------------------------


@pytest.mark.parametrize("simulator", ["verilator", "icarus"])
def test_pass_builds_and_runs_through_the_runner(
    simulator: str, repo: Path, tool_path: str
) -> None:
    runner = FakeRunner(results=PASS_XML.format(tb=_tb(repo)), run_log="TESTS=1 PASS=1")
    fake = FakeEdalize()
    result = _run(repo, runner, tool_path, fake=fake, simulator=simulator, timeout_s=120)
    assert result.status == "pass", result
    assert result.issues == ()
    expected = (["probe"] if simulator == "verilator" else []) + ["build", "run"]
    assert runner.steps() == expected
    build, run = runner.calls[-2:]
    work = build.cwd
    assert work == (repo / ".chipgraph" / "state" / "sim" / "sim" / "block=gpio" / simulator)
    assert work.resolve() == work
    if simulator == "verilator":
        assert run.cmd == [str(work / "Vtop")]
    else:
        assert run.cmd == ["make", "run"]
    assert build.timeout_s is not None and 0 < build.timeout_s <= 120
    assert "tests=1 pass=1" in result.log_tail and str(work) in result.log_tail
    assert (work / "build.log").is_file() and (work / "run.log").is_file()
    assert result.idempotency_key == EdalizeTool.key_for(
        _spec(simulator=simulator, timeout_s=120), _ctx(repo, runner, tool_path)
    )
    assert len(fake.edams) == 1


def test_failure_is_fail_with_a_repo_relative_location(repo: Path, tool_path: str) -> None:
    runner = FakeRunner(results=FAIL_XML.format(tb=_tb(repo)))
    result = _run(repo, runner, tool_path)
    assert result.status == "fail"
    (issue,) = result.issues
    assert (issue.rule, issue.file, issue.line) == ("cocotb/AssertionError", "dv/tb_gpio.py", 5)


def test_issue_files_outside_the_repo_move_into_the_message(
    repo: Path, tool_path: str, tmp_path: Path
) -> None:
    outside = tmp_path / "shared" / "tb_gpio.py"
    outside.parent.mkdir()
    outside.write_text("# shared testbench\n")
    runner = FakeRunner(results=FAIL_XML.format(tb=str(outside.resolve())))
    result = _run(repo, runner, tool_path, test_module=str(outside))
    assert result.status == "fail"
    (issue,) = result.issues
    assert (issue.file, issue.line) == (None, None)
    assert issue.msg.endswith(f"(at {outside.resolve()}:5)")


def test_issue_files_are_valid_finding_evidence(repo: Path, tool_path: str) -> None:
    from chipgraph.core.contracts.finding import Evidence

    log = "%Error: /elsewhere/x.sv:3:5: syntax error\n/abs/y.sv:4: error: bad\n"
    result = _run(repo, FakeRunner(build_rc=2, build_log=log), tool_path)
    assert result.status == "error"
    for issue in result.issues:
        if issue.file is not None:
            Evidence(file=issue.file, line=issue.line)


def test_build_error_stops_before_the_run(repo: Path, tool_path: str) -> None:
    log = (
        "%Error: rtl/tiny_gpio.sv:3:5: syntax error, unexpected ';'\nmake: *** [Vtop.mk] Error 1\n"
    )
    runner = FakeRunner(build_rc=2, build_log=log, results=PASS_XML.format(tb=_tb(repo)))
    result = _run(repo, runner, tool_path)
    assert result.status == "error"
    assert "run" not in runner.steps()
    assert [(i.rule, i.file, i.line) for i in result.issues] == [
        ("verilator/error", "rtl/tiny_gpio.sv", 3)
    ]
    assert "syntax error" in result.log_tail


# --- "no results.xml is never a pass", in every path ---------------------------------


def test_green_exit_without_results_is_an_error(repo: Path, tool_path: str) -> None:
    runner = FakeRunner(run_log="ERROR gpi ... in gpi_load_users   No GPI_USERS specified")
    result = _run(repo, runner, tool_path)
    assert result.status == "error"
    assert result.issues[0].rule == "cocotb/no_results"


def test_a_stale_results_file_from_a_previous_run_is_never_read(repo: Path, tool_path: str) -> None:
    first = _run(repo, FakeRunner(results=PASS_XML.format(tb=_tb(repo))), tool_path)
    assert first.status == "pass"
    again = _run(repo, FakeRunner(results=None), tool_path)  # this run writes no results
    assert again.status == "error"
    assert again.issues[0].rule == "cocotb/no_results"


@pytest.mark.parametrize("text", ["", "<testsuites/>", "<testsuites><testsuite"])
def test_empty_or_bad_results_is_an_error(repo: Path, tool_path: str, text: str) -> None:
    result = _run(repo, FakeRunner(results=text), tool_path)
    assert result.status == "error"
    assert result.issues[0].rule == "cocotb/no_results"


def _missing_extra() -> SimModules:
    raise edalize_mod._SetupError("the `sim` extra is not installed: run `uv sync --extra sim`")


@pytest.mark.parametrize(
    "setup",
    [
        "bad-args",
        "missing-extra",
        "missing-tool",
        "no-libpython",
        "build-error",
        "build-timeout",
        "run-timeout",
        "nonzero-exit",
        "no-results",
    ],
)
def test_no_path_without_a_results_file_is_a_pass(
    setup: str, repo: Path, tool_path: str, tmp_path: Path
) -> None:
    runner = FakeRunner()
    fake = FakeEdalize()
    kwargs: dict[str, Any] = {}
    path = tool_path
    if setup == "bad-args":
        kwargs["top"] = None
    elif setup == "missing-extra":
        kwargs["modules"] = _missing_extra
    elif setup == "missing-tool":
        path = str(tmp_path)
    elif setup == "no-libpython":
        fake.libpython = None
    elif setup == "build-error":
        runner.build_rc = 2
    elif setup == "build-timeout":
        runner = _TimeoutRunner("build")
    elif setup == "run-timeout":
        runner = _TimeoutRunner("run")
    elif setup == "nonzero-exit":
        runner.run_rc = 1
    result = _run(repo, runner, path, fake=fake, **kwargs)
    assert result.status == "error", result
    assert not result.ok
    assert any(i.severity == "error" for i in result.issues)


class _TimeoutRunner(FakeRunner):
    def __init__(self, step: str) -> None:
        super().__init__()
        self.timeout_step = step

    async def run(self, cmd: Sequence[str], **kw: Any) -> RunResult:
        result = await super().run(cmd, **kw)
        if self.step(self.calls[-1]) == self.timeout_step:
            return result.model_copy(update={"returncode": -9, "timed_out": True})
        return result


# --- setup errors name what is missing ----------------------------------------------


def test_missing_extra_says_how_to_install_it(
    repo: Path, tool_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_edalize(name: str) -> Any:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(edalize_mod.importlib, "import_module", no_edalize)
    with pytest.raises(edalize_mod._SetupError, match=r"uv sync --extra sim"):
        load_sim_modules()
    result = _run(repo, FakeRunner(), tool_path, modules=load_sim_modules)
    assert result.status == "error"
    (issue,) = result.issues
    assert issue.rule == "sim/setup"
    assert "uv sync --extra sim" in issue.msg and "edalize" in issue.msg


def _path_with(tmp_path: Path, *names: str) -> str:
    bin_dir = tmp_path / "somebin"
    bin_dir.mkdir()
    for name in names:
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    return str(bin_dir)


def test_missing_simulator_is_named(repo: Path, tmp_path: Path) -> None:
    result = _run(repo, FakeRunner(), _path_with(tmp_path, "make", "g++"))
    assert result.status == "error"
    assert "verilator" in result.issues[0].msg
    assert result.issues[0].rule == "sim/setup"


def test_missing_cxx_compiler_is_named(repo: Path, tmp_path: Path) -> None:
    result = _run(repo, FakeRunner(), _path_with(tmp_path, "verilator", "make"))
    assert result.status == "error"
    assert "C++ compiler (g++)" in result.issues[0].msg
    assert "apt-get install g++" in result.issues[0].msg


def test_missing_vvp_is_named_for_icarus(repo: Path, tmp_path: Path) -> None:
    result = _run(repo, FakeRunner(), _path_with(tmp_path, "iverilog", "make"), simulator="icarus")
    assert result.status == "error"
    assert "vvp" in result.issues[0].msg and "iverilog" in result.issues[0].msg


def test_icarus_needs_no_cxx_compiler(repo: Path, tmp_path: Path) -> None:
    path = _path_with(tmp_path, "iverilog", "vvp", "make")
    runner = FakeRunner(results=PASS_XML.format(tb=_tb(repo)))
    assert _run(repo, runner, path, simulator="icarus").status == "pass"


def test_no_libpython_is_an_error(repo: Path, tool_path: str) -> None:
    fake = FakeEdalize(libpython=None)
    result = _run(repo, FakeRunner(), tool_path, fake=fake)
    assert result.status == "error"
    assert "libpython" in result.issues[0].msg


# --- environment ---------------------------------------------------------------------


def test_cocotb_env_has_what_cocotb_2_needs(tmp_path: Path) -> None:
    env = cocotb_env(
        simulator="verilator",
        top="tiny_gpio",
        test_modules=("tb_a", "tb_b"),
        test_dirs=(tmp_path / "dv",),
        results_file=tmp_path / "w" / "results.xml",
        python="/venv/bin/python",
        libpython=LIBPYTHON,
        pygpi_entry_point=ENTRY,
        path="/usr/bin:/bin",
        pythonpath="/extra",
        seed=7,
        testcase=("test_a", "test.b"),
    )
    assert env["GPI_USERS"] == f"{LIBPYTHON};{ENTRY}"
    assert env["PYGPI_PYTHON_BIN"] == "/venv/bin/python"
    assert env["PATH"] == os.pathsep.join(["/venv/bin", "/usr/bin:/bin"])
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(tmp_path / "dv"), "/extra"]
    assert env["COCOTB_TEST_MODULES"] == "tb_a,tb_b"
    assert env["COCOTB_TOPLEVEL"] == "tiny_gpio"
    assert env["TOPLEVEL_LANG"] == "verilog"
    assert env["COCOTB_RESULTS_FILE"] == str(tmp_path / "w" / "results.xml")
    assert env["COCOTB_RANDOM_SEED"] == "7"
    assert env["COCOTB_TRUST_INERTIAL_WRITES"] == "1"
    assert env["COCOTB_TEST_FILTER"] == r"(^|\.)(test_a|test\.b)$"

    icarus = cocotb_env(
        simulator="icarus",
        top="t",
        test_modules=("m",),
        test_dirs=(),
        results_file=tmp_path / "r.xml",
        python="/venv/bin/python",
        libpython=LIBPYTHON,
        pygpi_entry_point=ENTRY,
        path="",
        seed=None,
    )
    assert "COCOTB_TRUST_INERTIAL_WRITES" not in icarus
    assert "COCOTB_RANDOM_SEED" not in icarus
    assert "COCOTB_TEST_FILTER" not in icarus


def test_the_runner_gets_the_cocotb_env_with_the_venv_bin_first(repo: Path, tool_path: str) -> None:
    runner = FakeRunner(results=PASS_XML.format(tb=_tb(repo)))
    _run(repo, runner, tool_path, testcase="test_a", env={"MY_VAR": "1"}, seed=3)
    build, run = runner.calls[-2:]
    for call in (build, run):
        env = call.env
        assert env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
        assert tool_path in env["PATH"].split(os.pathsep)
        assert env["GPI_USERS"] == f"{LIBPYTHON};{ENTRY}"
        assert env["COCOTB_TEST_MODULES"] == "tb_gpio"
        assert env["COCOTB_TOPLEVEL"] == "tiny_gpio"
        assert env["COCOTB_RESULTS_FILE"] == str(build.cwd / "results.xml")
        assert env["COCOTB_RANDOM_SEED"] == "3"
        assert env["MY_VAR"] == "1"
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str((repo / "dv").resolve())
        assert "COCOTB_TEST_FILTER" in env


def test_dotted_module_in_test_dir(repo: Path, tool_path: str) -> None:
    runner = FakeRunner(results=PASS_XML.format(tb=_tb(repo)))
    result = _run(repo, runner, tool_path, test_module="tb_gpio", test_dir="dv")
    assert result.status == "pass"
    assert runner.calls[-1].env["COCOTB_TEST_MODULES"] == "tb_gpio"


# --- the EDAM and the S3 fixes -------------------------------------------------------


def test_edam_from_the_filelist_and_args(repo: Path, tool_path: str) -> None:
    fake = FakeEdalize()
    _run(
        repo,
        FakeRunner(results=PASS_XML.format(tb=_tb(repo))),
        tool_path,
        fake=fake,
        parameters={"WIDTH": 8},
        plusargs={"seed": "1"},
        tool_options={"verilator_options": ["-Wno-fatal"]},
        jobs=2,
    )
    (edam,) = fake.edams
    assert edam["toplevel"] == "tiny_gpio"
    assert edam["files"] == [
        {"name": str((repo / "rtl" / "tiny_gpio.sv").resolve()), "file_type": "systemVerilogSource"}
    ]
    assert edam["parameters"]["WIDTH"] == {
        "datatype": "int",
        "paramtype": "vlogparam",
        "default": 8,
    }
    assert edam["parameters"]["SIM"]["paramtype"] == "vlogdefine"
    assert edam["parameters"]["seed"]["paramtype"] == "plusarg"
    opts = edam["flow_options"]
    assert opts["tool"] == "verilator" and opts["cocotb_module"] == "tb_gpio"
    assert f"+incdir+{(repo / 'rtl' / 'inc').resolve()}" in opts["verilator_options"]
    assert opts["verilator_options"][-1] == "-Wno-fatal"
    assert opts["make_options"] == ["-j2"]
    assert "-std=gnu++17" not in opts["verilator_options"]


def test_old_cxx_default_gets_the_cxx17_flag(repo: Path, tool_path: str) -> None:
    fake = FakeEdalize()
    runner = FakeRunner(results=PASS_XML.format(tb=_tb(repo)), cplusplus="199711L")
    _run(repo, runner, tool_path, fake=fake)
    assert runner.calls[0].cmd[:2] == ["g++", "-dM"]
    assert fake.edams[0]["flow_options"]["verilator_options"][:2] == ["-CFLAGS", "-std=gnu++17"]


def test_icarus_options(repo: Path, tool_path: str) -> None:
    fake = FakeEdalize()
    _run(
        repo,
        FakeRunner(results=PASS_XML.format(tb=_tb(repo))),
        tool_path,
        fake=fake,
        simulator="icarus",
    )
    opts = fake.edams[0]["flow_options"]
    assert opts["iverilog_options"][0] == "-g2012"
    assert opts["timescale"] == "1ns/1ps"


@pytest.mark.parametrize("simulator", ["verilator", "icarus"])
def test_waves(simulator: str, repo: Path, tool_path: str) -> None:
    fake = FakeEdalize()
    runner = FakeRunner(results=PASS_XML.format(tb=_tb(repo)))
    _run(repo, runner, tool_path, fake=fake, simulator=simulator, waves=True)
    (edam,) = fake.edams
    opts = edam["flow_options"]
    if simulator == "verilator":
        assert "--trace-fst" in opts["verilator_options"]
        assert opts["run_options"] == ["--trace"]
    else:
        assert edam["toplevel"] == "tiny_gpio chipgraph_dump"
        dump = Path(edam["files"][-1]["name"])
        assert '$dumpfile("dump.fst")' in dump.read_text()
        assert "$dumpvars(0, tiny_gpio)" in dump.read_text()


def test_edam_change_wipes_the_old_build(repo: Path, tool_path: str) -> None:
    runner = FakeRunner(results=PASS_XML.format(tb=_tb(repo)))
    _run(repo, runner, tool_path)
    work = runner.calls[-1].cwd
    (work / "Vtop").write_text("old model")
    _run(repo, FakeRunner(results=PASS_XML.format(tb=_tb(repo))), tool_path)
    assert (work / "Vtop").is_file()  # same EDAM: incremental
    _run(repo, FakeRunner(results=PASS_XML.format(tb=_tb(repo))), tool_path, parameters={"W": 1})
    assert not (work / "Vtop").exists()


def test_no_direct_subprocess(repo: Path, tool_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*a: object, **k: object) -> None:
        raise AssertionError("EdalizeTool must go through ctx.runner")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    runner = FakeRunner(results=PASS_XML.format(tb=_tb(repo)))
    assert _run(repo, runner, tool_path).status == "pass"


# --- args and work root --------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"top": None}, "args['top']"),
        ({"simulator": "vcs"}, "must be one of"),
        ({"test_module": None}, "test_module"),
        ({"test_module": "dv/nope.py"}, "not found"),
        ({"filelist": None}, "no HDL source"),
        ({"filelist": "filelists/missing.f"}, "filelist not found"),
        ({"files": ["rtl/missing.sv"]}, "not found"),
        ({"top": "tiny_{nope}"}, "cannot substitute"),
        ({"timeout_s": 0}, "timeout_s"),
        ({"seed": "x"}, "seed"),
        ({"work_root": "rtl/build"}, "source tree"),
    ],
)
def test_bad_args_are_errors(args: dict[str, Any], needle: str, repo: Path, tool_path: str) -> None:
    runner = FakeRunner(results=PASS_XML.format(tb=_tb(repo)))
    result = _run(repo, runner, tool_path, **args)
    assert result.status == "error"
    assert needle in result.issues[0].msg
    assert [s for s in runner.steps() if s != "probe"] == []


def test_tool_is_an_alias_of_simulator(repo: Path, tool_path: str) -> None:
    fake = FakeEdalize()
    _run(
        repo, FakeRunner(results=PASS_XML.format(tb=_tb(repo))), tool_path, fake=fake, tool="icarus"
    )
    assert fake.edams[0]["flow_options"]["tool"] == "icarus"


def test_temp_and_outside_work_roots(repo: Path, tool_path: str, tmp_path: Path) -> None:
    runner = FakeRunner(results=PASS_XML.format(tb=_tb(repo)))
    _run(repo, runner, tool_path, work_root="temp")
    work = runner.calls[-1].cwd
    assert work.is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert not work.is_relative_to(repo.resolve())

    outside = tmp_path / "simwork"
    runner = FakeRunner(results=PASS_XML.format(tb=_tb(repo)))
    _run(repo, runner, tool_path, work_root=str(outside))
    assert runner.calls[-1].cwd == outside.resolve()


# --- idempotency key -----------------------------------------------------------------


def _key(repo: Path, path: str, **args: Any) -> str:
    return EdalizeTool.key_for(_spec(**args), _ctx(repo, FakeRunner(), path))


def test_key_is_stable_and_ignores_work_root_and_timeout(repo: Path, tool_path: str) -> None:
    key = _key(repo, tool_path)
    assert key == _key(repo, tool_path)
    assert len(key) == 64
    assert key == _key(repo, tool_path, timeout_s=5, work_root="temp")


@pytest.mark.parametrize(
    "change",
    ["rtl", "test", "helper", "args", "simulator", "seed", "testcase", "waves", "tool"],
)
def test_key_changes_with_its_inputs(
    change: str, repo: Path, tool_path: str, tmp_path: Path
) -> None:
    before = _key(repo, tool_path)
    args: dict[str, Any] = {}
    path = tool_path
    if change == "rtl":
        (repo / "rtl" / "tiny_gpio.sv").write_text("module tiny_gpio; wire w; endmodule\n")
    elif change == "test":
        (repo / "dv" / "tb_gpio.py").write_text("# changed\n")
    elif change == "helper":
        (repo / "dv" / "helpers.py").write_text("# changed\n")
    elif change == "args":
        args["parameters"] = {"W": 2}
    elif change == "simulator":
        args["simulator"] = "icarus"
    elif change == "seed":
        args["seed"] = 1
    elif change == "testcase":
        args["testcase"] = "test_a"
    elif change == "waves":
        args["waves"] = True
    elif change == "tool":
        verilator = Path(tool_path) / "verilator"
        verilator.write_text("#!/bin/sh\necho 'Verilator 6'\n")
    assert _key(repo, path, **args) != before


def test_key_never_raises_on_bad_args(repo: Path, tool_path: str) -> None:
    assert len(_key(repo, tool_path, top=None)) == 64
    assert _key(repo, tool_path, top=None) != _key(repo, tool_path, top=None, seed=1)

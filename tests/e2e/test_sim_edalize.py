"""End-to-end: `EdalizeTool` runs real cocotb testbenches on Verilator and Icarus (M2-05).

Runs `examples/tinysoc/dv/tb_tiny_gpio.py` (the sample testbench) and the deliberately
broken modules under `fixtures/cocotb/` against `tiny_gpio`, through Edalize's `sim`
flow (configure only) and a real `LocalRunner`. Each case checks the `CheckResult` the
adapter returns: pass, an assertion failure, an exception in a test, an import error
(no `results.xml`: `error`), an RTL build error, and `waves=True` writing `dump.fst`.

Skipped, not failed, when the `sim` extra (edalize, cocotb), the simulator, `make` or
(for Verilator) a C++ compiler is missing; the `sim` CI job and the chipgraph-eda image
have them all and fail on a skip.
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
from pathlib import Path
from typing import Any

import pytest

from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.adapters.tool.edalize import EdalizeTool
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api.types import ToolContext

pytestmark = pytest.mark.e2e

_REPO = Path(__file__).resolve().parents[2]
_TINYSOC = _REPO / "examples" / "tinysoc"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cocotb"
_BROKEN = _FIXTURES / "gpio_tb_broken.py"
_IMPORT_ERROR = _FIXTURES / "gpio_tb_import_error.py"


def _missing(*names: str) -> list[str]:
    return [n for n in names if shutil.which(n) is None]


def _skip_reason(simulator: str) -> str | None:
    if importlib.util.find_spec("edalize") is None or importlib.util.find_spec("cocotb") is None:
        return "the `sim` extra is not installed (uv sync --extra sim)"
    needs = ("verilator", "make") if simulator == "verilator" else ("iverilog", "vvp", "make")
    missing = _missing(*needs)
    if simulator == "verilator" and all(_missing(c) for c in ("g++", "c++", "clang++")):
        missing.append("a C++ compiler")
    return f"{', '.join(missing)} not found on PATH" if missing else None


SIMULATORS = [
    pytest.param(
        sim,
        marks=pytest.mark.skipif(_skip_reason(sim) is not None, reason=str(_skip_reason(sim))),
        id=sim,
    )
    for sim in ("verilator", "icarus")
]


@pytest.fixture
def tinysoc(tmp_path: Path) -> Path:
    """A copy of tinysoc, with the broken test modules in its `dv/` next to the sample."""
    root = tmp_path / "tinysoc"
    shutil.copytree(_TINYSOC, root)
    for module in (_BROKEN, _IMPORT_ERROR):
        shutil.copy2(module, root / "dv" / module.name)
    return root


def _line_of(path: Path, marker: str) -> int:
    lines = path.read_text().splitlines()
    return next(i for i, line in enumerate(lines, start=1) if marker in line)


def _sim(root: Path, simulator: str, work: Path, **args: Any) -> CheckResult:
    spec_args: dict[str, Any] = {
        "simulator": simulator,
        "top": "tiny_{block}",
        "filelist": "filelists/{block}.f",
        "test_module": "dv/tb_tiny_gpio.py",
        "work_root": str(work),
        "timeout_s": 300,
    }
    spec_args.update(args)
    spec = CheckSpec(id="sim", capability="sim", adapter="edalize", args=spec_args)
    ctx = ToolContext(repo_root=root, runner=LocalRunner(), params={"block": "gpio"})
    return asyncio.run(EdalizeTool().run(spec, ctx))


@pytest.mark.parametrize("simulator", SIMULATORS)
def test_sample_testbench_passes(simulator: str, tinysoc: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    result = _sim(tinysoc, simulator, work)
    assert result.status == "pass", result.log_tail
    assert result.issues == ()
    assert "tests=3 pass=3 fail=0" in result.log_tail
    assert (work / "results.xml").read_text().count("<testcase") == 3
    assert not (work / "dump.fst").exists()


@pytest.mark.parametrize("simulator", SIMULATORS)
def test_assertion_failure(simulator: str, tinysoc: Path, tmp_path: Path) -> None:
    result = _sim(
        tinysoc,
        simulator,
        tmp_path / "work",
        test_module="dv/gpio_tb_broken.py",
        testcase=["test_passes", "test_wrong_readback"],
    )
    assert result.status == "fail", result.log_tail
    (issue,) = result.issues
    assert issue.rule == "cocotb/AssertionError"
    assert (issue.file, issue.line) == ("dv/gpio_tb_broken.py", _line_of(_BROKEN, "CG_ASSERT_LINE"))
    assert "DATA_OUT readback: got 0xa5, expected 0x5a" in issue.msg
    assert "tests=2 pass=1 fail=1" in result.log_tail


@pytest.mark.parametrize("simulator", SIMULATORS)
def test_exception_in_a_test(simulator: str, tinysoc: Path, tmp_path: Path) -> None:
    result = _sim(
        tinysoc,
        simulator,
        tmp_path / "work",
        test_module="dv/gpio_tb_broken.py",
        testcase="test_missing_signal",
    )
    assert result.status == "fail", result.log_tail
    (issue,) = result.issues
    assert issue.rule == "cocotb/AttributeError"
    assert (issue.file, issue.line) == ("dv/gpio_tb_broken.py", _line_of(_BROKEN, "CG_CRASH_LINE"))


@pytest.mark.parametrize("simulator", SIMULATORS)
def test_import_error_is_an_error_not_a_pass(simulator: str, tinysoc: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    result = _sim(tinysoc, simulator, work, test_module="dv/gpio_tb_import_error.py")
    assert result.status == "error", result.log_tail
    (issue,) = result.issues
    assert issue.rule == "cocotb/no_results"
    assert "ImportError: gpio_tb_import_error" in issue.msg
    assert not (work / "results.xml").exists()


@pytest.mark.parametrize("simulator", SIMULATORS)
def test_rtl_build_error(simulator: str, tinysoc: Path, tmp_path: Path) -> None:
    (tinysoc / "rtl" / "broken.sv").write_text(
        "module broken (input logic a);\n  assign b = ;\nendmodule\n"
    )
    result = _sim(tinysoc, simulator, tmp_path / "work", files=["rtl/broken.sv"])
    assert result.status == "error", result.log_tail
    locations = {(i.rule, i.file, i.line) for i in result.issues if i.severity == "error"}
    assert (f"{simulator}/error", "rtl/broken.sv", 2) in locations


@pytest.mark.parametrize("simulator", SIMULATORS)
def test_waves_write_dump_fst(simulator: str, tinysoc: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    result = _sim(tinysoc, simulator, work, waves=True)
    assert result.status == "pass", result.log_tail
    dump = work / "dump.fst"
    assert dump.is_file() and dump.stat().st_size > 0


@pytest.mark.parametrize("simulator", SIMULATORS)
def test_test_module_outside_the_repo(simulator: str, tinysoc: Path, tmp_path: Path) -> None:
    """A failure in a test file outside the project has no `file` (findings need a
    repo-relative one); its location is in the message."""
    result = _sim(
        tinysoc,
        simulator,
        tmp_path / "work",
        test_module=str(_BROKEN),
        testcase="test_missing_signal",
    )
    assert result.status == "fail", result.log_tail
    (issue,) = result.issues
    assert (issue.file, issue.line) == (None, None)
    assert f"(at {_BROKEN}:{_line_of(_BROKEN, 'CG_CRASH_LINE')})" in issue.msg


@pytest.mark.parametrize("simulator", SIMULATORS)
def test_default_work_root_is_run_state(simulator: str, tinysoc: Path) -> None:
    result = _sim(tinysoc, simulator, Path("unused"), work_root="state")
    assert result.status == "pass", result.log_tail
    work = tinysoc / ".chipgraph" / "state" / "sim" / "sim" / "block=gpio" / simulator
    assert (work / "results.xml").is_file()

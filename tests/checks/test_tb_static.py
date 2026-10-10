"""M2-06: `tb_static`, a cocotb test checked without a simulator and without the RTL.

One test per rule (on the test file only, `file:line`), then the check as the engine runs
it on a tinysoc copy (interface from the spec, or from the declaration when no model was
built; a runner that refuses every command proves no simulator ran), and the optional
`sim` stage.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from conftest import FakeRunner

from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.checks.tb_static import TbStaticCheck, static_issues
from chipgraph.core.contracts import CheckSpec, Issue
from chipgraph.core.model import BlockEntity, DesignModel, RequirementEntity
from chipgraph.core.plugin_api.registry import Registry
from chipgraph.core.plugin_api.types import ToolContext
from chipgraph.packs.dv.interface import Interface, InterfacePort

REPO = Path(__file__).resolve().parents[2]
TB = "dv/gpio/test_gpio.py"


def _fixture() -> ModuleType:
    name = "tb_acceptance_fixture"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, REPO / "docs" / "tb-claude-code" / "fixture.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


IFACE = Interface(
    module="tiny_gpio",
    block="gpio",
    source="spec",
    ports=tuple(
        InterfacePort(name=n, source="spec") for n in ("clk", "rst_n", "addr", "rdata", "pin_in")
    ),
    clock="clk",
    reset="rst_n",
)
MODEL = DesignModel.build(
    [
        BlockEntity(key="block:gpio", name="gpio"),
        RequirementEntity(
            key="requirement:REQ-GPIO-001", name="REQ-GPIO-001", attrs={"block": "block:gpio"}
        ),
        RequirementEntity(
            key="requirement:REQ-GPIO-002", name="REQ-GPIO-002", attrs={"block": "block:gpio"}
        ),
        RequirementEntity(
            key="requirement:REQ-TIM-001", name="REQ-TIM-001", attrs={"block": "block:timer"}
        ),
    ],
    [],
)

GOOD = '''\
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge


@cocotb.test()
async def test_read(dut):
    """REQ-GPIO-001 and REQ-GPIO-002: readback."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    rng = random.Random(1)
    dut.rst_n.value = 0
    dut.pin_in.value = rng.randrange(256)
    dut._log.info("reset")
    await RisingEdge(dut.clk)
    assert dut.rdata.value.to_unsigned() == 0, "REQ-GPIO-002: reads 0 after reset"
'''


def _issues(root: Path, text: str, **kw: object) -> list[Issue]:
    (root / TB).parent.mkdir(parents=True, exist_ok=True)
    (root / TB).write_text(text)
    args: dict[str, object] = {"interface": IFACE, "model": MODEL, "block": "gpio"}
    args.update(kw)
    return static_issues(root, TB, **args)  # type: ignore[arg-type]


def _rules(issues: list[Issue], severity: str = "error") -> list[str]:
    return [i.rule for i in issues if i.severity == severity]


def test_a_good_test_has_no_issue(tmp_path: Path) -> None:
    assert _issues(tmp_path, GOOD) == []


def test_missing_file(tmp_path: Path) -> None:
    [issue] = static_issues(tmp_path, TB, interface=IFACE, model=MODEL, block="gpio")
    assert (issue.rule, issue.file) == ("tb_static.missing", TB)


def test_syntax_error_is_at_its_line(tmp_path: Path) -> None:
    [issue] = _issues(tmp_path, "import cocotb\n\nasync def broken(:\n")
    assert (issue.rule, issue.file, issue.line) == ("tb_static.syntax", TB, 3)


def test_no_cocotb_and_no_test(tmp_path: Path) -> None:
    issues = _issues(
        tmp_path, "def test_x(dut):\n    # verifies: REQ-GPIO-001, REQ-GPIO-002\n    pass\n"
    )
    assert _rules(issues) == ["tb_static.no_cocotb", "tb_static.no_test"]


def test_a_cocotb_test_must_be_async(tmp_path: Path) -> None:
    text = GOOD + "\n\n@cocotb.test\ndef test_sync(dut):\n    pass\n"
    [issue] = _issues(tmp_path, text)
    assert issue.rule == "tb_static.not_async" and issue.line == text.count("\n") - 1


def test_unknown_ports_and_internals_are_errors_with_their_line(tmp_path: Path) -> None:
    text = GOOD.replace(
        '    dut._log.info("reset")\n',
        '    dut._log.info("reset")\n'
        "    dut.u_core.state_q.value = 1\n"
        '    x = getattr(dut, "secret_q")\n'
        "    y = getattr(dut, name)\n"
        '    z = dut._id("hidden", extended=False)\n',
    )
    issues = _issues(tmp_path, text)
    got = [(i.rule, i.line) for i in issues if i.severity == "error"]
    line = text.splitlines().index("    dut.u_core.state_q.value = 1") + 1
    assert got == [
        ("tb_static.unknown_port", line),
        ("tb_static.unknown_port", line + 1),
        ("tb_static.dynamic_access", line + 2),
        ("tb_static.dynamic_access", line + 3),
    ]
    assert "u_core" in issues[0].msg and "addr, clk, pin_in, rdata, rst_n" in issues[0].msg


def test_the_test_s_own_dut_name_is_checked(tmp_path: Path) -> None:
    text = GOOD.replace("async def test_read(dut):", "async def test_read(top):").replace(
        "dut.", "top."
    )
    assert _issues(tmp_path, text) == []
    issues = _issues(tmp_path, text + "    top.nope.value = 1\n")
    assert _rules(issues) == ["tb_static.unknown_port"]


def test_allow_names_a_clock_reset_convention(tmp_path: Path) -> None:
    text = GOOD + "    dut.clk_i.value = 0\n"
    assert _rules(_issues(tmp_path, text)) == ["tb_static.unknown_port"]
    assert _issues(tmp_path, text, allow=frozenset({"clk_i"})) == []


def test_no_interface_skips_the_port_check_with_a_warning(tmp_path: Path) -> None:
    issues = _issues(tmp_path, GOOD + "    dut.anything.value = 1\n", interface=None)
    assert _rules(issues) == [] and _rules(issues, "warning") == ["tb_static.no_interface"]


@pytest.mark.parametrize(
    "line",
    [
        "    text = open('notes.txt').read()",
        "    import subprocess",
        "    from pathlib import Path",
        "    import glob",
        "    import os; os.open('x', 0)",
        "    import os; os.system('ls')",
        "    import os; os.listdir('.')",
        "    from os import popen",
        "    data = dut.path_obj.read_text()",
        "    eval('1')",
        "    exec('x = 1')",
        "    __import__('shutil')",
        "    src = 'rtl/tiny_gpio.sv'",
        "    src = '../design/top.v'",
        "    src = 'x/RTL/a.txt'",
    ],
)
def test_anything_that_could_read_the_rtl_is_forbidden(tmp_path: Path, line: str) -> None:
    issues = _issues(tmp_path, GOOD + line + "\n")
    forbidden = [i for i in issues if i.rule == "tb_static.forbidden"]
    assert forbidden, issues
    assert all(i.file == TB and i.line == GOOD.count("\n") + 1 for i in forbidden)


def test_os_environ_is_allowed(tmp_path: Path) -> None:
    assert _issues(tmp_path, "import os\n" + GOOD + "    os.environ.get('SEED')\n") == []


def test_a_requirement_not_cited_is_a_warning(tmp_path: Path) -> None:
    text = GOOD.replace("REQ-GPIO-001 and REQ-GPIO-002: readback.", "Readback.")
    issues = _issues(tmp_path, text.replace('"REQ-GPIO-002: reads', '"reads'))
    assert _rules(issues) == []
    assert sorted(i.msg.split()[1] for i in issues if i.rule == "tb_static.req_missing") == [
        "REQ-GPIO-001",
        "REQ-GPIO-002",
    ]
    assert {i.severity for i in issues} == {"warning"}


def test_a_verifies_comment_cites(tmp_path: Path) -> None:
    text = GOOD.replace('    """REQ-GPIO-001 and REQ-GPIO-002: readback."""\n', "").replace(
        "async def test_read(dut):\n",
        "async def test_read(dut):\n    # verifies: REQ-GPIO-001, REQ-GPIO-002\n",
    )
    assert _issues(tmp_path, text) == []


def test_a_cited_id_not_in_the_model_is_an_error(tmp_path: Path) -> None:
    text = GOOD.replace(
        "async def test_read(dut):\n",
        "async def test_read(dut):\n    # verifies: REQ-GPIO-009\n",
    )
    [issue] = _issues(tmp_path, text)
    assert (issue.rule, issue.severity, issue.line) == ("tb_static.req_unknown", "error", 10)
    # another block's id exists in the model: cited, not unknown
    assert _issues(tmp_path, text.replace("REQ-GPIO-009", "REQ-TIM-001")) == []


def test_without_a_model_requirements_are_not_checked(tmp_path: Path) -> None:
    issues = _issues(tmp_path, GOOD, model=None)
    assert _rules(issues) == [] and _rules(issues, "warning") == ["tb_static.req_unchecked"]


def test_the_entry_point_is_registered() -> None:
    registry = Registry()
    registry.discover()
    assert isinstance(registry.get("check", "tb_static"), TbStaticCheck)


# --- the check on a project ---------------------------------------------------------------


def _run(root: Path, runner: object, **args: object) -> object:
    spec = CheckSpec(id="tb_static", capability="tb_static", adapter="tb_static", args=args)
    ctx = ToolContext(repo_root=root, runner=runner, params={"block": "gpio"})  # type: ignore[arg-type]
    return asyncio.run(TbStaticCheck().run(spec, ctx))


def _gpio_test() -> str:
    return (
        GOOD.replace("REQ-GPIO-001 and REQ-GPIO-002", "REQ-GPIO-001, REQ-GPIO-002, REQ-GPIO-003")
        + "    dut.pin_dir.value\n"
    )


def test_on_tinysoc_with_the_model(tmp_path: Path, runner: FakeRunner) -> None:
    root = _fixture().make_tb_project(tmp_path / "t")
    (root / TB).parent.mkdir(parents=True)
    (root / TB).write_text(_gpio_test())
    res = _run(root, runner, top="tiny_{block}")
    assert res.status == "pass", res.issues  # type: ignore[attr-defined]
    assert "interface tiny_gpio from spec" in res.log_tail  # type: ignore[attr-defined]
    (root / TB).write_text(_gpio_test() + "    dut.out_q.value\n")
    res = _run(root, runner, top="tiny_{block}")
    assert res.status == "fail"  # type: ignore[attr-defined]
    [issue] = res.issues  # type: ignore[attr-defined]
    assert (issue.rule, issue.file) == ("tb_static.unknown_port", TB)


def test_on_tinysoc_without_a_model_the_declaration_gives_the_ports(
    tmp_path: Path, runner: FakeRunner
) -> None:
    root = _fixture().make_tb_project(tmp_path / "t", ingest=False)
    (root / TB).parent.mkdir(parents=True)
    (root / TB).write_text(_gpio_test())
    res = _run(root, runner, top="tiny_{block}")
    assert res.status == "pass", res.issues  # type: ignore[attr-defined]
    assert "from rtl_declaration" in res.log_tail  # type: ignore[attr-defined]
    assert [i.rule for i in res.issues] == ["tb_static.req_unchecked"]  # type: ignore[attr-defined]


def test_bad_args_are_an_error(tmp_path: Path, runner: FakeRunner) -> None:
    assert _run(tmp_path, runner, sim="edalize").status == "error"  # type: ignore[attr-defined]
    assert _run(tmp_path, runner, test="dv/{nope}.py").status == "error"  # type: ignore[attr-defined]


def test_the_sim_stage_runs_only_after_the_static_rules_pass(tmp_path: Path) -> None:
    root = _fixture().make_tb_project(tmp_path / "t")
    (root / TB).parent.mkdir(parents=True)
    (root / TB).write_text(_gpio_test())
    sim = {
        "use": "cmd",
        "cmd": ["python3", "-c", "print('tests=1 pass=0 fail=1'); raise SystemExit(1)"],
    }
    res = _run(root, LocalRunner(), top="tiny_{block}", sim=sim)
    assert res.status == "fail" and "tests=1 pass=0 fail=1" in res.log_tail  # type: ignore[attr-defined]
    (root / TB).write_text(_gpio_test() + "    open('x')\n")
    res = _run(root, FakeRunner(), top="tiny_{block}", sim=sim)  # never reaches the runner
    assert res.status == "fail"  # type: ignore[attr-defined]
    assert [i.rule for i in res.issues] == ["tb_static.forbidden"]  # type: ignore[attr-defined]
    (root / TB).write_text(_gpio_test())
    res = _run(root, FakeRunner(), top="tiny_{block}", sim={"use": "no-such-adapter"})
    assert res.status == "error"  # type: ignore[attr-defined]
    assert "no-such-adapter" in res.issues[-1].msg  # type: ignore[attr-defined]

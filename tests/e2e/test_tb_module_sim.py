"""M2-06 end to end with a real simulator: `dv/tb_module` on tinysoc, `tb_static` with its
`sim` stage (the `edalize` adapter on Verilator), the test played by this test.

A test that follows the spec is accepted against the real RTL; a wrong expectation is
rejected with the assertion's text; an RTL whose body does not build (with markers in
it) gives a redo text with no RTL line, path or marker, and says something was withheld.

Skipped, not failed, when the `sim` extra, Verilator, `make` or a C++ compiler is missing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from chipgraph.adapters.runtime.claude_code import service
from chipgraph.app.context import AppContext
from chipgraph.core.engine.agent_rule import WITHHELD_NOTE

REPO = Path(__file__).resolve().parents[2]


def _missing() -> str | None:
    if importlib.util.find_spec("edalize") is None or importlib.util.find_spec("cocotb") is None:
        return "the `sim` extra is not installed (uv sync --extra sim)"
    tools = [t for t in ("verilator", "make") if shutil.which(t) is None]
    if all(shutil.which(c) is None for c in ("g++", "c++", "clang++")):
        tools.append("a C++ compiler")
    return f"{', '.join(tools)} not found on PATH" if tools else None


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(_missing() is not None, reason=str(_missing())),
]


def _load(name: str, path: Path) -> ModuleType:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


fx = _load("tb_acceptance_fixture", REPO / "docs" / "tb-claude-code" / "fixture.py")
helpers = _load(
    "tb_author_helpers", REPO / "tests" / "adapters" / "runtime" / "tb_author_helpers.py"
)
GPIO = fx.TASKS["gpio"]
OUT = fx.OUTPUTS["gpio"]
MARKERS = ("ZQXSIMCOMMENT", "zqx_sim_signal")


def _ctx(root: Path) -> AppContext:
    return AppContext.load(root)


def _try(root: Path, test_text: str) -> dict:
    asyncio.run(service.next_task(_ctx(root), fx.RULE))
    asyncio.run(service.get_context(_ctx(root), GPIO))
    (root / OUT).parent.mkdir(parents=True, exist_ok=True)
    (root / OUT).write_text(test_text)
    return asyncio.run(service.submit(_ctx(root), GPIO, service.SubmitReport()))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return fx.make_tb_project(tmp_path / "tinysoc", sim=True, blocks=fx.BLOCKS)


def test_a_test_that_follows_the_spec_is_accepted(project: Path) -> None:
    answer = _try(project, helpers.GOOD_GPIO_TEST)
    assert answer["accepted"] is True, (answer["reasons"], answer["checks"])


def test_a_wrong_expectation_is_rejected_with_its_message(project: Path) -> None:
    bad = helpers.GOOD_GPIO_TEST.replace("got == 0x3C,", "got == 0x3D,")
    answer = _try(project, bad)
    assert answer["status"] == "rejected" and answer["label"] == "verification"
    assert "REQ-GPIO-002: DATA_IN expected 0x3c, got 0x3c" in answer["redo"]
    assert WITHHELD_NOTE not in answer["redo"]


def test_a_design_that_does_not_build_is_withheld(project: Path) -> None:
    rtl = project / "rtl" / "tiny_gpio.sv"
    text = rtl.read_text().replace(
        "  assign pin_out = out_q;",
        f"  logic {MARKERS[1]};\n  assign {MARKERS[1]} = ;  // {MARKERS[0]}\n"
        "  assign pin_out = out_q;",
    )
    rtl.write_text(text)
    subprocess.run(["git", "commit", "-qam", "broken"], cwd=project, check=True)
    answer = _try(project, helpers.GOOD_GPIO_TEST)
    assert answer["status"] == "rejected"
    dumped = json.dumps(answer)
    for leak in (*MARKERS, "tiny_gpio.sv", "assign"):
        assert leak not in dumped, leak
    assert WITHHELD_NOTE in answer["redo"]
    journal = "".join(p.read_text() for p in (project / ".chipgraph").rglob("journal.jsonl"))
    assert MARKERS[1] in journal  # a person still sees the full build log

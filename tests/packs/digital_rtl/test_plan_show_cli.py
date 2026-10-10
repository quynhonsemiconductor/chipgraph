"""M2-03: `chipgraph plan show <block>`, what the approver of `plan:<block>` reads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from plan_helpers import approve_plan, fixture_plan, overlapping, tinysoc_project, write_plan
from typer.testing import CliRunner

from chipgraph.cli import app

runner = CliRunner()


@pytest.fixture(scope="module")
def tiny(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tinysoc_project(tmp_path_factory.mktemp("plan-show") / "tinysoc")
    write_plan(root, "gpio")
    return root


def _show(root: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["-C", str(root), *args])
    return result.exit_code, result.output


def test_plan_show_prints_what_the_approver_needs(tiny: Path) -> None:
    code, out = _show(tiny, "plan", "show", "gpio")
    assert code == 0, out
    assert "plan for block gpio: plan/gpio.plan.yml" in out
    assert "gate plan:gpio: waiting" in out
    assert "check: ok" in out
    assert "top: tiny_gpio   modules 2/12, depth 2/4, tries 5/36" in out
    # Modules in build order, with REQs, dependencies, write sets and ports.
    assert out.index("  tiny_gpio_regs:") < out.index("  tiny_gpio:")
    assert "    depends on: tiny_gpio_regs" in out
    assert "    writes:     rtl/tiny_gpio.sv (replaces)" in out
    assert "pin_in:input[8]" in out
    # REQ coverage, assumptions, open questions (also a check warning).
    assert "  REQ-GPIO-002: tiny_gpio" in out
    assert "  - pin_in is already synchronous to clk" in out
    assert "open questions (read before approving):" in out
    assert "  - Should DATA_IN go through a two-flop synchroniser" in out
    assert "warning plan/gpio.plan.yml" in out
    assert (
        "approve: chipgraph approve plan:gpio --instance 'digital-rtl/plan_expand[block=gpio]'"
    ) in out


def test_plan_show_json(tiny: Path) -> None:
    code, out = _show(tiny, "--json", "plan", "show", "gpio")
    assert code == 0, out
    data = json.loads(out)
    assert data["gate"] == {
        "id": "plan:gpio",
        "status": "waiting",
        "instance": "digital-rtl/plan_expand[block=gpio]",
        "approve": ("chipgraph approve plan:gpio --instance 'digital-rtl/plan_expand[block=gpio]'"),
    }
    assert data["order"] == ["tiny_gpio_regs", "tiny_gpio"]
    assert data["coverage"][0] == {
        "req": "REQ-GPIO-001",
        "modules": ["tiny_gpio_regs"],
        "unassigned": None,
    }
    assert data["limits"] == {"modules": [2, 12], "depth": [2, 4], "tries": [5, 36]}
    assert len(data["warnings"]) == 1 and data["errors"] == []


def test_plan_show_after_approval(tiny: Path) -> None:
    write_plan(tiny, "timer")
    approve_plan(tiny, "timer")
    code, out = _show(tiny, "plan", "show", "timer")
    assert code == 0, out
    assert "gate plan:timer: approved" in out
    assert "approve:" not in out


def test_plan_show_with_a_failing_plan_exits_1(tiny: Path) -> None:
    data = fixture_plan("timer")
    data["modules"][2]["reqs"] = []
    write_plan(tiny, "timer", overlapping(data))
    try:
        code, out = _show(tiny, "plan", "show", "timer")
    finally:
        write_plan(tiny, "timer")
    assert code == 1
    assert "check: FAILS" in out
    assert "[plan.write_overlap]" in out
    assert "  REQ-TIM-005: NOT COVERED" in out


def test_plan_show_without_a_plan_exits_1(tiny: Path) -> None:
    code, out = _show(tiny, "plan", "show", "top")
    assert code == 1
    assert "no plan yet" in out


def test_plan_show_unknown_block_is_a_usage_error(tiny: Path) -> None:
    code, out = _show(tiny, "plan", "show", "uart")
    assert code == 2
    assert "unknown block 'uart'" in out


def test_chipgraph_approve_opens_the_plan_gate(tiny: Path) -> None:
    # Last in this module: it approves gpio, which the tests above read as waiting.
    instance = "digital-rtl/plan_expand[block=gpio]"
    code, out = _show(tiny, "approve", "plan:gpio", "--instance", instance, "--by", "lead")
    assert code == 0, out
    assert "approve recorded for gate 'plan:gpio' by 'lead'" in out
    code, out = _show(tiny, "plan", "show", "gpio")
    assert code == 0 and "gate plan:gpio: approved" in out

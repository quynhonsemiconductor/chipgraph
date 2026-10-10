"""M2-03: every rejection of `plan_check`, on the pure `validate_plan` and on tinysoc.

The pure tests check a plan against a hand-built `PlanEnv` (the timer block's slice of
tinysoc's Design Model); the tinysoc tests go through the real check plugin.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest
from plan_helpers import fixture_plan, overlapping, tinysoc_project, write_plan

from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.checks._naming_rules import KindRule, NamingRules
from chipgraph.checks.naming import _CompiledRules
from chipgraph.checks.plan_check import (
    PlanCheck,
    PlanEnv,
    PortInfo,
    ReqInfo,
    check_plan_file,
    dependency_depth,
    parse_plan,
    validate_plan,
)
from chipgraph.core.config.models import PlanCfg
from chipgraph.core.contracts import CheckSpec
from chipgraph.core.plugin_api.types import ToolContext
from chipgraph.packs.digital_rtl.plan.model import Plan

PATH = "plan/timer.plan.yml"

_PORTS = (
    PortInfo("clk", "input", 1),
    PortInfo("rst_n", "input", 1),
    PortInfo("addr", "input", 2),
    PortInfo("wr_en", "input", 1),
    PortInfo("wdata", "input", 32),
    PortInfo("rdata", "output", 32),
    PortInfo("irq", "output", 1),
)
_REQS = tuple(
    ReqInfo(f"requirement:REQ-TIM-00{i}", f"REQ-TIM-00{i}", f"text {i}", "timer")
    for i in range(1, 6)
)


def _env(**changes: Any) -> PlanEnv:
    base: dict[str, Any] = {
        "block": "timer",
        "requirements": _REQS,
        "other_requirements": (
            ReqInfo("requirement:REQ-GPIO-001", "REQ-GPIO-001", "gpio", "gpio"),
        ),
        "interface": _PORTS,
        "interface_source": "spec",
        "layout": ("rtl/tiny_timer*.sv", "filelists/timer.f"),
        "engine_outputs": {"filelists/timer.f": "demo/filelist[block=timer]"},
        "agent_outputs": {"reports/review/timer.json": "digital-rtl/review[block=timer]"},
        "existing": {"rtl/tiny_timer.sv": "a" * 64},
    }
    base.update(changes)
    return PlanEnv(**base)


def _plan(data: dict[str, Any] | None = None) -> Plan:
    return Plan.model_validate(data if data is not None else fixture_plan("timer"))


def _rules(issues: list[Any]) -> list[str]:
    return [i.rule for i in issues if i.severity == "error"]


def _check(data: dict[str, Any], **env: Any) -> list[Any]:
    return validate_plan(_plan(data), _env(**env), plan_path=PATH)


def test_the_fixture_plan_passes_the_pure_check() -> None:
    assert validate_plan(_plan(), _env(), plan_path=PATH) == []


def test_overlapping_writes_are_rejected() -> None:
    issues = _check(overlapping(fixture_plan("timer")))
    assert _rules(issues) == ["plan.write_overlap"]
    assert issues[0].msg == (
        "modules 'tiny_timer_counter' and 'tiny_timer_irq' both write "
        "rtl/tiny_timer_counter.sv; the write sets of modules must not overlap (F2)"
    )
    assert issues[0].file == PATH


def test_a_write_outside_the_layout_is_rejected() -> None:
    data = fixture_plan("timer")
    data["modules"][0]["writes"] = [{"path": "design/elsewhere/counter.sv"}]
    issues = _check(data)
    assert _rules(issues) == ["plan.write_layout"]
    assert "outside block 'timer''s layout paths (rtl/tiny_timer*.sv" in issues[0].msg


def test_a_write_to_a_file_another_rule_produces_is_rejected() -> None:
    data = fixture_plan("timer")
    data["modules"][0]["writes"].append({"path": "filelists/timer.f"})
    assert _rules(_check(data)) == ["plan.write_engine"]
    data = fixture_plan("timer")
    data["modules"][0]["writes"].append({"path": "reports/review/timer.json"})
    env = {"layout": ("rtl/tiny_timer*.sv", "reports/review/{block}.json")}
    assert "plan.write_taken" in _rules(_check(data, **env))


def test_overwriting_an_existing_file_needs_replaces() -> None:
    data = fixture_plan("timer")
    data["modules"][2]["writes"] = [{"path": "rtl/tiny_timer.sv"}]  # replaces dropped
    issues = _check(data)
    assert _rules(issues) == ["plan.write_existing"]
    assert "set `replaces: true`" in issues[0].msg
    # A file this block's plan nodes wrote (its hash is on record) is the plan's own.
    assert _check(data, built={"rtl/tiny_timer.sv": frozenset({"a" * 64})}) == []
    # After approval the rule is not applied (the files exist by design).
    assert validate_plan(_plan(data), _env(), plan_path=PATH, check_existing=False) == []


def test_a_module_must_write_what_the_plan_module_rules_write_for_it() -> None:
    env = {"module_outputs": (("stub/module_rtl", "rtl/{module}.sv"),)}
    assert _check(fixture_plan("timer"), **env) == []
    data = fixture_plan("timer")
    data["modules"][0]["writes"] = [{"path": "rtl/tiny_timer_cnt.sv"}]
    issues = _check(data, **env)
    assert _rules(issues) == ["plan.write_missing"]
    assert "rule 'stub/module_rtl' writes rtl/tiny_timer_counter.sv for it" in issues[0].msg


@pytest.mark.parametrize(
    ("depends_on", "rule"),
    [
        (["tiny_timer_nope"], "plan.unknown_dependency"),
        (["tiny_timer_counter"], "plan.self_dependency"),
    ],
)
def test_unknown_and_self_dependencies_are_rejected(depends_on: list[str], rule: str) -> None:
    data = fixture_plan("timer")
    data["modules"][0]["depends_on"] = depends_on
    assert _rules(_check(data)) == [rule]


def test_a_dependency_cycle_is_rejected() -> None:
    data = fixture_plan("timer")
    data["modules"][0]["depends_on"] = ["tiny_timer"]  # tiny_timer depends on it already
    issues = _check(data)
    assert _rules(issues) == ["plan.dependency_cycle"]
    assert "tiny_timer_counter -> tiny_timer -> tiny_timer_counter" in issues[0].msg or (
        "tiny_timer -> tiny_timer_counter -> tiny_timer" in issues[0].msg
    )


def test_an_unknown_req_is_rejected() -> None:
    data = fixture_plan("timer")
    data["modules"][0]["reqs"].append("REQ-TIM-099")
    issues = _check(data)
    assert _rules(issues) == ["plan.unknown_req"]
    assert "not a requirement in the Design Model" in issues[0].msg
    data = fixture_plan("timer")
    data["modules"][0]["reqs"].append("REQ-GPIO-001")
    assert "of block 'gpio'" in _check(data)[0].msg


def test_an_unassigned_req_is_rejected_unless_listed_with_a_reason() -> None:
    data = fixture_plan("timer")
    data["modules"][2]["reqs"] = []  # REQ-TIM-005 no longer covered
    issues = _check(data)
    assert _rules(issues) == ["plan.unassigned_req"]
    assert "REQ-TIM-005" in issues[0].msg
    data["unassigned_reqs"] = [{"req": "REQ-TIM-005", "reason": "the bus wrapper does it"}]
    assert _check(data) == []
    # The inferred-key spelling of a requirement works too.
    data["unassigned_reqs"] = [{"req": "requirement:REQ-TIM-005", "reason": "later"}]
    assert _check(data) == []


def test_an_inferred_requirement_is_named_by_its_key() -> None:
    inferred = ReqInfo(
        "requirement:timer.h1a2b3c4d", "Tick rate for each row of 7.1", "...", "timer", False
    )
    env = {"requirements": (*_REQS, inferred)}
    data = fixture_plan("timer")
    issues = _check(data, **env)
    assert _rules(issues) == ["plan.unassigned_req"]
    assert "requirement timer.h1a2b3c4d is neither assigned" in issues[0].msg
    data["modules"][0]["reqs"].append("timer.h1a2b3c4d")
    assert _check(data, **env) == []
    data["modules"][0]["reqs"][-1] = "Tick rate for each row of 7.1"  # the text is no id
    assert sorted(_rules(_check(data, **env))) == ["plan.unassigned_req", "plan.unknown_req"]


def test_a_port_not_on_the_block_interface_is_rejected() -> None:
    data = fixture_plan("timer")
    data["modules"][2]["interface"]["ports"].append(
        {"name": "prescale", "direction": "input", "width": 8}
    )
    issues = _check(data)
    assert _rules(issues) == ["plan.unknown_port"]
    assert "not on block 'timer''s interface" in issues[0].msg


def test_a_port_that_differs_from_the_model_is_rejected() -> None:
    data = fixture_plan("timer")
    data["modules"][2]["interface"]["ports"][2] = {"name": "addr", "direction": "input", "width": 4}
    assert _rules(_check(data)) == ["plan.port_mismatch"]


def test_internal_ports_are_not_allowed_on_the_top() -> None:
    data = fixture_plan("timer")
    data["modules"][2]["interface"]["ports"].append(
        {"name": "dbg", "direction": "output", "width": 1, "internal": True}
    )
    assert _rules(_check(data)) == ["plan.internal_port_on_top"]


def test_the_top_must_be_a_module_and_the_block_must_match() -> None:
    data = fixture_plan("timer")
    data["top"] = "tiny_timer_wrap"
    assert _rules(_check(data)) == ["plan.top"]
    data = fixture_plan("timer")
    data["block"] = "gpio"
    assert _rules(_check(data)) == ["plan.block"]
    data = fixture_plan("timer")
    data["modules"].append(copy.deepcopy(data["modules"][0]))
    assert "plan.duplicate_module" in _rules(_check(data))


def test_the_module_limit() -> None:
    issues = _check(fixture_plan("timer"), limits=PlanCfg(max_modules=2))
    assert _rules(issues) == ["plan.limit.modules"]
    assert "3 modules; the limit is 2" in issues[0].msg


def test_the_depth_limit() -> None:
    assert dependency_depth(_plan()) == 2
    issues = _check(fixture_plan("timer"), limits=PlanCfg(max_depth=1))
    assert _rules(issues) == ["plan.limit.depth"]


def test_the_total_tries_limit() -> None:
    issues = _check(fixture_plan("timer"), limits=PlanCfg(max_total_tries=8))
    assert _rules(issues) == ["plan.limit.tries"]
    assert "add up to 9 tries; the limit is 8" in issues[0].msg


def test_module_names_follow_the_naming_rule_when_one_is_set() -> None:
    rules = NamingRules(
        document="doc",
        version="1",
        identifiers={
            "module": KindRule(rule="2.1 module", pattern="^m_[a-z_]+$", message="must be m_*")
        },
    )
    issues = _check(fixture_plan("timer"), naming=_CompiledRules(rules))
    assert set(_rules(issues)) == {"plan.naming"}
    assert len(issues) == 3
    assert "must be m_* (2.1 module)" in issues[0].msg
    assert _check(fixture_plan("timer"), naming=None) == []


def test_open_questions_are_a_warning_not_an_error() -> None:
    data = fixture_plan("timer")
    data["open_questions"] = ["Does COUNT wrap?"]
    issues = _check(data)
    assert [(i.rule, i.severity) for i in issues] == [("plan.open_questions", "warning")]


def test_issues_carry_the_module_line_when_the_source_is_given() -> None:
    text = (Path(__file__).parent / "plan_fixtures" / "timer.plan.yml").read_text()
    bad = text.replace("rtl/tiny_timer_irq.sv", "rtl/tiny_timer_counter.sv")
    plan, problems = parse_plan(bad, plan_path=PATH)
    assert plan is not None and not problems
    [issue] = validate_plan(plan, _env(), plan_path=PATH, source=bad)
    assert issue.line == bad.splitlines().index("  - name: tiny_timer_irq") + 1


def test_a_schema_error_is_an_issue() -> None:
    plan, issues = parse_plan("schema_version: 1\nblock: timer\n", plan_path=PATH)
    assert plan is None
    assert {i.rule for i in issues} == {"plan.schema"}
    assert any("top" in i.msg for i in issues)


# --- on tinysoc, through the check plugin ---------------------------------------------


def _run(root: Path, block: str) -> Any:
    ctx = ToolContext(repo_root=root, runner=LocalRunner(), params={"block": block})
    spec = CheckSpec(id="plan_check", capability="plan_check", adapter="plan_check")
    return asyncio.run(PlanCheck().run(spec, ctx))


@pytest.fixture(scope="module")
def tiny(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tinysoc_project(tmp_path_factory.mktemp("plan-check") / "tinysoc")


def test_valid_plans_for_timer_and_gpio_pass_on_tinysoc(tiny: Path) -> None:
    write_plan(tiny, "timer")
    write_plan(tiny, "gpio")
    timer = _run(tiny, "timer")
    assert timer.status == "pass", timer.issues
    assert timer.issues == ()
    gpio = _run(tiny, "gpio")
    assert gpio.status == "pass", gpio.issues
    # gpio's fixture has an open question: a warning the approver must read.
    assert [(i.rule, i.severity) for i in gpio.issues] == [("plan.open_questions", "warning")]


def test_the_overlapping_plan_fails_on_tinysoc(tiny: Path) -> None:
    write_plan(tiny, "timer", overlapping(fixture_plan("timer")))
    try:
        result = _run(tiny, "timer")
    finally:
        write_plan(tiny, "timer")
    assert result.status == "fail"
    assert [i.rule for i in result.issues] == ["plan.write_overlap"]


def test_no_plan_yet_is_not_a_failure(tiny: Path) -> None:
    result = _run(tiny, "top")
    assert result.status == "pass"
    assert [i.rule for i in result.issues] == ["plan.missing"]


def test_the_env_comes_from_the_model_and_the_profile(tiny: Path) -> None:
    write_plan(tiny, "timer")
    report = check_plan_file(tiny, "timer")
    env = report.env
    assert env is not None
    assert [r.name for r in env.requirements] == [f"REQ-TIM-00{i}" for i in range(1, 6)]
    assert env.interface_source == "spec"
    assert [p.name for p in env.interface] == [p.name for p in _PORTS]
    assert [m.name for m in env.existing_modules] == ["tiny_timer"]
    assert "rtl/tiny_timer*.sv" in env.layout
    assert "plan/timer.approved.json" in env.engine_outputs
    assert "plan/timer.plan.yml" in env.agent_outputs
    assert "rtl/tiny_timer.sv" in env.existing


def test_a_top_rtl_file_a_human_rule_owns_is_rejected(tmp_path: Path) -> None:
    # With tinysoc's own pack on, `tinysoc/rtl` (kind human) produces rtl/tiny_timer.sv.
    root = tinysoc_project(tmp_path / "t", packs=("tinysoc", "digital-rtl"))
    write_plan(root, "timer")
    result = _run(root, "timer")
    assert result.status == "fail"
    [issue] = result.issues
    assert issue.rule == "plan.write_engine"
    assert "'tinysoc/rtl[block=timer]'" in issue.msg


def test_the_project_naming_rule_applies(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path / "t", profile={"naming": {"rules": "org:qnsc/naming-v1.yml"}})
    write_plan(root, "timer")
    result = _run(root, "timer")
    assert result.status == "fail"
    assert {i.rule for i in result.issues} == {"plan.naming"}
    assert "must be m_qnsc_<function>" in result.issues[0].msg


def test_the_profile_limits_apply(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path / "t", profile={"plan": {"max_modules": 2}})
    write_plan(root, "timer")
    result = _run(root, "timer")
    assert [i.rule for i in result.issues] == ["plan.limit.modules"]

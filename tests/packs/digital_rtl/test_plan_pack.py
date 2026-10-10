"""M2-03: the plan file's schema, the pack's rules and skill, and the planner's context."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from plan_helpers import FIXTURES, REPO, fixture_plan, tinysoc_project
from pydantic import ValidationError

from chipgraph.adapters.runtime.claude_code import service
from chipgraph.app.build import builtin_packs_dir, make_scheduler
from chipgraph.app.context import AppContext
from chipgraph.core.engine.graph import StaticForeach, build_graph
from chipgraph.core.engine.rules import load_pack_rules
from chipgraph.core.plugin_api.pack import Pack, discover_packs
from chipgraph.core.runtime.roles import RoleError, SkillError, check_agent_rules, load_skills
from chipgraph.packs.digital_rtl.plan.model import Plan, schema_json


@pytest.fixture(scope="module")
def pack() -> Pack:
    packs_dir = builtin_packs_dir()
    assert packs_dir is not None
    return discover_packs([packs_dir])["digital-rtl"]


# --- schema ---------------------------------------------------------------------------


def test_committed_schema_matches_the_model() -> None:
    committed = REPO / "schemas" / "formats" / "plan.schema.json"
    assert committed.read_text(encoding="utf-8") == schema_json()
    schema = json.loads(schema_json())
    assert schema["title"] == "Plan"
    assert set(schema["required"]) == {"block", "top", "modules"}
    module = schema["$defs"]["PlanModule"]
    assert set(module["properties"]) == {
        "name",
        "summary",
        "reqs",
        "interface",
        "depends_on",
        "writes",
        "budget",
    }


@pytest.mark.parametrize("name", ["timer", "gpio"])
def test_the_fixture_plans_validate(name: str) -> None:
    plan = Plan.model_validate(fixture_plan(name))
    assert plan.schema_version == 1
    assert plan.block == name
    assert plan.module(plan.top) is not None


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda d: d["modules"][0].update(writes=[]), "at least 1 item"),
        (lambda d: d["modules"][0].update(writes=[{"path": "/abs.sv"}]), "relative"),
        (lambda d: d["modules"][0].update(writes=[{"path": "../up.sv"}]), "'..'"),
        (lambda d: d["modules"][0].update(writes=[{"path": "rtl/*.sv"}]), "plain file path"),
        (lambda d: d["modules"][0].update(name="bad-name"), "pattern"),
        (lambda d: d["modules"][0].update(budget={"tries": 0}), "greater than or equal"),
        (lambda d: d["modules"][0].update(extra=1), "Extra inputs"),
        (lambda d: d.update(schema_version=2), "Input should be 1"),
        (lambda d: d.update(unassigned_reqs=[{"req": "REQ-TIM-001", "reason": ""}]), "at least 1"),
    ],
)
def test_the_schema_rejects(change: object, match: str) -> None:
    data = fixture_plan("timer")
    change(data)  # type: ignore[operator]
    with pytest.raises(ValidationError, match=match):
        Plan.model_validate(data)


def test_fixtures_name_their_source() -> None:
    for path in FIXTURES.glob("*.plan.yml"):
        assert path.read_text().startswith("# A realistic plan for the tinysoc"), path


# --- the pack -------------------------------------------------------------------------


def test_the_plan_rules_load_from_the_pack(pack: Pack) -> None:
    rules = {rule.id: rule for rule in load_pack_rules(pack)}
    assert set(rules) == {"digital-rtl/plan", "digital-rtl/plan_expand", "digital-rtl/review"}
    plan = rules["digital-rtl/plan"]
    assert (plan.kind, plan.role, plan.foreach) == ("agent", "planner", "blocks")
    assert [(i.source, i.selector) for i in plan.inputs] == [("model", "block/{block}")]
    assert plan.outputs == ("plan/{block}.plan.yml",)
    assert plan.skills == ("plan/modules",)
    assert plan.checks == ("plan_check",)
    assert plan.budget.tries == 3 and plan.budget.model_fields_set == {"tries"}
    expand = rules["digital-rtl/plan_expand"]
    assert (expand.kind, expand.foreach, expand.gate) == ("gen", "blocks", "plan:{block}")
    assert [(i.source, i.selector) for i in expand.inputs] == [
        ("artifact", "plan/{block}.plan.yml")
    ]
    assert expand.outputs == ("plan/{block}.approved.json",)
    assert expand.run is not None and expand.run.use == "plan_expand"


def test_check_agent_rules_accepts_the_plan_rule(pack: Pack) -> None:
    rules = {rule.id: rule for rule in load_pack_rules(pack)}
    graph = build_graph(rules.values(), StaticForeach({"blocks": [{"block": "timer"}]}))
    check_agent_rules(graph.rules, graph.instances.values())  # does not raise
    # The planner writes exactly one plan file: a second output is refused.
    two = rules["digital-rtl/plan"].model_copy(
        update={"outputs": ("plan/{block}.plan.yml", "plan/{block}.notes.md")}
    )
    with pytest.raises(RoleError, match="exactly one output"):
        check_agent_rules({two.id: two}, ())


def test_the_plan_skill_resolves_for_the_planner_only(pack: Pack) -> None:
    skills = load_skills([pack])
    [skill] = skills.resolve(["plan/modules"], "planner")
    assert skill.roles == ("planner",)
    for needle in ("Interface first", "Write sets", "replaces: true", "unassigned_reqs"):
        assert needle in skill.text, needle
    for role in ("author", "critic", "tb-author"):
        with pytest.raises(SkillError, match=f"not for role '{role}'"):
            skills.resolve(["plan/modules"], role)


# --- the planner's context ------------------------------------------------------------


def test_a_planner_task_gets_the_model_slice_limits_naming_and_schema(tmp_path: Path) -> None:
    root = tinysoc_project(
        tmp_path / "t",
        profile={"naming": {"rules": "org:qnsc/naming-v1.yml"}, "plan": {"max_modules": 6}},
    )
    ctx = AppContext.load(root)
    answer = asyncio.run(service.next_task(ctx, "digital-rtl/plan[block=timer]"))
    [task] = answer["tasks"]
    assert task["role"] == "planner" and task["agent"] == "chipgraph:planner"
    assert task["outputs"] == ["plan/timer.plan.yml"]

    context = asyncio.run(service.get_context(AppContext.load(root), task["task_id"]))
    assert context["skills"] == ["plan/modules"]
    assert "plan/modules" in context["skill_texts"]
    assert context["checks"] == ["plan_check"]
    plan = context["plan"]
    assert plan["block"] == "timer" and plan["plan_path"] == "plan/timer.plan.yml"
    assert [r["id"] for r in plan["requirements"]] == [f"REQ-TIM-00{i}" for i in range(1, 6)]
    assert plan["interface"]["source"] == "spec"
    assert {"name": "irq", "direction": "output", "width": 1} in plan["interface"]["ports"]
    assert [m["name"] for m in plan["existing_modules"]] == ["tiny_timer"]
    assert "rtl/tiny_timer*.sv" in plan["layout"]
    assert "rtl/tiny_timer.sv" in plan["existing_files"]
    assert plan["limits"] == {"max_modules": 6, "max_depth": 4, "max_total_tries": 36}
    assert plan["naming"]["rules"] == "org:qnsc/naming-v1.yml"
    assert plan["naming"]["module"]["rule"] == "2.1 module"
    assert plan["schema"] == Plan.model_json_schema()


def test_other_roles_get_no_plan_context(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path / "t")
    ctx = AppContext.load(root)
    answer = asyncio.run(service.next_task(ctx, "digital-rtl/review[block=timer]"))
    [task] = answer["tasks"]
    context = asyncio.run(service.get_context(AppContext.load(root), task["task_id"]))
    assert "plan" not in context


def test_the_graph_has_the_plan_gate_instance(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path / "t", ingest=False)
    graph = make_scheduler(AppContext.load(root), "*").graph
    instance = graph.instances["digital-rtl/plan_expand[block=timer]"]
    assert [ref.path for ref in instance.inputs] == ["plan/timer.plan.yml"]
    assert graph.deps(instance.instance_id) == ("digital-rtl/plan[block=timer]",)

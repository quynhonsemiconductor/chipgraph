"""M2-03: the gate `plan:<block>` and the bounded `foreach: plan.modules` expansion.

The downstream rule is the test-only stub `stub/module_rtl` (M2-04/M2-06 are not merged
yet): `foreach: plan.modules`, one output `rtl/{module}.sv`.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
import yaml
from plan_helpers import (
    STUB_RULE,
    approve_plan,
    fixture_plan,
    overlapping,
    tinysoc_project,
    write_plan,
)

from chipgraph.app.build import load_rules, make_scheduler
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.checks.plan_check import check_plan_file
from chipgraph.core.engine.graph import BuildGraph
from chipgraph.packs.digital_rtl.plan.resolver import (
    gate_status,
    plan_gates,
    plan_modules,
    within_limits,
)

EXPAND = "digital-rtl/plan_expand"


def _ctx(root: Path) -> AppContext:
    return AppContext.load(root)


def _status(root: Path, block: str) -> str:
    ctx = _ctx(root)
    gate = plan_gates(load_rules(ctx), ctx.require_profile().profile.blocks)[block]
    return gate_status(ctx, gate)


def _graph(root: Path) -> BuildGraph:
    return make_scheduler(_ctx(root), "*").graph


def _stub_instances(root: Path) -> list[str]:
    return sorted(i for i, inst in _graph(root).instances.items() if inst.rule_id == STUB_RULE)


def _module(block: str, module: str) -> str:
    return f"{STUB_RULE}[block={block},module={module}]"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tinysoc_project(tmp_path / "tinysoc", stub=True)


# --- the gate -------------------------------------------------------------------------


def test_gate_waits_then_is_approved_then_waits_again_after_an_edit(project: Path) -> None:
    assert _status(project, "timer") == "waiting"  # no plan yet
    write_plan(project, "timer")
    assert _status(project, "timer") == "waiting"  # a plan, not approved
    approve_plan(project, "timer")
    assert _status(project, "timer") == "approved"
    assert _status(project, "gpio") == "waiting"  # per block
    path = write_plan(project, "timer")
    path.write_text(path.read_text() + "# a reviewer's note\n")
    assert _status(project, "timer") == "waiting"  # the approval pinned the old hash
    approve_plan(project, "timer")
    assert _status(project, "timer") == "approved"


def test_a_rejected_plan_gate_is_rejected(project: Path) -> None:
    write_plan(project, "timer")
    approve_plan(project, "timer", decision="reject")
    assert _status(project, "timer") == "rejected"
    assert _stub_instances(project) == []


def test_the_approval_pins_the_plan_file_hash(project: Path) -> None:
    write_plan(project, "timer")
    approve_plan(project, "timer")
    ctx = _ctx(project)
    [approval] = ctx.review.approvals("plan:timer")
    report = check_plan_file(project, "timer")
    assert approval.artifact_hashes == {".:plan/timer.plan.yml": report.sha256}


# --- the resolver ---------------------------------------------------------------------


def test_an_unapproved_plan_expands_to_nothing(project: Path) -> None:
    write_plan(project, "timer")
    assert plan_modules(_ctx(project), load_rules(_ctx(project))) == []
    assert _stub_instances(project) == []


def test_an_invalid_approved_plan_expands_to_nothing(project: Path) -> None:
    write_plan(project, "timer", overlapping(fixture_plan("timer")))
    approve_plan(project, "timer")  # a person approved it anyway
    assert _status(project, "timer") == "approved"
    assert _stub_instances(project) == []


def test_an_edited_plan_expands_to_nothing_until_approved_again(project: Path) -> None:
    write_plan(project, "timer")
    approve_plan(project, "timer")
    assert len(_stub_instances(project)) == 3
    data = fixture_plan("timer")
    data["modules"][0]["summary"] = "Changed after approval."
    write_plan(project, "timer", data)
    assert _stub_instances(project) == []
    approve_plan(project, "timer")
    assert len(_stub_instances(project)) == 3


def test_an_approved_plan_expands_with_params_edges_and_write_sets(project: Path) -> None:
    write_plan(project, "timer")
    write_plan(project, "gpio")
    approve_plan(project, "timer")

    entries = plan_modules(_ctx(project), load_rules(_ctx(project)))
    assert [e.params for e in entries] == [  # build order: dependencies first
        {"block": "timer", "module": "tiny_timer_counter"},
        {"block": "timer", "module": "tiny_timer_irq"},
        {"block": "timer", "module": "tiny_timer"},
    ]
    top = entries[2]
    assert top.after == (
        {"block": "timer", "module": "tiny_timer_counter"},
        {"block": "timer", "module": "tiny_timer_irq"},
    )
    assert top.writes == frozenset({"rtl/tiny_timer.sv"})
    assert top.inputs == ("plan/timer.approved.json",)

    graph = _graph(project)
    ids = sorted(i for i, inst in graph.instances.items() if inst.rule_id == STUB_RULE)
    assert ids == sorted(
        _module("timer", m) for m in ("tiny_timer_counter", "tiny_timer_irq", "tiny_timer")
    )  # gpio's plan is not approved: no gpio instances
    inst = graph.instances[_module("timer", "tiny_timer")]
    assert inst.params == {"block": "timer", "module": "tiny_timer"}
    assert [r.path for r in inst.outputs] == ["rtl/tiny_timer.sv"]
    assert graph.deps(inst.instance_id) == (
        f"{EXPAND}[block=timer]",
        _module("timer", "tiny_timer_counter"),
        _module("timer", "tiny_timer_irq"),
    )
    assert graph.deps(_module("timer", "tiny_timer_counter")) == (f"{EXPAND}[block=timer]",)
    order = graph.topo_order()
    assert order.index(_module("timer", "tiny_timer")) > order.index(
        _module("timer", "tiny_timer_irq")
    )


def test_a_plan_over_the_limits_expands_to_nothing(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path / "t", stub=True, profile={"plan": {"max_modules": 2}})
    write_plan(root, "timer")
    approve_plan(root, "timer")
    assert _stub_instances(root) == []
    report = check_plan_file(root, "timer")
    assert not within_limits(report, AppContext.load(root).require_profile().profile.plan)


def test_within_limits_is_checked_apart_from_plan_check(project: Path) -> None:
    from chipgraph.core.config.models import PlanCfg

    write_plan(project, "timer")
    report = check_plan_file(project, "timer")
    assert within_limits(report, PlanCfg())
    assert not within_limits(report, PlanCfg(max_modules=2))
    assert not within_limits(report, PlanCfg(max_depth=1))
    assert not within_limits(report, PlanCfg(max_total_tries=8))


def test_plan_modules_needs_the_plan_expand_rule(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path / "t", packs=(), stub=True, ingest=False)
    with pytest.raises(AppError, match="needs rule 'digital-rtl/plan_expand'"):
        make_scheduler(_ctx(root), "*")


def test_a_rule_writing_outside_a_module_write_set_gets_no_instances(project: Path) -> None:
    # plan_check sees what the plan.modules rules will write for each module, so the
    # plan fails before approval matters; the graph's own write-set guard is the last
    # line (tests/core/engine/test_graph_entries.py).
    rule_file = project / ".chipgraph" / "packs" / "stub" / "rules" / "module_rtl.yml"
    rule = yaml.safe_load(rule_file.read_text())
    rule["outputs"] = ["rtl/{module}_x.sv"]
    rule_file.write_text(yaml.safe_dump(rule))
    write_plan(project, "timer")
    approve_plan(project, "timer")
    report = check_plan_file(project, "timer")
    assert {i.rule for i in report.issues} == {"plan.write_missing"}
    assert _stub_instances(project) == []


def _random_plan(rng: random.Random, n: int) -> dict[str, object]:
    names = [f"tiny_timer_m{i}" for i in range(n)]
    modules = []
    for i, name in enumerate(names):
        deps = rng.sample(names[:i], k=rng.randint(0, min(i, 2))) if i else []
        if i and rng.random() < 0.1:  # now and then a back edge: a cycle
            deps.append(names[rng.randint(i, n - 1)])
        modules.append(
            {
                "name": name,
                "summary": f"module {i}",
                "reqs": [],
                "depends_on": deps,
                "writes": [{"path": f"rtl/{name}.sv"}],
                "budget": {"tries": rng.randint(1, 4)},
            }
        )
    for k in range(1, 6):
        modules[rng.randrange(n)]["reqs"].append(f"REQ-TIM-00{k}")  # type: ignore[attr-defined]
    return {"schema_version": 1, "block": "timer", "top": names[-1], "modules": modules}


def test_random_plans_never_expand_past_the_limits(tmp_path: Path) -> None:
    limits = {"max_modules": 5, "max_depth": 3, "max_total_tries": 12}
    root = tinysoc_project(tmp_path / "t", stub=True, profile={"plan": limits})
    rng = random.Random(20261010)
    expanded = 0
    for _ in range(40):
        n = rng.randint(1, 9)
        write_plan(root, "timer", _random_plan(rng, n))
        approve_plan(root, "timer")
        ctx = _ctx(root)
        entries = plan_modules(ctx, load_rules(ctx))
        assert len(entries) <= limits["max_modules"]
        if entries:
            expanded += 1
            assert len(entries) == n  # a plan expands whole, or not at all
    assert expanded > 0  # some random plans were valid

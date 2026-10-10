"""M2-03 end to end on tinysoc, with a scripted runtime and the test-only stub rule.

plan (agent: a first, overlapping plan is rejected by `plan_check`, the redo text names
the overlap, the second try passes) -> the gate `plan:timer` waits -> a person approves
-> `plan_expand` writes the approved plan -> one `stub/module_rtl` instance per module
runs, in dependency order. Editing the plan afterwards removes the instances again.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml
from plan_helpers import (
    FIXTURES,
    STUB_RULE,
    PlanRuntime,
    approve_plan,
    fixture_plan,
    overlapping,
    tinysoc_project,
)

from chipgraph.app.build import make_scheduler
from chipgraph.app.context import AppContext
from chipgraph.core.engine.scheduler import RunSummary
from chipgraph.core.state.journal import read

PLAN = "digital-rtl/plan[block=timer]"
EXPAND = "digital-rtl/plan_expand[block=timer]"
MODULES = ("tiny_timer_counter", "tiny_timer_irq", "tiny_timer")


def _module(name: str) -> str:
    return f"{STUB_RULE}[block=timer,module={name}]"


def _build(root: Path, runtime: PlanRuntime, target: str) -> tuple[AppContext, RunSummary]:
    ctx = AppContext.load(root)
    ctx.registry.register("runtime", "generic", runtime)
    return ctx, asyncio.run(make_scheduler(ctx, target).run(target))


def test_plan_check_gate_then_expanded_nodes_in_dependency_order(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path / "tinysoc", stub=True, runtime="generic")
    bad = yaml.safe_dump(overlapping(fixture_plan("timer")), sort_keys=False)
    good = (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8")
    runtime = PlanRuntime(root, [bad, good])

    # 1. The planner writes the plan; plan_check rejects the first try at once.
    ctx, first = _build(root, runtime, EXPAND)
    assert len(runtime.tasks) == 2
    feedback = runtime.tasks[1].context["feedback"]
    assert "plan_check" in feedback and "plan.write_overlap" in feedback
    assert "both write rtl/tiny_timer_counter.sv" in feedback
    assert first.done == (PLAN,)
    # 2. The gate waits: nothing the plan describes is in the graph yet.
    assert first.waiting_gate == (EXPAND,)
    graph = make_scheduler(ctx, "*").graph
    assert not [i for i in graph.instances if i.startswith(STUB_RULE)]

    # 3. A person approves; the plan is expanded and its modules run in order.
    approve_plan(root, "timer")
    ctx, second = _build(root, runtime, STUB_RULE)
    assert second.ok, second
    assert second.skipped_fresh == (PLAN,)
    assert set(second.done) == {EXPAND, *(_module(m) for m in MODULES)}
    assert len(runtime.tasks) == 2  # the approved plan was not planned again

    events = read(ctx.layout.journal(second.run_id)).events
    start = {e.rule_instance: e.seq for e in events if e.type == "rule_start"}
    done = {e.rule_instance: e.seq for e in events if e.type == "rule_done"}
    for name in MODULES:
        assert start[_module(name)] > done[EXPAND]
    top = _module("tiny_timer")
    assert start[top] > done[_module("tiny_timer_counter")]
    assert start[top] > done[_module("tiny_timer_irq")]

    for name in MODULES:
        assert (root / "rtl" / f"{name}.sv").read_text() == f"module {name};\nendmodule\n"
    approved = json.loads((root / "plan" / "timer.approved.json").read_text())
    assert approved["block"] == "timer"
    assert approved["order"] == list(MODULES)
    assert approved["modules"][2]["depends_on"] == ["tiny_timer_counter", "tiny_timer_irq"]
    assert approved["modules"][2]["writes"] == ["rtl/tiny_timer.sv"]

    # 4. Built again: everything is fresh (files the plan's nodes wrote are its own).
    _, third = _build(root, runtime, STUB_RULE)
    assert third.ok and not third.done
    assert set(third.skipped_fresh) == {PLAN, EXPAND, *(_module(m) for m in MODULES)}

    # 5. Editing the plan puts the gate back to waiting: the instances are gone.
    plan_file = root / "plan" / "timer.plan.yml"
    plan_file.write_text(plan_file.read_text() + "# edited after approval\n")
    graph = make_scheduler(AppContext.load(root), "*").graph
    assert not [i for i in graph.instances if i.startswith(STUB_RULE)]

"""M2-11a: the graph of target `rtl:<block>`, and what makes an accepted module stale.

The target is `plan -> plan_expand (gate plan:<block>) -> rtl_module per module (in
dependency order)`, with `tb_module` (independent of the RTL), then `review` and `open_pr`
(both after every module and the testbench), then `pr_gate` (gate `pr:<block>`). The graph
is data-driven: the plan decides the modules, so before the plan is approved the target is
the plan and its gate only, and a plan edit removes everything after it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from plan_helpers import FIXTURES, TINYSOC, approve_plan, write_plan
from rtl_target_helpers import (
    EXPAND,
    MODULE_IDS,
    OPEN_PR,
    PLAN,
    PR_GATE,
    REVIEW,
    TB,
    ChainRuntime,
    module_id,
    register,
    target_project,
)

from chipgraph.app.build import load_targets, make_scheduler, project_graph
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.core.engine.graph import BuildGraph, Staleness, compute_staleness
from chipgraph.core.engine.scheduler import RunSummary

COUNTER, IRQ, TOP = MODULE_IDS
GOOD = (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8")


def _approved_project(tmp_path: Path, **kwargs: object) -> Path:
    root = target_project(tmp_path / "tinysoc", real_lint=False, **kwargs)  # type: ignore[arg-type]
    write_plan(root, "timer")
    approve_plan(root, "timer")
    return root


def _graph(root: Path) -> BuildGraph:
    return project_graph(AppContext.load(root))


def _drive(root: Path, runtime: ChainRuntime, target: str) -> RunSummary:
    ctx = register(AppContext.load(root), runtime)
    return asyncio.run(make_scheduler(ctx, target).run(target))


def _staleness(root: Path, target: str) -> dict[str, Staleness]:
    scheduler = make_scheduler(AppContext.load(root), target)
    graph = scheduler.graph
    refs = [r for i in graph.instances.values() for r in (*i.inputs, *i.outputs) if r.path]
    return compute_staleness(graph, scheduler.records.all(), scheduler.store.current_hashes(refs))


# --- the graph ---------------------------------------------------------------------------


def test_before_the_plan_is_approved_the_target_is_the_plan_and_its_gate(tmp_path: Path) -> None:
    root = target_project(tmp_path / "tinysoc", real_lint=False)
    graph = _graph(root)
    assert graph.select("rtl:timer") == {PLAN, EXPAND}
    assert graph.select("plan:timer") == {PLAN, EXPAND}
    assert graph.rules["digital-rtl/plan_expand"].gate == "plan:{block}"
    # Nothing after the plan exists: no module, no open_pr, no PR gate.
    assert not [i for i in graph.instances if "rtl_module" in i or "open_pr" in i or "pr_gate" in i]


def test_the_graph_of_rtl_timer_instances_edges_and_order(tmp_path: Path) -> None:
    root = _approved_project(tmp_path)
    graph = _graph(root)
    selected = graph.select("rtl:timer")
    assert selected == {PLAN, EXPAND, *MODULE_IDS, TB, REVIEW, OPEN_PR, PR_GATE}
    assert graph.select("plan:timer") == {PLAN, EXPAND}

    deps = {iid: set(graph.deps(iid)) & selected for iid in selected}
    assert deps[PLAN] == set()
    assert deps[EXPAND] == {PLAN}  # the plan gate comes first
    assert deps[COUNTER] == {EXPAND}
    assert deps[IRQ] == {EXPAND}
    assert deps[TOP] == {EXPAND, COUNTER, IRQ}  # after the modules it instantiates
    assert deps[TB] == set()  # the testbench is independent of the RTL, by design
    assert not any(TB in deps[m] or m in deps[TB] for m in MODULE_IDS)
    assert deps[REVIEW] == {*MODULE_IDS, TB, EXPAND}  # after every module and the testbench
    assert deps[OPEN_PR] == {REVIEW, *MODULE_IDS, TB, EXPAND}
    assert deps[PR_GATE] == {OPEN_PR, EXPAND, *MODULE_IDS, TB}  # and what the PR holds
    assert graph.rules["digital-rtl/pr_gate"].gate == "pr:{block}"

    order = [iid for iid in graph.topo_order() if iid in selected]
    for iid in selected:
        assert all(order.index(dep) < order.index(iid) for dep in deps[iid])
    assert order[-1] == PR_GATE and order.index(PLAN) < order.index(EXPAND)

    # Other blocks have nothing in this target.
    assert not any("gpio" in iid or "block=top" in iid for iid in selected)


def test_the_files_the_instances_read_and_write(tmp_path: Path) -> None:
    root = _approved_project(tmp_path)
    graph = _graph(root)
    outputs = {
        iid: [r.path for r in graph.instances[iid].outputs] for iid in graph.select("rtl:timer")
    }
    assert outputs[TOP] == ["rtl/tiny_timer.sv"]
    assert outputs[TB] == ["dv/timer/test_timer.py"]
    assert outputs[REVIEW] == ["reports/review/timer.json"]
    assert outputs[OPEN_PR] == ["pr/timer.json"]
    assert outputs[PR_GATE] == ["pr/timer.approved.json"]
    inputs = {r.path for r in graph.instances[OPEN_PR].inputs if r.path}
    assert inputs == {
        "plan/timer.approved.json",
        "reports/review/timer.json",
        "rtl/tiny_timer_counter.sv",
        "rtl/tiny_timer_irq.sv",
        "rtl/tiny_timer.sv",
        "dv/timer/test_timer.py",
    }
    assert {r.path for r in graph.instances[PR_GATE].inputs if r.path} >= {"pr/timer.json"}


def test_a_plan_with_two_modules(tmp_path: Path) -> None:
    root = target_project(tmp_path / "tinysoc", real_lint=False)
    write_plan(root, "gpio")
    approve_plan(root, "gpio")
    graph = _graph(root)
    regs = module_id("tiny_gpio_regs").replace("timer", "gpio")
    top = "digital-rtl/rtl_module[block=gpio,module=tiny_gpio]"
    selected = graph.select("rtl:gpio")
    assert selected == {
        "digital-rtl/plan[block=gpio]",
        "digital-rtl/plan_expand[block=gpio]",
        regs,
        top,
        "dv/tb_module[block=gpio]",
        "digital-rtl/review[block=gpio]",
        "digital-rtl/open_pr[block=gpio]",
        "digital-rtl/pr_gate[block=gpio]",
    }
    assert regs in graph.deps(top)
    assert graph.deps(regs) == ("digital-rtl/plan_expand[block=gpio]",)


def test_a_block_without_a_plan_is_still_reviewed_as_before(tmp_path: Path) -> None:
    """`review` is over every block; only a block with an approved plan waits for modules."""
    root = _approved_project(tmp_path)
    graph = _graph(root)
    gpio = "digital-rtl/review[block=gpio]"
    assert gpio in graph.instances and graph.deps(gpio) == ()
    assert set(graph.deps(REVIEW)) >= set(MODULE_IDS)


# --- the target's name and params --------------------------------------------------------


def test_an_unknown_block_or_target_or_param_is_an_error(tmp_path: Path) -> None:
    root = _approved_project(tmp_path)
    graph = _graph(root)
    with pytest.raises(
        Exception,
        match=r"target 'rtl:nope'.*matched no instances; check its params \(block='nope'\)",
    ):
        graph.select("rtl:nope")
    with pytest.raises(Exception, match=r"needs block: write 'rtl:<block>'"):
        graph.select("rtl")
    with pytest.raises(Exception, match=r"has no param 'colour'"):
        graph.select("rtl:colour=red")
    with pytest.raises(Exception, match="unknown rule"):
        graph.select("nothing:timer")  # not a target: read as a rule id
    with pytest.raises(AppError, match="check its params"):
        make_scheduler(AppContext.load(root), "rtl:nope")


def test_the_pack_provides_the_targets(tmp_path: Path) -> None:
    root = _approved_project(tmp_path)
    targets = load_targets(AppContext.load(root))
    assert targets.names() == ("plan", "rtl")
    spec = targets.get("rtl")
    assert spec is not None and spec.pack == "digital-rtl"
    assert [p.name for p in spec.params] == ["block"]


# --- a plan edit removes everything after it ----------------------------------------------


def test_a_plan_edit_removes_the_downstream_instances_of_the_whole_chain(tmp_path: Path) -> None:
    root = target_project(tmp_path / "tinysoc")
    runtime = ChainRuntime(root, [GOOD])
    first = _drive(root, runtime, "rtl:timer")
    assert first.waiting_gate == (EXPAND,)
    approve_plan(root, "timer")
    second = _drive(root, runtime, "rtl:timer")
    assert second.waiting_gate == (PR_GATE,), second
    assert {*MODULE_IDS, TB, REVIEW, OPEN_PR} <= set(second.done)
    assert len(runtime.calls) == 1 + 3 + 1 + 1  # plan, three modules, testbench, review

    # Built again: everything is fresh, and the PR gate still waits.
    third = _drive(root, runtime, "rtl:timer")
    assert not third.done and third.waiting_gate == (PR_GATE,)
    assert len(runtime.calls) == 6

    # The plan is edited after approval: the gate waits again and the chain is gone.
    plan = root / "plan" / "timer.plan.yml"
    plan.write_text(plan.read_text(encoding="utf-8") + "# edited after approval\n")
    graph = _graph(root)
    leftover = [i for i in graph.instances if "rtl_module" in i or "open_pr" in i or "pr_gate" in i]
    assert leftover == []
    assert graph.select("rtl:timer") == {PLAN, EXPAND}
    # The block is reviewed as any block without an approved plan, on its own.
    assert graph.deps(REVIEW) == ()
    fourth = _drive(root, runtime, "rtl:timer")
    assert not fourth.ok  # the edited plan is not the one the planner wrote nor one approved
    assert not set(fourth.done) & {*MODULE_IDS, TB, REVIEW, OPEN_PR, PR_GATE}
    assert len(runtime.of("digital-rtl/rtl_module")) == 3  # nothing was rebuilt


# --- exemplars make an accepted module stale ---------------------------------------------------


def test_editing_an_exemplar_makes_an_accepted_module_stale_not_diverged(tmp_path: Path) -> None:
    root = target_project(tmp_path / "tinysoc", real_lint=False)
    runtime = ChainRuntime(root, [GOOD])
    _drive(root, runtime, "rtl:timer")
    approve_plan(root, "timer")
    target = "digital-rtl/rtl_module"
    _drive(root, runtime, target)
    assert len(runtime.of(target)) == 3
    inputs = {r.path for r in _graph(root).instances[TOP].inputs if r.path}
    assert "rtl/tiny_gpio.sv" in inputs  # an exemplar of the profile
    assert "rtl/tiny_timer.sv" not in inputs  # an exemplar the plan itself rewrites
    assert all(s.state == "fresh" for i, s in _staleness(root, target).items() if "rtl_module" in i)

    # An unrelated file: still fresh, nothing runs.
    (root / "README.md").write_text("notes\n")
    (root / "rtl" / "tiny_top.sv").write_text("// an unrelated change\n")
    again = _drive(root, runtime, target)
    assert not again.done and len(runtime.of(target)) == 3

    # An exemplar is edited: every module is stale (an input changed), none is diverged,
    # and the next build runs them again.
    exemplar = root / "rtl" / "tiny_gpio.sv"
    exemplar.write_text(exemplar.read_text(encoding="utf-8") + "// a new house style\n")
    states = _staleness(root, target)
    for iid in MODULE_IDS:
        assert states[iid].state == "stale", states[iid]
    assert any("input rtl/tiny_gpio.sv changed" in r for r in states[COUNTER].reasons)
    assert not any(
        "diverged" in s.state or "changed after" in " ".join(s.reasons) for s in states.values()
    )
    rebuilt = _drive(root, runtime, target)
    assert set(rebuilt.done) == set(MODULE_IDS) and rebuilt.ok, rebuilt
    assert len(runtime.of(target)) == 6
    assert all(s.state == "fresh" for i, s in _staleness(root, target).items() if "rtl_module" in i)


def test_the_naming_rules_file_the_profile_names_is_an_input_too(tmp_path: Path) -> None:
    root = target_project(tmp_path / "tinysoc", real_lint=False)
    path = root / ".chipgraph.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    tiny = yaml.safe_load((TINYSOC / ".chipgraph.yml").read_text(encoding="utf-8"))
    data["adapters"]["naming"] = tiny["adapters"]["naming"]  # `rules: .chipgraph/naming.yml`
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    runtime = ChainRuntime(root, [GOOD])
    _drive(root, runtime, "rtl:timer")
    approve_plan(root, "timer")
    inputs = {r.path for r in _graph(root).instances[TOP].inputs if r.path}
    assert ".chipgraph/naming.yml" in inputs and "rtl/tiny_gpio.sv" in inputs

    target = "digital-rtl/rtl_module"
    # The naming check is the project's own: the scripted modules must pass it.
    assert _drive(root, runtime, target).ok
    rules = root / ".chipgraph" / "naming.yml"
    rules.write_text(rules.read_text(encoding="utf-8") + "\n# a stricter house rule\n")
    states = _staleness(root, target)
    assert all(states[m].state == "stale" for m in MODULE_IDS)
    assert any("input .chipgraph/naming.yml changed" in r for r in states[COUNTER].reasons)

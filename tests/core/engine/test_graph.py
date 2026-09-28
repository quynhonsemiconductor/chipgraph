"""Tests for the build graph: foreach expansion, edges, cycles, select, staleness."""

from __future__ import annotations

import pytest

from chipgraph.core.contracts import InputSpec, RuleSpec, RunSpec
from chipgraph.core.engine.graph import (
    GraphError,
    ProductionRecord,
    StaticForeach,
    build_graph,
    compute_staleness,
    kind_for,
)
from chipgraph.core.state.artifacts import hash_bytes, hash_inputs


def _rule(
    id_: str,
    outputs: tuple[str, ...],
    *,
    inputs: tuple[InputSpec, ...] = (),
    foreach: str | None = None,
    kind: str = "gen",
) -> RuleSpec:
    run = RunSpec(use="cmd") if kind == "gen" else None
    return RuleSpec(id=id_, kind=kind, outputs=outputs, inputs=inputs, foreach=foreach, run=run)


# --- kind_for ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("design/timer/rtl/m_timer.sv", "rtl"),
        ("design/timer/rtl/m_timer.v", "rtl"),
        ("design/timer/rtl/pkg.svh", "rtl"),
        ("design/timer/rtl/pkg.vh", "rtl"),
        ("tools/gen.py", "script"),
        ("doc/README.md", "doc"),
        ("design/timer.yml", "config"),
        ("design/timer.yaml", "config"),
        ("pyproject.toml", "config"),
        ("report/lint.json", "report"),
        ("report/lint.log", "report"),
        ("report/lint.xml", "report"),
        ("fig/block.svg", "diagram"),
        ("fig/block.png", "diagram"),
        ("fig/block.drawio", "diagram"),
        ("design/timer.bin", "other"),
    ],
)
def test_kind_for(path: str, expected: str) -> None:
    assert kind_for(path) == expected


# --- foreach expansion and substitution ---------------------------------------------


def test_foreach_expansion_and_substitution() -> None:
    rule = _rule(
        "digital-rtl/rtl_module",
        outputs=("design/{block}/rtl/m_{module}.sv",),
        inputs=(InputSpec(source="model", selector="block/{block}"),),
        foreach="model.plan(block).modules",
    )
    resolver = StaticForeach(
        {
            "model.plan(block).modules": [
                {"block": "timer", "module": "cnt"},
                {"block": "timer", "module": "cmp"},
            ]
        }
    )
    graph = build_graph([rule], resolver)
    assert set(graph.instances) == {
        "digital-rtl/rtl_module[block=timer,module=cnt]",
        "digital-rtl/rtl_module[block=timer,module=cmp]",
    }
    inst = graph.instances["digital-rtl/rtl_module[block=timer,module=cnt]"]
    assert inst.outputs[0].path == "design/timer/rtl/m_cnt.sv"
    assert inst.inputs[0].model_key == "block/timer"


def test_foreach_none_yields_single_empty_params_instance() -> None:
    rule = _rule("digital-rtl/top", outputs=("design/top.sv",))
    graph = build_graph([rule], StaticForeach({}))
    assert list(graph.instances) == ["digital-rtl/top[]"]
    assert graph.instances["digital-rtl/top[]"].params == {}


def test_base_params_merge_into_no_foreach_instance() -> None:
    rule = _rule("digital-rtl/top", outputs=("design/{block}/top.sv",))
    graph = build_graph([rule], StaticForeach({}), base_params={"block": "timer"})
    [instance] = graph.instances.values()
    assert instance.params == {"block": "timer"}
    assert instance.outputs[0].path == "design/timer/top.sv"


def test_missing_placeholder_raises_graph_error() -> None:
    rule = _rule("digital-rtl/top", outputs=("design/{block}/top.sv",))
    with pytest.raises(GraphError, match=r"digital-rtl/top.*\{block\}"):
        build_graph([rule], StaticForeach({}))


def test_dotted_placeholder_substitution() -> None:
    rule = _rule(
        "digital-rtl/rtl_module",
        outputs=("design/{module.name}.sv",),
        foreach="modules",
    )
    resolver = StaticForeach({"modules": [{"module.name": "cnt"}]})
    graph = build_graph([rule], resolver)
    [instance] = graph.instances.values()
    assert instance.outputs[0].path == "design/cnt.sv"


def test_spec_input_fragment_is_split_and_kept() -> None:
    rule = _rule(
        "digital-rtl/rtl_module",
        outputs=("design/{block}.sv",),
        inputs=(InputSpec(source="spec", selector="{block}#req:R1"),),
        foreach="modules",
    )
    resolver = StaticForeach({"modules": [{"block": "timer"}]})
    graph = build_graph([rule], resolver)
    [instance] = graph.instances.values()
    assert instance.inputs[0].path == "timer"
    assert instance.inputs[0].kind == "spec"
    assert graph.spec_fragments[".:timer"] == "req:R1"


# --- write-set conflicts (F2) --------------------------------------------------------


def test_two_rules_writing_same_output_is_an_error() -> None:
    rule_a = _rule("digital-rtl/a", outputs=("design/timer.sv",))
    rule_b = _rule("digital-rtl/b", outputs=("design/timer.sv",))
    with pytest.raises(GraphError, match="write-set conflict") as excinfo:
        build_graph([rule_a, rule_b], StaticForeach({}))
    message = str(excinfo.value)
    assert "digital-rtl/a[]" in message
    assert "digital-rtl/b[]" in message
    assert "design/timer.sv" in message


# --- edges, topo order, cycles -------------------------------------------------------


def _chain_rules() -> list[RuleSpec]:
    rule_a = _rule("p/a", outputs=("design/a.sv",))
    rule_b = _rule(
        "p/b",
        outputs=("design/b.sv",),
        inputs=(InputSpec(source="path", selector="design/a.sv"),),
    )
    rule_c = _rule(
        "p/c",
        outputs=("design/c.sv",),
        inputs=(InputSpec(source="path", selector="design/b.sv"),),
    )
    return [rule_a, rule_b, rule_c]


def test_chain_edges_and_topo_order() -> None:
    graph = build_graph(_chain_rules(), StaticForeach({}))
    a, b, c = "p/a[]", "p/b[]", "p/c[]"
    assert graph.deps(b) == (a,)
    assert graph.deps(c) == (b,)
    assert graph.deps(a) == ()
    assert graph.dependents(a) == (b,)
    assert graph.topo_order() == [a, b, c]


def test_cycle_is_detected() -> None:
    rule_a = _rule(
        "p/a",
        outputs=("design/a.sv",),
        inputs=(InputSpec(source="path", selector="design/b.sv"),),
    )
    rule_b = _rule(
        "p/b",
        outputs=("design/b.sv",),
        inputs=(InputSpec(source="path", selector="design/a.sv"),),
    )
    with pytest.raises(GraphError, match="cycle detected"):
        build_graph([rule_a, rule_b], StaticForeach({}))


def test_ready_progression() -> None:
    graph = build_graph(_chain_rules(), StaticForeach({}))
    a, b, c = "p/a[]", "p/b[]", "p/c[]"
    assert graph.ready(set()) == [a]
    assert graph.ready({a}) == [b]
    assert graph.ready({a, b}) == [c]
    assert graph.ready({a, b, c}) == []
    assert graph.ready(set(), running={a}) == []


def test_downstream() -> None:
    graph = build_graph(_chain_rules(), StaticForeach({}))
    a = "p/a[]"
    assert graph.downstream(a) == {"p/b[]", "p/c[]"}


def test_external_inputs() -> None:
    graph = build_graph(_chain_rules(), StaticForeach({}))
    assert graph.external_inputs() == set()

    rule = _rule(
        "p/only",
        outputs=("design/x.sv",),
        inputs=(InputSpec(source="path", selector="design/spec.yml"),),
    )
    graph2 = build_graph([rule], StaticForeach({}))
    assert graph2.external_inputs() == {".:design/spec.yml"}


# --- select --------------------------------------------------------------------------


def test_select_star() -> None:
    graph = build_graph(_chain_rules(), StaticForeach({}))
    assert graph.select("*") == {"p/a[]", "p/b[]", "p/c[]"}


def test_select_rule_id_pulls_upstream() -> None:
    graph = build_graph(_chain_rules(), StaticForeach({}))
    assert graph.select("p/c") == {"p/a[]", "p/b[]", "p/c[]"}
    assert graph.select("p/b") == {"p/a[]", "p/b[]"}
    assert graph.select("p/a") == {"p/a[]"}


def test_select_with_partial_params() -> None:
    rule = _rule(
        "p/mod",
        outputs=("design/{block}/{module}.sv",),
        foreach="modules",
    )
    resolver = StaticForeach(
        {
            "modules": [
                {"block": "timer", "module": "cnt"},
                {"block": "timer", "module": "cmp"},
                {"block": "uart", "module": "rx"},
            ]
        }
    )
    graph = build_graph([rule], resolver)
    selected = graph.select("p/mod[block=timer]")
    assert selected == {
        "p/mod[block=timer,module=cnt]",
        "p/mod[block=timer,module=cmp]",
    }


def test_select_unknown_rule_errors() -> None:
    graph = build_graph(_chain_rules(), StaticForeach({}))
    with pytest.raises(GraphError):
        graph.select("p/does-not-exist")


# --- staleness -------------------------------------------------------------------


def _record_for(graph, iid: str, current: dict[str, str]) -> ProductionRecord:
    instance = graph.instances[iid]
    inputs_hash = hash_inputs(
        (ref.repo + ":" + str(ref.path), current[ref.repo + ":" + str(ref.path)])
        for ref in instance.inputs
    )
    output_hashes = {
        ref.repo + ":" + str(ref.path): current[ref.repo + ":" + str(ref.path)]
        for ref in instance.outputs
    }
    return ProductionRecord(instance_id=iid, inputs_hash=inputs_hash, output_hashes=output_hashes)


def test_never_built() -> None:
    graph = build_graph(_chain_rules(), StaticForeach({}))
    states = compute_staleness(graph, records={}, current={})
    assert states["p/a[]"].state == "never_built"


def test_fresh_after_matching_record() -> None:
    graph = build_graph(_chain_rules(), StaticForeach({}))
    a = "p/a[]"
    current = {".:design/a.sv": hash_bytes(b"content-a")}
    record = _record_for(graph, a, current)
    states = compute_staleness(graph, records={a: record}, current=current)
    assert states[a].state == "fresh"


def test_input_change_marks_stale_and_propagates_downstream() -> None:
    graph = build_graph(_chain_rules(), StaticForeach({}))
    a, b, c = "p/a[]", "p/b[]", "p/c[]"
    current = {
        ".:design/a.sv": hash_bytes(b"a1"),
        ".:design/b.sv": hash_bytes(b"b1"),
        ".:design/c.sv": hash_bytes(b"c1"),
    }
    records = {iid: _record_for(graph, iid, current) for iid in (a, b, c)}

    # a's own content changes (an external edit to its "source"): simulate by
    # changing what current says a's output now hashes to.
    changed = dict(current)
    changed[".:design/a.sv"] = hash_bytes(b"a2-different")

    states = compute_staleness(graph, records=records, current=changed)
    assert states[a].state == "diverged"
    assert states[b].state == "stale"
    assert any("design/a.sv" in reason for reason in states[b].reasons)
    assert states[c].state == "stale"
    assert any(b in reason for reason in states[c].reasons)


def test_output_missing_is_stale() -> None:
    graph = build_graph(_chain_rules(), StaticForeach({}))
    a = "p/a[]"
    current = {".:design/a.sv": hash_bytes(b"a1")}
    record = _record_for(graph, a, current)
    states = compute_staleness(graph, records={a: record}, current={})
    assert states[a].state == "stale"
    assert any("missing" in reason for reason in states[a].reasons)


def test_output_edited_is_diverged_and_dependents_stale() -> None:
    graph = build_graph(_chain_rules(), StaticForeach({}))
    a, b = "p/a[]", "p/b[]"
    current = {
        ".:design/a.sv": hash_bytes(b"a1"),
        ".:design/b.sv": hash_bytes(b"b1"),
    }
    records = {
        a: _record_for(graph, a, current),
        b: _record_for(graph, b, current),
    }
    edited = dict(current)
    edited[".:design/a.sv"] = hash_bytes(b"hand-edited")

    states = compute_staleness(graph, records=records, current=edited)
    assert states[a].state == "diverged"
    assert states[b].state == "stale"


def test_unrelated_branch_stays_fresh() -> None:
    rule_root = _rule("p/root", outputs=("design/root.sv",))
    rule_left = _rule(
        "p/left",
        outputs=("design/left.sv",),
        inputs=(InputSpec(source="path", selector="design/root.sv"),),
    )
    rule_right = _rule("p/right", outputs=("design/right.sv",))
    graph = build_graph([rule_root, rule_left, rule_right], StaticForeach({}))
    root, left, right = "p/root[]", "p/left[]", "p/right[]"

    current = {
        ".:design/root.sv": hash_bytes(b"root1"),
        ".:design/left.sv": hash_bytes(b"left1"),
        ".:design/right.sv": hash_bytes(b"right1"),
    }
    records = {iid: _record_for(graph, iid, current) for iid in (root, left, right)}

    changed = dict(current)
    changed[".:design/root.sv"] = hash_bytes(b"root2")

    states = compute_staleness(graph, records=records, current=changed)
    assert states[root].state == "diverged"
    assert states[left].state == "stale"
    assert states[right].state == "fresh"

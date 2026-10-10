"""M2-03: `ForeachEntry` expansions in `build_graph`: extra inputs, ordering edges
between instances of one rule, and write sets."""

from __future__ import annotations

import pytest

from chipgraph.core.contracts import InputSpec, RuleSpec, RunSpec
from chipgraph.core.engine.graph import (
    EntryForeachResolver,
    ForeachEntry,
    GraphError,
    StaticForeach,
    build_graph,
)


class _Entries:
    def __init__(self, entries: list[ForeachEntry]) -> None:
        self.entries = entries

    def expand(self, expr: str) -> list[dict[str, str]]:
        return [dict(e.params) for e in self.expand_entries(expr)]

    def expand_entries(self, expr: str) -> list[ForeachEntry]:
        assert expr == "plan.modules"
        return list(self.entries)


def _rule(outputs: tuple[str, ...] = ("rtl/{module}.sv",)) -> RuleSpec:
    return RuleSpec(
        id="p/mod",
        kind="gen",
        run=RunSpec(use="cmd"),
        foreach="plan.modules",
        inputs=(InputSpec(source="path", selector="spec/{block}.md"),),
        outputs=outputs,
    )


def _producer() -> RuleSpec:
    return RuleSpec(id="p/expand", kind="gen", run=RunSpec(use="cmd"), outputs=("plan/a.json",))


def _entry(module: str, *after: str, writes: set[str] | None = None) -> ForeachEntry:
    return ForeachEntry(
        params={"block": "a", "module": module},
        inputs=("plan/a.json",),
        after=tuple({"block": "a", "module": m} for m in after),
        writes=frozenset(writes) if writes is not None else frozenset({f"rtl/{module}.sv"}),
    )


def _id(module: str) -> str:
    return f"p/mod[block=a,module={module}]"


def test_the_protocol_is_recognised() -> None:
    assert isinstance(_Entries([]), EntryForeachResolver)
    assert not isinstance(StaticForeach({}), EntryForeachResolver)


def test_entries_add_inputs_and_ordering_edges() -> None:
    resolver = _Entries([_entry("x"), _entry("y"), _entry("top", "x", "y")])
    graph = build_graph([_rule(), _producer()], resolver)
    top = graph.instances[_id("top")]
    assert [r.path for r in top.inputs] == ["spec/a.md", "plan/a.json"]
    assert graph.deps(_id("top")) == ("p/expand[]", _id("x"), _id("y"))
    assert graph.deps(_id("x")) == ("p/expand[]",)
    assert graph.dependents(_id("x")) == (_id("top"),)
    order = graph.topo_order()
    assert order.index(_id("top")) > max(order.index(_id("x")), order.index(_id("y")))
    assert graph.downstream("p/expand[]") == {_id("x"), _id("y"), _id("top")}


def test_a_plain_resolver_still_works() -> None:
    resolver = StaticForeach({"plan.modules": [{"block": "a", "module": "x"}]})
    graph = build_graph([_rule()], resolver)
    assert graph.deps(_id("x")) == ()
    assert [r.path for r in graph.instances[_id("x")].inputs] == ["spec/a.md"]


def test_an_output_outside_the_write_set_is_rejected() -> None:
    resolver = _Entries([_entry("x", writes={"rtl/other.sv"})])
    with pytest.raises(GraphError, match="outside the write set its foreach entry grants"):
        build_graph([_rule()], resolver)


def test_ordering_edges_to_unknown_or_self_are_rejected() -> None:
    with pytest.raises(GraphError, match="not in the graph"):
        build_graph([_rule()], _Entries([_entry("x", "ghost")]))
    with pytest.raises(GraphError, match="after itself"):
        build_graph([_rule()], _Entries([_entry("x", "x")]))


def test_an_ordering_cycle_is_rejected() -> None:
    resolver = _Entries([_entry("x", "y"), _entry("y", "x")])
    with pytest.raises(GraphError, match="cycle detected"):
        build_graph([_rule()], resolver)


def test_entries_with_no_write_set_are_unbounded() -> None:
    entry = ForeachEntry(params={"block": "a", "module": "x"})
    graph = build_graph([_rule(("rtl/{module}.sv", "dv/{module}.py"))], _Entries([entry]))
    assert [r.path for r in graph.instances[_id("x")].outputs] == ["rtl/x.sv", "dv/x.py"]

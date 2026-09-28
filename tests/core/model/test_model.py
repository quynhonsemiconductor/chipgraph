"""Tests for `DesignModel`: queries, merge conflicts, and dangling-relation validation."""

from __future__ import annotations

import pytest

from chipgraph.core.model.entities import BlockEntity, ModuleEntity, PortEntity
from chipgraph.core.model.keys import make_key
from chipgraph.core.model.model import DesignModel, ModelConflict
from chipgraph.core.model.provenance import Provenance
from chipgraph.core.model.relations import Relation


def _tinysoc_model() -> DesignModel:
    block = BlockEntity(key=make_key("block", "timer"), name="timer")
    module = ModuleEntity(key=make_key("module", "tiny_timer"), name="tiny_timer", block=block.key)
    port_clk = PortEntity(
        key=make_key("port", "tiny_timer", "i_clk"),
        name="i_clk",
        direction="input",
        clock="clock:peri",
        module=module.key,
    )
    port_rst = PortEntity(
        key=make_key("port", "tiny_timer", "i_rst_n"),
        name="i_rst_n",
        direction="input",
        module=module.key,
    )
    relations = (
        Relation(kind="contains", src=block.key, dst=module.key),
        Relation(kind="contains", src=module.key, dst=port_clk.key),
        Relation(kind="contains", src=module.key, dst=port_rst.key),
    )
    return DesignModel.build([block, module, port_clk, port_rst], relations)


def test_get_returns_entity_or_none() -> None:
    model = _tinysoc_model()
    assert model.get("block:timer") is not None
    assert model.get("block:timer").name == "timer"  # type: ignore[union-attr]
    assert model.get("block:nope") is None


def test_by_kind_filters_entities() -> None:
    model = _tinysoc_model()
    ports = model.by_kind("port")
    assert {p.key for p in ports} == {"port:tiny_timer.i_clk", "port:tiny_timer.i_rst_n"}
    assert model.by_kind("register") == ()


def test_children_walks_contains_relations() -> None:
    model = _tinysoc_model()
    modules = model.children("block:timer")
    assert [m.key for m in modules] == ["module:tiny_timer"]
    ports = model.children("module:tiny_timer")
    assert {p.key for p in ports} == {"port:tiny_timer.i_clk", "port:tiny_timer.i_rst_n"}
    assert model.children("port:tiny_timer.i_clk") == ()


def test_find_by_kind_only() -> None:
    model = _tinysoc_model()
    assert {e.key for e in model.find(kind="module")} == {"module:tiny_timer"}


def test_find_by_typed_attr() -> None:
    model = _tinysoc_model()
    clocked = model.find(kind="port", clock="clock:peri")
    assert [p.key for p in clocked] == ["port:tiny_timer.i_clk"]


def test_find_excludes_entities_missing_the_attr() -> None:
    model = _tinysoc_model()
    # 'clock' exists as a field on PortEntity but is unset on i_rst_n, so it must not match.
    assert model.find(kind="port", clock="clock:missing") == ()


def test_find_by_generic_attrs_dict() -> None:
    block = BlockEntity(key=make_key("block", "gpio"), name="gpio", attrs={"width": 8})
    model = DesignModel.build([block])
    assert model.find(width=8) == (block,)
    assert model.find(width=16) == ()


def test_get_relations_filters_by_src_dst_kind() -> None:
    model = _tinysoc_model()
    assert len(model.get_relations(src="block:timer")) == 1
    assert len(model.get_relations(kind="contains")) == 3
    assert model.get_relations(src="block:timer", dst="module:tiny_timer", kind="contains")
    assert model.get_relations(src="block:timer", dst="module:nope") == ()


def test_build_raises_on_conflicting_duplicate_keys() -> None:
    first = BlockEntity(key="block:timer", name="timer", source=Provenance(file="a.yml"))
    second = BlockEntity(key="block:timer", name="timer-renamed", source=Provenance(file="b.yml"))
    with pytest.raises(ModelConflict) as excinfo:
        DesignModel.build([first, second])
    assert excinfo.value.key == "block:timer"
    assert excinfo.value.first == first
    assert excinfo.value.second == second


def test_build_allows_identical_duplicate_entities() -> None:
    entity = BlockEntity(key="block:timer", name="timer")
    model = DesignModel.build([entity, entity])
    assert model.entities == {"block:timer": entity}


def test_merge_combines_disjoint_models() -> None:
    a = DesignModel.build([BlockEntity(key="block:timer", name="timer")])
    b = DesignModel.build([BlockEntity(key="block:uart", name="uart")])
    merged = a.merge(b)
    assert set(merged.entities) == {"block:timer", "block:uart"}


def test_merge_raises_model_conflict_on_incompatible_overlap() -> None:
    a = DesignModel.build([BlockEntity(key="block:timer", name="timer", owner="nghia")])
    b = DesignModel.build([BlockEntity(key="block:timer", name="timer", owner="someone-else")])
    with pytest.raises(ModelConflict):
        a.merge(b)


def test_merge_concatenates_relations() -> None:
    r1 = Relation(kind="contains", src="block:timer", dst="module:tiny_timer")
    r2 = Relation(kind="verifies", src="test:x", dst="requirement:REQ-1")
    a = DesignModel.build([BlockEntity(key="block:timer", name="timer")], [r1])
    b = DesignModel.build([], [r2])
    merged = a.merge(b)
    assert merged.relations == (r1, r2)


def test_validate_relations_reports_dangling_endpoints() -> None:
    block = BlockEntity(key="block:timer", name="timer")
    relation = Relation(kind="contains", src="block:timer", dst="module:missing")
    model = DesignModel.build([block], [relation])
    errors = model.validate_relations()
    assert len(errors) == 1
    assert "module:missing" in errors[0]


def test_validate_relations_reports_both_endpoints_when_both_dangle() -> None:
    relation = Relation(kind="contains", src="block:missing-a", dst="module:missing-b")
    model = DesignModel.build([], [relation])
    errors = model.validate_relations()
    assert len(errors) == 2


def test_validate_relations_empty_when_all_resolve() -> None:
    model = _tinysoc_model()
    assert model.validate_relations() == []

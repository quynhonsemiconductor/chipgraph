"""Tests for the pure core ingest merge (`chipgraph.core.model.ingest`)."""

from __future__ import annotations

import random

from chipgraph.core.model.entities import ClockEntity, ModuleEntity, PortEntity
from chipgraph.core.model.ingest import InputFile, SourcePart, ingest
from chipgraph.core.model.model import DesignModel
from chipgraph.core.model.provenance import Provenance
from chipgraph.core.model.relations import Relation


def _module_part(source: str, block: str | None, *modules: ModuleEntity) -> SourcePart:
    return SourcePart(source=source, block=block, model=DesignModel.build(list(modules), []))


def _mod(name: str, file: str | None = None) -> ModuleEntity:
    return ModuleEntity(key=f"module:{name}", name=name, file=file)


def test_single_candidate_gets_owner_and_contains_relation() -> None:
    from chipgraph.core.model.entities import BlockEntity

    part = SourcePart(
        source="chip",
        block=None,
        model=DesignModel.build([BlockEntity(key="block:timer", name="timer")], []),
    )
    rtl = _module_part("rtl", "block:timer", _mod("tiny_timer", "rtl/tiny_timer.sv"))
    model, _result = ingest([part, rtl])
    module = model.get("module:tiny_timer")
    assert module is not None
    assert module.block == "block:timer"
    rels = model.get_relations(src="block:timer", dst="module:tiny_timer", kind="contains")
    assert len(rels) == 1
    assert "used_by" not in module.attrs


def test_top_aggregation_owner_is_the_block_whose_dir_holds_the_file() -> None:
    # Both the timer block and the top block elaborate tiny_timer; the timer block's
    # filelist directory holds the file, so it owns it. `used_by` lists both candidates.
    timer = _module_part("rtl", "block:timer", _mod("tiny_timer", "rtl/tiny_timer.sv"))
    top = _module_part(
        "rtl",
        "block:top",
        _mod("tiny_top", "rtl/tiny_top.sv"),
        _mod("tiny_timer", "rtl/tiny_timer.sv"),
    )
    model, _ = ingest([timer, top])
    tiny_timer = model.get("module:tiny_timer")
    assert tiny_timer is not None
    assert tiny_timer.block == "block:timer"
    assert tiny_timer.attrs.get("used_by") == ["block:timer", "block:top"]
    # tiny_top is only in the top part, so top owns it.
    assert model.get("module:tiny_top").block == "block:top"


def test_tie_with_no_dir_or_name_match_and_equal_counts_has_no_owner() -> None:
    # A vendor cell appears in two blocks, its file is under neither block's directory,
    # its name matches neither, and both parts have the same module count: no owner.
    a = _module_part(
        "rtl",
        "block:cpu",
        _mod("prim_buf", "vendor/prim/prim_buf.sv"),
        _mod("cpu_core", "rtl/cpu_core.sv"),
    )
    b = _module_part(
        "rtl",
        "block:dma",
        _mod("prim_buf", "vendor/prim/prim_buf.sv"),
        _mod("dma_core", "rtl/dma_core.sv"),
    )
    model, _ = ingest([a, b])
    prim = model.get("module:prim_buf")
    assert prim is not None
    assert prim.block is None
    assert prim.attrs.get("used_by") == ["block:cpu", "block:dma"]
    # No owner => no `contains` relation for it.
    assert model.get_relations(dst="module:prim_buf", kind="contains") == ()


def test_fewest_modules_breaks_a_tie_when_unique() -> None:
    small = _module_part("rtl", "block:small", _mod("shared", "x/shared.sv"))
    big = _module_part(
        "rtl",
        "block:big",
        _mod("shared", "x/shared.sv"),
        _mod("other", "y/other.sv"),
    )
    model, _ = ingest([small, big])
    assert model.get("module:shared").block == "block:small"


def test_order_independence() -> None:
    parts = [
        _module_part("rtl", "block:timer", _mod("tiny_timer", "rtl/tiny_timer.sv")),
        _module_part("rtl", "block:gpio", _mod("tiny_gpio", "rtl/tiny_gpio.sv")),
        _module_part(
            "rtl",
            "block:top",
            _mod("tiny_top", "rtl/tiny_top.sv"),
            _mod("tiny_timer", "rtl/tiny_timer.sv"),
            _mod("tiny_gpio", "rtl/tiny_gpio.sv"),
        ),
    ]
    model_a, result_a = ingest(parts)
    shuffled = list(parts)
    random.Random(1234).shuffle(shuffled)
    model_b, result_b = ingest(shuffled)

    dump_a = {k: e.model_dump(mode="json") for k, e in model_a.entities.items()}
    dump_b = {k: e.model_dump(mode="json") for k, e in model_b.entities.items()}
    assert dump_a == dump_b
    assert sorted(r.model_dump_json() for r in model_a.relations) == sorted(
        r.model_dump_json() for r in model_b.relations
    )
    assert result_a.stats == result_b.stats
    assert result_a.build_inputs_hash == result_b.build_inputs_hash


def test_content_conflict_keeps_one_and_records_a_warning() -> None:
    # Same clock key, different content (frequency), from a chip spec and from RTL.
    chip = SourcePart(
        source="chip",
        block=None,
        model=DesignModel.build(
            [
                ClockEntity(
                    key="clock:clk",
                    name="clk",
                    frequency_hz=100.0,
                    source=Provenance(file="chip.yml", line=1, extractor="chip-yaml"),
                )
            ],
            [],
        ),
    )
    rtl = SourcePart(
        source="rtl",
        block="block:top",
        model=DesignModel.build(
            [
                ClockEntity(
                    key="clock:clk",
                    name="clk",
                    frequency_hz=None,
                    source=Provenance(file="rtl/top.sv", line=3, extractor="pyslang"),
                )
            ],
            [],
        ),
    )
    model, result = ingest([rtl, chip])
    kept = model.get("clock:clk")
    assert kept is not None
    # chip beats rtl by source priority.
    assert kept.frequency_hz == 100.0
    conflicts = [i for i in result.issues if i.code == "conflict"]
    assert len(conflicts) == 1
    assert conflicts[0].severity == "warning"
    assert conflicts[0].key == "clock:clk"
    assert set(conflicts[0].sources) == {"chip", "rtl"}
    assert result.stats.conflicts == 1


def test_identical_duplicates_are_silent() -> None:
    clk = ClockEntity(key="clock:clk", name="clk")
    a = SourcePart(source="rtl", block="block:a", model=DesignModel.build([clk], []))
    b = SourcePart(source="rtl", block="block:b", model=DesignModel.build([clk], []))
    _, result = ingest([a, b])
    assert result.stats.conflicts == 0
    assert [i for i in result.issues if i.code == "conflict"] == []


def test_dangling_relation_becomes_a_warning() -> None:
    part = SourcePart(
        source="rtl",
        block="block:top",
        model=DesignModel(
            entities={"module:m": _mod("m", "rtl/m.sv")},
            relations=(Relation(kind="instantiates", src="module:m", dst="module:missing"),),
        ),
    )
    _, result = ingest([part])
    dangling = [i for i in result.issues if i.code == "dangling"]
    assert dangling
    assert all(i.severity == "warning" for i in dangling)


def test_build_inputs_hash_changes_when_a_listed_source_changes() -> None:
    part = _module_part("rtl", "block:timer", _mod("tiny_timer", "rtl/tiny_timer.sv"))
    files_v1 = [InputFile(rel_path="rtl/tiny_timer.sv", sha256="aaa")]
    files_v2 = [InputFile(rel_path="rtl/tiny_timer.sv", sha256="bbb")]
    _, r1 = ingest([part], input_files=files_v1)
    _, r2 = ingest([part], input_files=files_v2)
    assert r1.build_inputs_hash != r2.build_inputs_hash


def test_build_inputs_hash_is_order_independent_but_profile_sensitive() -> None:
    part = _module_part("rtl", "block:timer", _mod("tiny_timer", "rtl/tiny_timer.sv"))
    files = [
        InputFile(rel_path="a.sv", sha256="1"),
        InputFile(rel_path="b.sv", sha256="2"),
    ]
    _, r1 = ingest([part], input_files=files, profile_digest="p1")
    _, r2 = ingest([part], input_files=list(reversed(files)), profile_digest="p1")
    _, r3 = ingest([part], input_files=files, profile_digest="p2")
    assert r1.build_inputs_hash == r2.build_inputs_hash
    assert r1.build_inputs_hash != r3.build_inputs_hash


def test_stats_group_entities_by_kind_source_and_block() -> None:
    port = PortEntity(key="port:tiny_timer.clk", name="clk", module="module:tiny_timer")
    part = SourcePart(
        source="rtl",
        block="block:timer",
        model=DesignModel.build([_mod("tiny_timer", "rtl/tiny_timer.sv"), port], []),
    )
    _model, result = ingest([part])
    assert result.stats.entities == 2
    assert result.stats.entities_by_kind == {"module": 1, "port": 1}
    assert result.stats.entities_by_source == {"rtl": 2}
    # The module carries a `block`; the port does not, so only the module is counted.
    assert result.stats.entities_by_block["block:timer"] == 1


def test_shared_module_ports_follow_the_owning_block() -> None:
    # `sync` lives in common/ and is also elaborated by pwm with another width: the owner
    # (common, by directory) wins for the module's port too, whatever the order.
    def port(width: int) -> PortEntity:
        return PortEntity(key="port:sync.i_d", name="i_d", width=width, module="module:sync")

    def part(block: str, width: int) -> SourcePart:
        mod = _mod("sync", "design/common/rtl/sync.sv")
        return SourcePart(source="rtl", block=block, model=DesignModel.build([mod, port(width)]))

    for parts in (
        [part("block:common", 1), part("block:pwm", 4)],
        [part("block:pwm", 4), part("block:common", 1)],
    ):
        model, result = ingest(parts)
        assert model.get("module:sync").block == "block:common"
        assert model.get("port:sync.i_d").width == 1
        assert [i.code for i in result.issues] == ["conflict"]

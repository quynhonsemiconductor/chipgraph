"""Tests for the `instance_of` support of `ingest` (D38).

These exercise the generic `ip_blocks` argument: declared instances become `instance_of`
relations, the same-name rule maps a block to itself without a self relation or a
duplicate block, and the three warning codes (`instance_unknown`, `instance_unmapped`,
`block_unknown`) each fire once in a stable order.
"""

from __future__ import annotations

from chipgraph.core.model.entities import (
    BlockEntity,
    ModuleEntity,
    RegisterEntity,
)
from chipgraph.core.model.ingest import SourcePart, ingest
from chipgraph.core.model.model import DesignModel


def _part(source: str, block: str | None, *entities: object, relations=()) -> SourcePart:
    model = DesignModel(
        entities={e.key: e for e in entities},  # type: ignore[attr-defined]
        relations=tuple(relations),
    )
    return SourcePart(source=source, block=block, model=model)


def _block(name: str, **attrs: object) -> BlockEntity:
    return BlockEntity(key=f"block:{name}", name=name, attrs=attrs)


def test_declared_instances_become_instance_of_relations() -> None:
    chip = _part(
        "chip",
        None,
        _block("timer_0"),
        _block("timer_1"),
    )
    model, result = ingest([chip], ip_blocks={"timer": ("timer_0", "timer_1")})

    ip = model.get("block:timer")
    assert isinstance(ip, BlockEntity)
    assert ip.attrs["role"] == "ip"

    rels = {(r.src, r.dst) for r in model.relations if r.kind == "instance_of"}
    assert rels == {("block:timer_0", "block:timer"), ("block:timer_1", "block:timer")}
    assert not any(i.code.startswith("instance_") for i in result.issues)


def test_same_name_rule_makes_no_self_relation_and_no_duplicate_block() -> None:
    chip = _part("chip", None, _block("pwm"))
    model, _ = ingest([chip], ip_blocks={"pwm": ()})

    # Only the one block; no second block:pwm was created.
    assert len(model.by_kind("block")) == 1
    assert model.get("block:pwm") is not None
    assert not [r for r in model.relations if r.kind == "instance_of"]


def test_ip_block_is_created_with_role_ip_when_missing() -> None:
    # dma_cfg exists on the memory map, but block:dma does not.
    chip = _part("chip", None, _block("dma_cfg"))
    model, result = ingest([chip], ip_blocks={"dma": ("dma_cfg",)})

    ip = model.get("block:dma")
    assert isinstance(ip, BlockEntity)
    assert ip.attrs["role"] == "ip"
    assert ("block:dma_cfg", "block:dma") in {
        (r.src, r.dst) for r in model.relations if r.kind == "instance_of"
    }
    # dma_cfg is now mapped, so no instance_unmapped for it.
    assert not [i for i in result.issues if i.code == "instance_unmapped"]


def test_instance_unknown_reported_once() -> None:
    chip = _part("chip", None, _block("timer_0"))
    _model, result = ingest([chip], ip_blocks={"timer": ("timer_0", "timer_1")})

    unknown = [i for i in result.issues if i.code == "instance_unknown"]
    assert len(unknown) == 1
    assert unknown[0].key == "block:timer_1"
    assert unknown[0].severity == "warning"


def test_instance_unmapped_reported_once() -> None:
    chip = _part("chip", None, _block("timer_0"), _block("mystery"))
    _model, result = ingest([chip], ip_blocks={"timer": ("timer_0",)})

    unmapped = [i for i in result.issues if i.code == "instance_unmapped"]
    assert [i.key for i in unmapped] == ["block:mystery"]
    assert unmapped[0].severity == "warning"


def test_block_unknown_reported_once_with_count_and_example() -> None:
    # Two registers point at a block that is not in the model.
    reg_a = RegisterEntity(key="register:ghost.A", name="A", block="block:ghost")
    reg_b = RegisterEntity(key="register:ghost.B", name="B", block="block:ghost")
    chip = _part("chip", None, reg_a, reg_b)
    _, result = ingest([chip], ip_blocks={})

    unknown = [i for i in result.issues if i.code == "block_unknown"]
    assert len(unknown) == 1
    assert unknown[0].key == "block:ghost"
    assert "2 " in unknown[0].message  # count of referrers
    assert "register:ghost.A" in unknown[0].message  # first example


def test_warning_order_is_stable() -> None:
    reg = RegisterEntity(key="register:ghost.A", name="A", block="block:ghost")
    chip = _part(
        "chip",
        None,
        _block("timer_0"),
        _block("mystery"),
        reg,
    )
    _, result = ingest([chip], ip_blocks={"timer": ("timer_0", "timer_1")})

    codes = [
        i.code
        for i in result.issues
        if i.code
        in {
            "block_unknown",
            "instance_unknown",
            "instance_unmapped",
        }
    ]
    # Sorted by code: block_unknown, instance_unknown, instance_unmapped.
    assert codes == ["block_unknown", "instance_unknown", "instance_unmapped"]


def test_without_ip_blocks_no_instance_relations_and_no_new_blocks() -> None:
    chip = _part("chip", None, _block("timer_0"), _block("timer_1"))
    model, result = ingest([chip])  # no ip_blocks

    assert not [r for r in model.relations if r.kind == "instance_of"]
    assert len(model.by_kind("block")) == 2
    assert not [i for i in result.issues if i.code.startswith(("instance_", "block_unknown"))]


def test_instances_are_order_independent() -> None:
    a = _part("chip", None, _block("timer_0"))
    b = _part("mas", "block:timer", _block("timer_1"))
    forward, _ = ingest([a, b], ip_blocks={"timer": ("timer_0", "timer_1")})
    reverse, _ = ingest([b, a], ip_blocks={"timer": ("timer_0", "timer_1")})

    def rels(m: DesignModel) -> set[tuple[str, str, str]]:
        return {(r.kind, r.src, r.dst) for r in m.relations}

    assert rels(forward) == rels(reverse)


def test_neighbors_and_impact_see_instance_of() -> None:
    from chipgraph.core.model.query import ModelQuery

    chip = _part("chip", None, _block("timer_0"), _block("timer_1"))
    model, _ = ingest([chip], ip_blocks={"timer": ("timer_0", "timer_1")})
    query = ModelQuery(model)

    # The instance sees its IP outgoing.
    out = query.neighbors("block:timer_0", direction="out")
    assert {"dst_key": "block:timer", "relation_kind": "instance_of"} in out.outgoing

    # impact from the IP does not filter out the new kind (it is not a downstream kind,
    # so we check neighbors incoming on the IP instead, which must include both instances).
    incoming = query.neighbors("block:timer", direction="in", relation="instance_of")
    srcs = {edge["src_key"] for edge in incoming.incoming}
    assert srcs == {"block:timer_0", "block:timer_1"}


def test_relate_module_owned_by_instance_still_referenced() -> None:
    # A module whose block is the instance key; block_unknown must not fire because the
    # instance block exists in the model.
    mod = ModuleEntity(key="module:timer_a", name="timer_a", block="block:timer_0")
    chip = _part("chip", None, _block("timer_0"))
    rtl = _part("rtl", "block:timer_0", mod)
    _, result = ingest([chip, rtl], ip_blocks={"timer": ("timer_0",)})
    assert not [i for i in result.issues if i.code == "block_unknown"]

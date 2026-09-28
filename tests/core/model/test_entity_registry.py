"""Tests for `EntityKinds`: the pack entity-kind registry, and `ExtEntity` fallback."""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import Field

from chipgraph.core.model.entities import BlockEntity, EntityBase, ExtEntity
from chipgraph.core.model.registry import DuplicateKindError, EntityKinds


class PinSpecEntity(EntityBase):
    """A pack-defined entity kind (analog pack, DESIGN.md 4.2)."""

    kind: Literal["pin_spec"] = "pin_spec"
    drive_strength_ma: int | None = Field(default=None)


def test_default_registry_knows_every_core_kind() -> None:
    from chipgraph.core.model.registry import DEFAULT_ENTITY_KINDS

    assert "block" in DEFAULT_ENTITY_KINDS.kinds()
    assert "register" in DEFAULT_ENTITY_KINDS.kinds()
    assert DEFAULT_ENTITY_KINDS.get("block") is BlockEntity


def test_unregistered_kind_parses_as_ext_entity() -> None:
    registry = EntityKinds()
    data = {"kind": "pin_spec", "key": "pin_spec:timer.pad0", "name": "pad0"}
    entity = registry.parse(data)
    assert isinstance(entity, ExtEntity)
    assert entity.kind == "pin_spec"


def test_registering_a_pack_kind_parses_to_the_typed_class() -> None:
    registry = EntityKinds()
    registry.register(PinSpecEntity)
    data = {
        "kind": "pin_spec",
        "key": "pin_spec:timer.pad0",
        "name": "pad0",
        "drive_strength_ma": 8,
    }
    entity = registry.parse(data)
    assert isinstance(entity, PinSpecEntity)
    assert entity.drive_strength_ma == 8


def test_registering_the_same_class_twice_is_fine() -> None:
    registry = EntityKinds()
    registry.register(PinSpecEntity)
    registry.register(PinSpecEntity)
    assert registry.get("pin_spec") is PinSpecEntity


def test_registering_a_conflicting_class_for_a_taken_kind_raises() -> None:
    registry = EntityKinds()

    class OtherBlock(EntityBase):
        kind: Literal["block"] = "block"

    with pytest.raises(DuplicateKindError):
        registry.register(OtherBlock)


def test_parse_requires_a_string_kind() -> None:
    registry = EntityKinds()
    with pytest.raises(ValueError, match="kind"):
        registry.parse({"key": "block:timer", "name": "timer"})

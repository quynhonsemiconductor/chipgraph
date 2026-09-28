"""Tests for `Relation`."""

from __future__ import annotations

import pytest

from chipgraph.core.model.relations import CORE_RELATION_KINDS, Relation


@pytest.mark.parametrize("kind", CORE_RELATION_KINDS)
def test_every_core_relation_kind_constructs_and_round_trips(kind: str) -> None:
    relation = Relation(kind=kind, src="block:timer", dst="module:tiny_timer")
    dumped = relation.model_dump(mode="json")
    assert Relation.model_validate(dumped) == relation


def test_relation_accepts_a_pack_defined_kind() -> None:
    relation = Relation(kind="analog/routes_to", src="pin_spec:timer.pad0", dst="block:timer")
    assert relation.kind == "analog/routes_to"


def test_relation_is_frozen_and_forbids_extra() -> None:
    relation = Relation(kind="contains", src="block:timer", dst="module:tiny_timer")
    with pytest.raises(Exception):  # noqa: B017
        relation.kind = "verifies"  # type: ignore[misc]
    with pytest.raises(Exception):  # noqa: B017
        Relation(kind="contains", src="a", dst="b", nope=1)  # type: ignore[call-arg]

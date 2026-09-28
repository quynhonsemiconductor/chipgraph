"""`DesignModel`: an immutable in-memory set of entities and relations.

This is the object every query in M1-02 (`model.block(...)`, `model.find(...)`, ...) is
built on top of. It never talks to disk: that is `ModelStore`'s job.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from chipgraph.core.model.entities import EntityBase
from chipgraph.core.model.provenance import Provenance
from chipgraph.core.model.relations import Relation


class ModelConflict(Exception):
    """Two entities share a `key` but disagree on content.

    Raised by `DesignModel.merge()` (and the `build`/`from_entities` constructors) when
    the same key appears with two different entities. Both provenances are attached so
    the caller can point a human at both sources.
    """

    def __init__(self, key: str, first: EntityBase, second: EntityBase) -> None:
        self.key = key
        self.first = first
        self.second = second
        super().__init__(
            f"conflicting entities for key {key!r}: {_describe(first)} vs {_describe(second)}"
        )


def _describe(entity: EntityBase) -> str:
    return f"kind={entity.kind!r} source={entity.source!r}"


@dataclass(frozen=True, slots=True)
class DesignModel:
    """An immutable snapshot of entities and relations.

    Construct with `DesignModel.build(...)`, not the dataclass constructor directly,
    so duplicate keys are always checked.
    """

    entities: Mapping[str, EntityBase] = field(default_factory=dict)
    relations: tuple[Relation, ...] = field(default_factory=tuple)

    @classmethod
    def build(
        cls, entities: Iterable[EntityBase], relations: Iterable[Relation] = ()
    ) -> DesignModel:
        """Build a `DesignModel`, raising `ModelConflict` on duplicate, differing keys."""
        by_key: dict[str, EntityBase] = {}
        for entity in entities:
            existing = by_key.get(entity.key)
            if existing is not None and existing != entity:
                raise ModelConflict(entity.key, existing, entity)
            by_key[entity.key] = entity
        return cls(entities=by_key, relations=tuple(relations))

    def get(self, key: str) -> EntityBase | None:
        """Return the entity for `key`, or None if it is not in the model."""
        return self.entities.get(key)

    def by_kind(self, kind: str) -> tuple[EntityBase, ...]:
        """Return every entity of a given `kind`, in insertion order."""
        return tuple(e for e in self.entities.values() if e.kind == kind)

    def children(self, key: str) -> tuple[EntityBase, ...]:
        """Return entities this entity `contains` or `instantiates`, in relation order."""
        hierarchy_kinds = ("contains", "instantiates")
        dsts = [r.dst for r in self.relations if r.src == key and r.kind in hierarchy_kinds]
        return tuple(self.entities[dst] for dst in dsts if dst in self.entities)

    def find(self, kind: str | None = None, **attrs: object) -> tuple[EntityBase, ...]:
        """Return entities matching `kind` (if given) and every `attrs` filter.

        Each keyword is matched first against a typed field on the entity, then against
        `entity.attrs`. An entity that lacks the field/attr entirely does not match.
        """
        result: list[EntityBase] = []
        for entity in self.entities.values():
            if kind is not None and entity.kind != kind:
                continue
            if _matches(entity, attrs):
                result.append(entity)
        return tuple(result)

    def get_relations(
        self,
        src: str | None = None,
        dst: str | None = None,
        kind: str | None = None,
    ) -> tuple[Relation, ...]:
        """Return relations filtered by any combination of `src`, `dst`, `kind`."""
        return tuple(
            r
            for r in self.relations
            if (src is None or r.src == src)
            and (dst is None or r.dst == dst)
            and (kind is None or r.kind == kind)
        )

    def merge(self, other: DesignModel) -> DesignModel:
        """Return a new `DesignModel` combining `self` and `other`.

        Raises `ModelConflict` if the same key maps to different entities in the two
        models. Relations are concatenated (duplicates are harmless and kept as-is).
        """
        by_key: dict[str, EntityBase] = dict(self.entities)
        for key, entity in other.entities.items():
            existing = by_key.get(key)
            if existing is not None and existing != entity:
                raise ModelConflict(key, existing, entity)
            by_key[key] = entity
        return DesignModel(entities=by_key, relations=self.relations + other.relations)

    def validate_relations(self) -> list[str]:
        """Return human-readable errors for every relation with a dangling endpoint.

        Does not raise: callers decide whether dangling relations should block anything.
        An empty list means every relation's `src` and `dst` resolve to a known entity.
        """
        errors: list[str] = []
        for i, relation in enumerate(self.relations):
            if relation.src not in self.entities:
                errors.append(
                    f"relation[{i}] kind={relation.kind!r}: "
                    f"src {relation.src!r} is not a known entity"
                )
            if relation.dst not in self.entities:
                errors.append(
                    f"relation[{i}] kind={relation.kind!r}: "
                    f"dst {relation.dst!r} is not a known entity"
                )
        return errors


def _matches(entity: EntityBase, attrs: Mapping[str, object]) -> bool:
    for name, expected in attrs.items():
        if hasattr(entity, name):
            if getattr(entity, name) != expected:
                return False
        elif name in entity.attrs:
            if entity.attrs[name] != expected:
                return False
        else:
            return False
    return True


__all__ = ["DesignModel", "ModelConflict", "Provenance"]

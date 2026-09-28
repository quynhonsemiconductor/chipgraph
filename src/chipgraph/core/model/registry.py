"""`EntityKinds`: the registry mapping an entity `kind` string to its pydantic class.

Core kinds (DESIGN.md 4.2) are registered by default. A pack that needs a new kind
(e.g. an analog pack adding `pin_spec`) registers its own frozen pydantic class that
subclasses `EntityBase`, without chipgraph.core knowing anything about it. A kind with
no registered class (because the reader does not have that pack installed, or has not
called `register` for it) still round-trips losslessly as `ExtEntity`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from chipgraph.core.model.entities import CORE_ENTITY_CLASSES, EntityBase, ExtEntity


class DuplicateKindError(ValueError):
    """Raised when registering a kind that is already registered to a different class."""


class EntityKinds:
    """A registry of entity `kind` -> pydantic class, seeded with the core kinds."""

    def __init__(self) -> None:
        self._classes: dict[str, type[EntityBase]] = {}
        for cls in CORE_ENTITY_CLASSES:
            self._classes[_kind_of(cls)] = cls

    def register(self, cls: type[EntityBase]) -> None:
        """Register a pack-defined entity class, keyed by its `kind` literal default."""
        kind = _kind_of(cls)
        existing = self._classes.get(kind)
        if existing is not None and existing is not cls:
            raise DuplicateKindError(
                f"kind {kind!r} is already registered to {existing.__qualname__}"
            )
        self._classes[kind] = cls

    def get(self, kind: str) -> type[EntityBase] | None:
        """Return the registered class for `kind`, or None if unregistered."""
        return self._classes.get(kind)

    def kinds(self) -> tuple[str, ...]:
        """Return every registered kind, sorted."""
        return tuple(sorted(self._classes))

    def parse(self, data: Mapping[str, Any]) -> EntityBase:
        """Build an entity from a plain dict, using the registered class for its `kind`.

        Falls back to `ExtEntity` when `kind` has no registered class, so entities from
        packs the reader does not know about still round-trip.
        """
        kind = data.get("kind")
        if not isinstance(kind, str):
            raise ValueError(f"entity data is missing a string 'kind': {data!r}")
        cls = self.get(kind)
        if cls is not None:
            return cls.model_validate(data)
        return ExtEntity.model_validate(data)


def _kind_of(cls: type[EntityBase]) -> str:
    field = cls.model_fields.get("kind")
    if field is None or not isinstance(field.default, str):
        raise TypeError(
            f"{cls.__qualname__} must define a 'kind: Literal[...]' field with a default"
        )
    return field.default


DEFAULT_ENTITY_KINDS = EntityKinds()
"""The process-wide default registry: core kinds only, until packs register more."""


def is_pydantic_entity_class(obj: Any) -> bool:
    """Return True if `obj` looks like a valid entity class to register."""
    return isinstance(obj, type) and issubclass(obj, BaseModel) and issubclass(obj, EntityBase)

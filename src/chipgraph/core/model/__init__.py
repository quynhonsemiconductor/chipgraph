"""The Design Model (DESIGN.md 4): a typed knowledge model of a chip design.

`chipgraph.core.model` is the schema and SQLite-backed store (M1-01). It knows nothing
about any specific chip project, bus or tool: entity kinds like `port` and `register`
are generic hardware concepts. The query API (`model.block(...)`, `model.find(...)`,
`model.trace(...)`) is built on top of this in M1-02.
"""

from chipgraph.core.model.entities import (
    CORE_ENTITY_CLASSES,
    BlockEntity,
    ClockEntity,
    DecisionEntity,
    EarsParts,
    EntityBase,
    ExtEntity,
    FieldEntity,
    InterfaceEntity,
    InterruptEntity,
    MemoryRegionEntity,
    ModuleEntity,
    OpenItemEntity,
    ParameterEntity,
    PortEntity,
    ProjectEntity,
    RegisterEntity,
    RequirementEntity,
    ResetEntity,
    TestEntity,
)
from chipgraph.core.model.json_value import JSONValue
from chipgraph.core.model.keys import InvalidKeyError, key_kind, make_key, parse_key
from chipgraph.core.model.model import DesignModel, ModelConflict
from chipgraph.core.model.provenance import Provenance
from chipgraph.core.model.registry import DEFAULT_ENTITY_KINDS, DuplicateKindError, EntityKinds
from chipgraph.core.model.relations import CORE_RELATION_KINDS, CoreRelationKind, Relation
from chipgraph.core.model.store import ModelStore, ModelStoreError, SearchHit, default_model_db_path

__all__ = [
    "CORE_ENTITY_CLASSES",
    "CORE_RELATION_KINDS",
    "DEFAULT_ENTITY_KINDS",
    "BlockEntity",
    "ClockEntity",
    "CoreRelationKind",
    "DecisionEntity",
    "DesignModel",
    "DuplicateKindError",
    "EarsParts",
    "EntityBase",
    "EntityKinds",
    "ExtEntity",
    "FieldEntity",
    "InterfaceEntity",
    "InterruptEntity",
    "InvalidKeyError",
    "JSONValue",
    "MemoryRegionEntity",
    "ModelConflict",
    "ModelStore",
    "ModelStoreError",
    "ModuleEntity",
    "OpenItemEntity",
    "ParameterEntity",
    "PortEntity",
    "ProjectEntity",
    "Provenance",
    "RegisterEntity",
    "Relation",
    "RequirementEntity",
    "ResetEntity",
    "SearchHit",
    "TestEntity",
    "default_model_db_path",
    "key_kind",
    "make_key",
    "parse_key",
]

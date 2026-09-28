"""JSON Schema sources for the Design Model's entities and relations.

`chipgraph.core.contracts.export` imports `MODEL_SCHEMA_MODELS` from here and writes
them into `schemas/model/`, so `make schemas` / `schemas-check` cover the Design Model
alongside the core contracts.
"""

from __future__ import annotations

from pydantic import BaseModel

from chipgraph.core.model.entities import CORE_ENTITY_CLASSES, ExtEntity
from chipgraph.core.model.relations import Relation

MODEL_SCHEMA_MODELS: tuple[type[BaseModel], ...] = (*CORE_ENTITY_CLASSES, ExtEntity, Relation)
"""Every pydantic model that should get a JSON Schema file under `schemas/model/`."""

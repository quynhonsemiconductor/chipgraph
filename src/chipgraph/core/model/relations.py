"""Relations between Design Model entities (DESIGN.md 4.2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.model.json_value import JSONValue
from chipgraph.core.model.provenance import Provenance

CoreRelationKind = Literal[
    "implements",
    "verifies",
    "connects",
    "derives_from",
    "contains",
    "instantiates",
]
"""The relation kinds DESIGN.md names, plus `contains`/`instantiates` for hierarchy.

DESIGN.md 4.2 only lists `implements`, `verifies`, `connects`, `derives_from`. Building
`DesignModel.children()` needs a way to walk hierarchy (block -> module -> port,
module -> module instance), so this task adds `contains` (a owns b, e.g. block contains
module) and `instantiates` (a module instantiates another module) as core kinds. Packs
may add further relation kinds; `Relation.kind` is a plain `str` so those round-trip too.
"""

CORE_RELATION_KINDS: tuple[str, ...] = (
    "implements",
    "verifies",
    "connects",
    "derives_from",
    "contains",
    "instantiates",
)


class Relation(BaseModel):
    """A directed edge between two entities, identified by their model keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    kind: str = Field(
        description=(
            "The relation kind: one of CORE_RELATION_KINDS, or a pack-defined kind string."
        )
    )
    src: str = Field(description="Model key of the source entity.")
    dst: str = Field(description="Model key of the destination entity.")
    source: Provenance = Field(
        default_factory=Provenance, description="Where this fact was learned from."
    )
    attrs: dict[str, JSONValue] = Field(
        default_factory=dict, description="Open-ended extra facts not covered by typed fields."
    )

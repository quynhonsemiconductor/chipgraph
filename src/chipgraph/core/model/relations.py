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
    "instance_of",
]
"""The relation kinds DESIGN.md names, plus `contains`/`instantiates`/`instance_of`.

DESIGN.md 4.2 only lists `implements`, `verifies`, `connects`, `derives_from`. Building
`DesignModel.children()` needs a way to walk hierarchy (block -> module -> port,
module -> module instance), so this adds `contains` (a owns b, e.g. block contains
module) and `instantiates` (a module instantiates another module) as core kinds.
`instance_of` links a block that is an instance on the memory map to the IP block it is a
copy of (`block:<instance>` -> `block:<ip>`, D38). Packs may add further relation kinds;
`Relation.kind` is a plain `str` so those round-trip too.
"""

CORE_RELATION_KINDS: tuple[str, ...] = (
    "implements",
    "verifies",
    "connects",
    "derives_from",
    "contains",
    "instantiates",
    "instance_of",
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

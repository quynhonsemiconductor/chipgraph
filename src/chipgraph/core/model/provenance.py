"""Provenance: where a Design Model fact came from."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.contracts.types import Sha256


class Provenance(BaseModel):
    """Records where a single entity or relation fact was learned from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    file: str | None = Field(
        default=None, description="POSIX path the fact was read from, relative to the repo root."
    )
    line: int | None = Field(default=None, ge=1, description="The 1-based line number, if any.")
    extractor: str | None = Field(
        default=None, description="The extractor or format adapter id that produced this fact."
    )
    artifact: Sha256 | None = Field(
        default=None, description="Content hash of the source artifact, if known."
    )

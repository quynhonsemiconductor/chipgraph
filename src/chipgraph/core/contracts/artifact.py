"""Artifacts: the nodes of the build graph."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chipgraph.core.contracts._paths import validate_relative_path
from chipgraph.core.contracts.types import ArtifactKind, DataLabel, RuleId, Sha256


class ArtifactRef(BaseModel):
    """A reference to an artifact, identifying it without describing its content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    repo: str = Field(
        default=".", description="The workspace member (repo) id this artifact belongs to."
    )
    kind: ArtifactKind = Field(description="The kind of artifact this is, e.g. rtl, spec, report.")
    path: str | None = Field(
        default=None,
        description="POSIX path to the artifact, relative to the repo root, when it is file-based.",
    )
    model_key: str | None = Field(
        default=None,
        description="Key into the Design Model when this artifact is model-based, not a file.",
    )
    label: DataLabel = Field(
        default="internal", description="Data sensitivity label: public, internal, or nda."
    )

    @model_validator(mode="after")
    def _check_exactly_one_locator(self) -> Self:
        if (self.path is None) == (self.model_key is None):
            raise ValueError("ArtifactRef needs exactly one of 'path' or 'model_key' to be set")
        if self.path is not None:
            validate_relative_path(self.path, field_name="path")
        return self


class ProducedBy(BaseModel):
    """Records which rule instance and run produced an artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: RuleId = Field(description="The id of the rule that produced this artifact.")
    run_id: str = Field(description="The id of the run in which this artifact was produced.")


class Artifact(BaseModel):
    """A concrete artifact: a reference plus its content hash and provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    ref: ArtifactRef = Field(description="The reference identifying this artifact.")
    content_hash: Sha256 = Field(description="SHA-256 hash of the artifact's content.")
    produced_by: ProducedBy | None = Field(
        default=None,
        description="Which rule and run produced this; None if human-written or pre-existing.",
    )
    inputs_hash: Sha256 | None = Field(
        default=None,
        description="Hash of the inputs this was produced from; None when not produced by a rule.",
    )

"""Gates: human approval points, and the approvals recorded against them."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from chipgraph.core.contracts.artifact import ArtifactRef
from chipgraph.core.contracts.types import Sha256


class GateSpec(BaseModel):
    """The specification of a gate: which artifacts need human approval, and how."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    id: str = Field(description="The id of this gate.")
    artifacts: tuple[ArtifactRef, ...] = Field(
        min_length=1, description="The artifacts this gate covers."
    )
    approvers: tuple[str, ...] = Field(
        default=(), description="Identities allowed to approve this gate."
    )
    mode: Literal["pr", "file"] = Field(default="file", description="How approval is recorded.")


class Approval(BaseModel):
    """A recorded decision against a gate, pinned to the artifact hashes it approved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    gate_id: str = Field(description="The id of the gate this approval is for.")
    by: str = Field(description="The identity that made this decision.")
    at: AwareDatetime = Field(description="When this decision was made.")
    artifact_hashes: dict[str, Sha256] = Field(
        min_length=1,
        description="Hashes the decision was made against, keyed by '<repo>:<path or model_key>'.",
    )
    decision: Literal["approve", "reject", "baseline", "waive"] = Field(
        description="The decision that was made."
    )
    note: str = Field(default="", description="An optional free-text note explaining the decision.")

    def is_current(self, current: Mapping[str, str]) -> bool:
        """Return True only if every hash this approval covers still matches `current`."""
        return all(current.get(key) == expected for key, expected in self.artifact_hashes.items())

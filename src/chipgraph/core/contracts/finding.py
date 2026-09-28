"""Findings: problems found in the design, at one of 5 layers of certainty (DESIGN.md 4.8).

A `Finding` is the common shape every check, Critic (AI) pass, z3, or formal run reports
issues in. Evidence is mandatory: a finding with nothing pointing at a file:line or a
Design Model key is not a finding. AI sources (anything other than the known
deterministic ones: `check:*`, `z3`, `formal`, `lint`) can never report `severity="error"`,
so an unreliable finding never blocks a build on its own (DESIGN.md 4.8).

A finding's identity across runs is its `fingerprint`: a stable hash of its source, claim,
and evidence locations, independent of `first_seen`/`last_seen`/`confidence`. The same
problem found again by the same source is the same finding, not a new one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from chipgraph.core.contracts._paths import validate_relative_path
from chipgraph.core.contracts.artifact import ArtifactRef
from chipgraph.core.contracts.types import Sha256

FindingSeverity = Literal["error", "warning", "question"]
"""How serious a finding is. Only deterministic layers may use `error` (DESIGN.md 4.8)."""

FindingStatus = Literal["open", "fixed", "waived"]
"""A finding's lifecycle: newly (re)detected, no longer detected, or waived by a person."""

_DETERMINISTIC_SOURCES = frozenset({"z3", "formal", "lint"})
_FINGERPRINT_ID_CHARS = 8


def is_ai_source(source: str) -> bool:
    """Whether `source` is an AI source (Critic or similar), never a deterministic one.

    Deterministic sources are `check:<id>`, `z3`, `formal`, and `lint`. Anything else
    -- `critic`, or any future AI source id -- is treated as AI, conservatively: an
    unrecognized source is never assumed safe enough to block a build.
    """
    if source.startswith("check:"):
        return False
    return source not in _DETERMINISTIC_SOURCES


def _artifact_key(ref: ArtifactRef) -> str:
    return f"{ref.repo}:{ref.path if ref.path is not None else ref.model_key}"


def _evidence_key(evidence: Evidence) -> str:
    if evidence.file is not None:
        return f"{evidence.file}:{evidence.line if evidence.line is not None else '-'}"
    return f"model:{evidence.model_key}"


def compute_fingerprint(source: str, claim: str, evidence: Iterable[Evidence]) -> str:
    """A stable sha256 hex digest identifying a finding across runs.

    Built from `source`, `claim`, and the sorted set of evidence locations
    (`file:line` or `model:<key>`). Deliberately excludes anything that can change
    run to run without the underlying problem changing: `first_seen`, `last_seen`,
    `confidence`, `suggestion`, `artifacts`/`artifact_hashes`, `run_id`.
    """
    digest = hashlib.sha256()
    digest.update(source.encode("utf-8"))
    digest.update(b"\0")
    digest.update(claim.encode("utf-8"))
    digest.update(b"\0")
    for loc in sorted(_evidence_key(e) for e in evidence):
        digest.update(loc.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


class Evidence(BaseModel):
    """A pointer to where a finding was observed: a file:line, or a Design Model key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str | None = Field(default=None, description="POSIX path, relative to the repo root.")
    line: int | None = Field(default=None, ge=1, description="1-based line number, if any.")
    model_key: str | None = Field(
        default=None, description="Key into the Design Model, when this evidence is not a file."
    )
    note: str = Field(default="", description="An optional free-text note about this evidence.")

    @model_validator(mode="after")
    def _check_exactly_one_locator(self) -> Self:
        if (self.file is None) == (self.model_key is None):
            raise ValueError("Evidence needs exactly one of 'file' or 'model_key' to be set")
        if self.file is not None:
            validate_relative_path(self.file, field_name="file")
        elif self.line is not None:
            raise ValueError("Evidence 'line' requires 'file' to be set")
        return self


class Finding(BaseModel):
    """A single detected problem, at one of the 5 layers of DESIGN.md 4.8."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    id: str = Field(default="", description="'F-<first 8 chars of fingerprint>'; computed.")
    fingerprint: str = Field(
        default="", description="Stable identity hash of source+claim+evidence; computed."
    )
    layer: int = Field(ge=1, le=5, description="Which of the 5 layers of DESIGN.md 4.8 found this.")
    severity: FindingSeverity = Field(description="How serious this finding is.")
    source: str = Field(
        description="Where this came from: 'check:<id>', 'critic', 'z3', 'formal', or 'lint'."
    )
    evidence: tuple[Evidence, ...] = Field(
        min_length=1, description="Where this was observed; at least one is required."
    )
    claim: str = Field(description="What is wrong, in plain language.")
    suggestion: str = Field(default="", description="A suggested fix, if any.")
    confidence: float = Field(default=1.0, ge=0, le=1, description="Confidence in this finding.")
    status: FindingStatus = Field(default="open", description="This finding's lifecycle status.")
    artifacts: tuple[ArtifactRef, ...] = Field(
        default=(), description="Artifacts this finding concerns; what a waiver binds to."
    )
    artifact_hashes: dict[str, Sha256] = Field(
        default={},
        description="Content hashes of `artifacts` at detection time, keyed by "
        "'<repo>:<path or model_key>'.",
    )
    first_seen: AwareDatetime = Field(description="When this finding (fingerprint) was first seen.")
    last_seen: AwareDatetime = Field(description="When this finding was last (re)detected.")
    run_id: str | None = Field(default=None, description="The run that (re)detected this finding.")

    @model_validator(mode="after")
    def _fill_fingerprint_and_id(self) -> Self:
        if not self.fingerprint:
            object.__setattr__(
                self, "fingerprint", compute_fingerprint(self.source, self.claim, self.evidence)
            )
        if not self.id:
            object.__setattr__(self, "id", f"F-{self.fingerprint[:_FINGERPRINT_ID_CHARS]}")
        return self

    @model_validator(mode="after")
    def _check_ai_source_not_error(self) -> Self:
        if self.severity == "error" and is_ai_source(self.source):
            raise ValueError(
                f"source {self.source!r} is an AI source; it cannot report severity='error' "
                "(DESIGN.md 4.8: AI findings never block, only deterministic layers can)"
            )
        return self

    @model_validator(mode="after")
    def _check_artifact_hashes_match_artifacts(self) -> Self:
        valid_keys = {_artifact_key(ref) for ref in self.artifacts}
        for key in self.artifact_hashes:
            if key not in valid_keys:
                raise ValueError(
                    f"artifact_hashes key {key!r} does not match any entry in 'artifacts'"
                )
        return self


__all__ = [
    "Evidence",
    "Finding",
    "FindingSeverity",
    "FindingStatus",
    "compute_fingerprint",
    "is_ai_source",
]

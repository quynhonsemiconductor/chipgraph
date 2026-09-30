"""Planning `chipgraph baseline` (DESIGN.md 6.4): what to record for an existing project.

A project that already carries reviewed specs, RTL and contracts (merged through PRs)
has no chipgraph decisions yet, so the very first build would stop at every spec gate.
`baseline` proposes treating the artifacts on the main branch as *approved at their
current hash*, records one `baseline` decision per undecided gate, and notes the open
findings so they are recorded without blocking the build.

This module is the generic, side-effect-free half of that command: given the gates in
the graph, the artifacts each gate covers, and per-artifact version-control facts, it
produces a deterministic `BaselinePlan`. It knows nothing about git, the review store or
the finding store (the app layer supplies those facts and applies the plan); it only
depends on `chipgraph.core.contracts` and is checked under `mypy --strict`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.contracts import ArtifactRef, RuleInstance

__all__ = [
    "ArtifactStatus",
    "BaselineArtifact",
    "BaselineGate",
    "BaselinePlan",
    "CommitInfo",
    "GateInput",
    "VcsFacts",
    "clean_refs_for_gate",
    "gate_inputs_from_instances",
    "plan_baseline",
]

ArtifactStatus = Literal["clean", "dirty", "untracked"]
"""How an artifact stands relative to the main branch.

- ``clean``: tracked and unmodified in the working tree -> can be baselined.
- ``dirty``: tracked but modified in the working tree -> not baselined.
- ``untracked``: not tracked by version control -> not baselined.
"""


class CommitInfo(BaseModel):
    """The identifying facts of a commit, as shown in a baseline listing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    short_sha: str = Field(description="The commit's abbreviated sha.")
    date: str = Field(description="The commit's author date, ISO-8601 (YYYY-MM-DD).")
    subject: str = Field(description="The commit's subject line.")
    is_merge: bool = Field(default=False, description="Whether this is a merge/PR commit.")


class VcsFacts(Protocol):
    """The version-control facts `plan_baseline` needs about one repo-relative path.

    The app layer implements this over the git adapter and the working tree; the core
    only reads it, so it never shells out or imports an adapter.
    """

    def sha256(self, path: str) -> str | None:
        """Current content sha256 (hex) of `path`, or `None` if it is not on disk."""
        ...

    def is_tracked(self, path: str) -> bool:
        """Whether `path` is tracked by version control on the current branch."""
        ...

    def is_dirty(self, path: str) -> bool:
        """Whether `path` is modified in the working tree relative to the current commit."""
        ...

    def last_commit(self, path: str) -> CommitInfo | None:
        """The last commit that touched `path`, or `None` if there is none."""
        ...


class GateInput(BaseModel):
    """One gate to plan a baseline for, with the artifacts it covers.

    `artifacts` are the gated instance's file-based inputs (the things a person
    reviewed); `already_decided` says the gate already has a current decision, so the
    baseline skips it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_id: str = Field(description="The formatted gate id, e.g. 'spec:timer'.")
    instance_id: str = Field(description="The rule instance the gate is evaluated for.")
    artifacts: tuple[ArtifactRef, ...] = Field(
        default=(), description="The gate's covered artifacts (the instance's file inputs)."
    )
    already_decided: bool = Field(
        default=False, description="Whether the gate already has a current decision."
    )


class BaselineArtifact(BaseModel):
    """One artifact covered by a gate, with its version-control facts for the listing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(description="The artifact's repo-relative path.")
    sha256: str | None = Field(
        default=None, description="Current content sha256 (hex), or None if missing on disk."
    )
    status: ArtifactStatus = Field(description="'clean', 'dirty', or 'untracked'.")
    last_commit: CommitInfo | None = Field(
        default=None, description="The last commit that touched this artifact, if any."
    )

    @property
    def short_sha256(self) -> str:
        """The first 12 chars of the content sha, or '-' when the file is missing."""
        return self.sha256[:12] if self.sha256 else "-"

    @property
    def baselineable(self) -> bool:
        """Whether this artifact is on the main branch and so can be baselined."""
        return self.status == "clean" and self.sha256 is not None


class BaselineGate(BaseModel):
    """A gate in the plan: its artifacts, whether it is skipped, and what will be baselined."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_id: str = Field(description="The formatted gate id.")
    instance_id: str = Field(description="The rule instance the gate is evaluated for.")
    already_decided: bool = Field(description="Whether the gate already has a current decision.")
    artifacts: tuple[BaselineArtifact, ...] = Field(
        default=(), description="The gate's covered artifacts, sorted by path."
    )

    @property
    def clean_artifacts(self) -> tuple[BaselineArtifact, ...]:
        """The gate's clean, tracked artifacts: what a baseline decision would pin."""
        return tuple(a for a in self.artifacts if a.baselineable)

    @property
    def will_baseline(self) -> bool:
        """Whether ``--confirm`` would record a decision for this gate.

        True only when the gate is undecided and has at least one clean, tracked
        artifact to pin the decision to.
        """
        return not self.already_decided and bool(self.clean_artifacts)


class BaselinePlan(BaseModel):
    """The full, deterministic plan a `baseline` run acts on (or just prints, when dry)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gates: tuple[BaselineGate, ...] = Field(
        default=(), description="Every gate found, sorted by gate id then instance id."
    )
    open_findings: int = Field(
        default=0, ge=0, description="Count of currently open findings, recorded at baseline."
    )

    @property
    def gates_to_baseline(self) -> tuple[BaselineGate, ...]:
        """The undecided gates with clean artifacts that ``--confirm`` will record."""
        return tuple(g for g in self.gates if g.will_baseline)

    @property
    def skipped_gates(self) -> tuple[BaselineGate, ...]:
        """The gates a baseline skips: already decided."""
        return tuple(g for g in self.gates if g.already_decided)

    @property
    def dirty_artifacts(self) -> tuple[BaselineArtifact, ...]:
        """Every distinct dirty/untracked artifact across all gates, sorted by path.

        These are *not* baselined: a baseline is only for what is on the main branch.
        """
        seen: dict[str, BaselineArtifact] = {}
        for gate in self.gates:
            for artifact in gate.artifacts:
                if not artifact.baselineable and artifact.path not in seen:
                    seen[artifact.path] = artifact
        return tuple(seen[path] for path in sorted(seen))


def _file_inputs(instance: RuleInstance) -> tuple[ArtifactRef, ...]:
    """The instance's file-based inputs: what a gate over it covers (DESIGN.md 6.1)."""
    return tuple(ref for ref in instance.inputs if ref.path is not None)


def gate_inputs_from_instances(
    gated: Iterable[tuple[str, RuleInstance, bool]],
) -> list[GateInput]:
    """Build `GateInput`s from `(gate_id, instance, already_decided)` triples.

    A small convenience for the app layer: each gate covers its instance's file-based
    inputs. Instances with no file inputs still produce a `GateInput` (with no
    artifacts), so the listing can show a gate that cannot be baselined.
    """
    return [
        GateInput(
            gate_id=gate_id,
            instance_id=instance.instance_id,
            artifacts=_file_inputs(instance),
            already_decided=already_decided,
        )
        for gate_id, instance, already_decided in gated
    ]


def _artifact_status(path: str, facts: VcsFacts) -> ArtifactStatus:
    if not facts.is_tracked(path):
        return "untracked"
    if facts.is_dirty(path):
        return "dirty"
    return "clean"


def plan_baseline(
    gates: Iterable[GateInput], facts: VcsFacts, *, open_findings: int = 0
) -> BaselinePlan:
    """Turn gate inputs and version-control facts into a deterministic `BaselinePlan`.

    For every gate, each covered artifact is resolved to its current sha256, its status
    ('clean'/'dirty'/'untracked') and the last commit that touched it. Gates are sorted
    by `(gate_id, instance_id)` and artifacts by path, so the plan (and any listing or
    JSON built from it) is stable regardless of input order.
    """
    planned: list[BaselineGate] = []
    for gate in sorted(gates, key=lambda g: (g.gate_id, g.instance_id)):
        artifacts = tuple(
            _plan_artifact(ref, facts)
            for ref in sorted(gate.artifacts, key=_ref_path)
            if ref.path is not None
        )
        planned.append(
            BaselineGate(
                gate_id=gate.gate_id,
                instance_id=gate.instance_id,
                already_decided=gate.already_decided,
                artifacts=artifacts,
            )
        )
    return BaselinePlan(gates=tuple(planned), open_findings=open_findings)


def _ref_path(ref: ArtifactRef) -> str:
    return ref.path or ""


def _plan_artifact(ref: ArtifactRef, facts: VcsFacts) -> BaselineArtifact:
    assert ref.path is not None  # guarded by the caller
    path = ref.path
    status = _artifact_status(path, facts)
    return BaselineArtifact(
        path=path,
        sha256=facts.sha256(path),
        status=status,
        last_commit=facts.last_commit(path),
    )


def clean_refs_for_gate(
    gate: BaselineGate, by_path: Mapping[str, ArtifactRef]
) -> tuple[ArtifactRef, ...]:
    """The `ArtifactRef`s of a gate's clean artifacts, looked up in `by_path`.

    The app layer keeps the original `ArtifactRef`s (with repo and label) keyed by
    path; this returns the ones a baseline decision should pin, in the plan's order.
    """
    refs: list[ArtifactRef] = []
    for artifact in gate.clean_artifacts:
        ref = by_path.get(artifact.path)
        if ref is not None:
            refs.append(ref)
    return tuple(refs)

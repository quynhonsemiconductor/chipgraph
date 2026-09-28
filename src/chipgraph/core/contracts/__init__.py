"""Core contracts: the pydantic models every other part of chipgraph builds against.

`chipgraph.core.contracts` knows nothing about chips or tools: no project, bus, PDK or
tool names appear here.
"""

from chipgraph.core.contracts.agent import AgentResult, Decision
from chipgraph.core.contracts.artifact import Artifact, ArtifactRef, ProducedBy
from chipgraph.core.contracts.check import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.event import Event, EventType, RunManifest
from chipgraph.core.contracts.gate import Approval, GateSpec
from chipgraph.core.contracts.rule import Budget, InputSpec, RuleInstance, RuleSpec
from chipgraph.core.contracts.types import (
    ArtifactKind,
    CheckStatus,
    DataLabel,
    FailureLabel,
    ModelTier,
    RuleId,
    RuleKind,
    Severity,
    Sha256,
)

__all__ = [
    "AgentResult",
    "Approval",
    "Artifact",
    "ArtifactKind",
    "ArtifactRef",
    "Budget",
    "CheckResult",
    "CheckSpec",
    "CheckStatus",
    "DataLabel",
    "Decision",
    "Event",
    "EventType",
    "FailureLabel",
    "GateSpec",
    "InputSpec",
    "Issue",
    "ModelTier",
    "ProducedBy",
    "RuleId",
    "RuleInstance",
    "RuleKind",
    "RuleSpec",
    "RunManifest",
    "Severity",
    "Sha256",
]

TOP_LEVEL_MODELS = (
    ArtifactRef,
    Artifact,
    RuleSpec,
    RuleInstance,
    CheckSpec,
    CheckResult,
    GateSpec,
    Approval,
    Event,
    RunManifest,
    AgentResult,
    Decision,
)
"""Top-level models: each gets its own file in `schemas/` (see export.py)."""

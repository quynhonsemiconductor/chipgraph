"""Shared type aliases and literals used across the core contracts."""

from typing import Annotated, Literal

from pydantic import StringConstraints

DataLabel = Literal["public", "internal", "nda"]
"""How sensitive an artifact's content is: public, internal-only, or NDA-restricted."""

ArtifactKind = Literal[
    "spec",
    "rtl",
    "tb",
    "sva",
    "report",
    "plan",
    "model",
    "doc",
    "diagram",
    "script",
    "config",
    "other",
]
"""The kind of thing an artifact is, independent of its file format."""

RuleKind = Literal["gen", "agent", "human", "import"]
"""How a rule produces its outputs: a generator, an AI agent, a human, or an import."""

ModelTier = Literal["small", "medium", "large"]
"""A relative capability/cost tier for an LLM used by an agent rule."""

CheckStatus = Literal["pass", "fail", "error", "skipped"]
"""The outcome of running a check: it passed, it failed, it errored, or it was skipped."""

Severity = Literal["error", "warning", "info"]
"""How serious a single check issue is."""

FailureLabel = Literal["context", "constraint", "verification", "planning", "infra"]
"""A coarse category for why a rule failed, used for run analytics."""

RuleId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_]*$")]
"""A namespaced rule identifier of the form ``<pack>/<name>``, e.g. ``digital-rtl/rtl_module``."""

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""A lowercase hex-encoded SHA-256 digest."""

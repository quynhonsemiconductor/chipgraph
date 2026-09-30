"""Result models for `chipgraph learn` (DESIGN.md 8.6 V1).

`learn` reads an existing repository and infers the conventions a `.chipgraph.yml`
declares: where files live (`layout`), how identifiers are named (`naming`), the common
file header, the filelist style, and the document template. It reports each inference as
evidence with a coverage statistic ("97% of ports match `^(i|o|io)_`") rather than as a
silent guess, so a human reviews the draft before it is used.

These models are the machine-readable form of that report. They live outside
`chipgraph.core` on purpose: `learn` reasons about chips, tools and file layouts, which
`core` must not.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal[
    "module",
    "port",
    "parameter",
    "localparam",
    "enum_value",
    "instance",
    "signal",
    "memory",
    "genvar",
]
"""The identifier kinds `learn` infers a naming pattern for (from `_naming_pyslang`)."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Coverage(_Model):
    """How many observed items an inferred pattern matches: `matched` of `total`."""

    matched: int = Field(ge=0, description="Number of observed items the pattern matches.")
    total: int = Field(ge=0, description="Total number of observed items of this kind.")

    @property
    def ratio(self) -> float:
        """`matched / total`, or `1.0` when there is nothing to cover."""
        return 1.0 if self.total == 0 else self.matched / self.total

    @property
    def percent(self) -> int:
        """Coverage as a whole-number percentage, e.g. `97`."""
        return round(self.ratio * 100)


class LayoutRule(_Model):
    """An inferred `layout` entry: an artifact kind and its path template, with coverage."""

    kind: str = Field(description="The artifact kind, e.g. 'rtl', 'filelist', 'spec', 'tb'.")
    template: str = Field(description="The inferred path template, e.g. 'rtl/{block}.sv'.")
    coverage: Coverage = Field(description="Files of this kind the template matches.")
    examples: tuple[str, ...] = Field(
        default=(), description="A few example paths the template was inferred from."
    )


class NamingRule(_Model):
    """An inferred naming pattern for one identifier kind, with coverage and evidence."""

    kind: Kind = Field(description="The identifier kind this rule constrains.")
    pattern: str = Field(description="A regex the identifier must fully match to pass.")
    coverage: Coverage = Field(description="Declared identifiers of this kind the pattern matches.")
    emitted: bool = Field(
        description="Whether coverage met the threshold, so this is a rule (not an observation)."
    )
    description: str = Field(
        description="Human-readable summary, e.g. '97% of ports match `^(i|o|io)_`'."
    )
    examples: tuple[str, ...] = Field(
        default=(), description="A few example identifiers the pattern was inferred from."
    )


class Observation(_Model):
    """A learned fact reported for the reviewer, not emitted as a check (e.g. the header)."""

    topic: str = Field(description="What was observed, e.g. 'header', 'filelist', 'doc'.")
    summary: str = Field(description="A human-readable one-line summary of the observation.")
    coverage: Coverage | None = Field(
        default=None, description="Coverage, when the observation is a ratio."
    )
    detail: dict[str, Any] = Field(
        default_factory=dict, description="Structured detail, e.g. the header text or headings."
    )


class LearnResult(_Model):
    """The full result of `learn`: a draft profile plus the evidence behind it (DESIGN 8.6)."""

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    root: str = Field(description="The repository root that was learned, as given.")
    project: str = Field(description="The inferred project name (the root directory name).")
    threshold: float = Field(
        description="Minimum coverage for a naming pattern to be emitted as a rule."
    )
    blocks: tuple[str, ...] = Field(
        default=(), description="Block names inferred from the repeated directory level."
    )
    layout: tuple[LayoutRule, ...] = Field(
        default=(), description="Inferred layout templates, one per artifact kind."
    )
    naming: tuple[NamingRule, ...] = Field(
        default=(), description="Inferred naming patterns, per identifier kind."
    )
    observations: tuple[Observation, ...] = Field(
        default=(), description="Learned facts reported for review (header, filelist, doc)."
    )
    vendor_paths: tuple[str, ...] = Field(
        default=(), description="Vendor/third-party path globs to exempt from checks."
    )
    filelist_style: Literal["filelist", "root", "none"] = Field(
        default="none",
        description=(
            "How filelist paths resolve: 'filelist' (relative to the filelist's own dir, "
            "which the `filelist` check enforces), 'root' (relative to the repo root, which "
            "it cannot yet), or 'none' (no filelists found)."
        ),
    )
    naming_rules_document: str = Field(
        default="learned", description="The `document` field written into the naming-rules file."
    )

    @property
    def emitted_naming(self) -> tuple[NamingRule, ...]:
        """The naming rules whose coverage met the threshold."""
        return tuple(r for r in self.naming if r.emitted)


__all__ = [
    "Coverage",
    "Kind",
    "LayoutRule",
    "LearnResult",
    "NamingRule",
    "Observation",
]

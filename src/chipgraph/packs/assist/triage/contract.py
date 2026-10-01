"""The `/triage` data contract: the classes a failing log is sorted into, and the report.

A `TriageReport` says which side has to change to fix a failing lint, simulation or check
run (`label`: `infra`, `rtl`, `tb` or `spec`), who decided it (`backend`: a deterministic
rule, or the small or large model tier of `decide()`), how sure it is, a short summary of
what failed and where, and what to do next (DESIGN 5.2: infra -> fix the environment and
retry without counting a try; rtl/tb -> fix that file; spec -> ask the spec owner).

A model's label is advice: it never blocks a build (DESIGN 4.8), and `low_confidence`
marks an answer under its tier's threshold.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.contracts import Issue

TriageLabel = Literal["infra", "rtl", "tb", "spec"]
"""Which side has to change: the environment, the RTL, the testbench, or the spec."""

LABELS: tuple[TriageLabel, ...] = ("infra", "rtl", "tb", "spec")
"""The choices of every triage question, in this order."""

TriageStatus = Literal["decided", "deferred", "undecided"]
"""`decided`: a label; `deferred`: a model in the user's Claude Code session will answer
(run the decider loop, then triage again); `undecided`: no rule and no model answered."""


class SpecLine(BaseModel):
    """A spec source the question showed the model: a document line or a model entity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    citation: str = Field(description="'path:line' or 'model:<key>'.")
    text: str = Field(description="The line, or the entity's facts.")
    defined_at: str | None = Field(
        default=None, description="For a model entity: the 'path:line' it was read from."
    )

    @property
    def location(self) -> str:
        """The file location when there is one, else the citation."""
        return self.defined_at or self.citation


class TriageReport(BaseModel):
    """What `/triage` says about one failing log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    status: TriageStatus = Field(description="Whether a label was decided.")
    label: TriageLabel | None = Field(default=None, description="The class, when decided.")
    backend: Literal["rule", "small", "large"] | None = Field(
        default=None, description="Who decided: a rule, or a model tier."
    )
    rule: str | None = Field(default=None, description="The rule that decided, if one did.")
    confidence: float = Field(default=0.0, ge=0, le=1, description="Confidence of the label.")
    low_confidence: bool = Field(
        default=False, description="A model's label under its tier's threshold: advice only."
    )
    question_id: str = Field(description="The decide() question id ('triage.<hash>').")
    check_id: str | None = Field(default=None, description="The failing check, if known.")
    source: str | None = Field(default=None, description="Where the log was read from.")
    summary: str = Field(description="What failed and where, from the parsed log.")
    suggestion: str = Field(default="", description="What to do next, for the label.")
    retry_without_counting: bool = Field(
        default=False,
        description="infra: fix the environment and retry; the retry does not count a try.",
    )
    ask_person: bool = Field(
        default=False, description="spec: the spec owner decides; ask them before changing it."
    )
    evidence: tuple[str, ...] = Field(
        default=(), description="'file:line' locations (and spec citations) behind the label."
    )
    issues: tuple[Issue, ...] = Field(default=(), description="Issues parsed from the log.")
    excerpt: str = Field(default="", description="The shortened log the question showed.")
    spec_lines: tuple[SpecLine, ...] = Field(
        default=(),
        description=(
            "Spec sources: those shown to the model with a simulation failure, or the spec "
            "lines a 'spec' label points at."
        ),
    )
    reason: str = Field(default="", description="Why: the rule's evidence or the model's reason.")
    model: str | None = Field(default=None, description="The model that answered, if any.")
    message: str = Field(default="", description="What to do next when not decided.")


__all__ = ["LABELS", "SpecLine", "TriageLabel", "TriageReport", "TriageStatus"]

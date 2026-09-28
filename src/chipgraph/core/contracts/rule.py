"""Rules: how artifacts get produced, and instances of rules bound to concrete params."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chipgraph.core.contracts._paths import validate_relative_path
from chipgraph.core.contracts.artifact import ArtifactRef
from chipgraph.core.contracts.types import ModelTier, RuleId, RuleKind

_TIER_ORDER: dict[ModelTier, int] = {"small": 0, "medium": 1, "large": 2}


class InputSpec(BaseModel):
    """A declared input to a rule: where it comes from and how to select it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["model", "spec", "artifact", "path"] = Field(
        description="Where this input comes from: the Design Model, a spec, an artifact, or a path."
    )
    selector: str = Field(description="A template selecting the input, e.g. 'block/{block}'.")

    @model_validator(mode="before")
    @classmethod
    def _accept_short_form(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and len(data) == 1
            and not (set(data.keys()) & {"source", "selector"})
        ):
            ((key, value),) = data.items()
            if key in ("model", "spec", "artifact", "path"):
                return {"source": key, "selector": value}
        return data


class Budget(BaseModel):
    """Resource budget for an agent rule: retries, model tier, and token cap."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tries: int = Field(default=3, ge=1, description="Maximum number of attempts.")
    tier: ModelTier = Field(default="medium", description="Model tier to use.")
    escalate: ModelTier | None = Field(
        default=None, description="Higher model tier to escalate to after repeated failures."
    )
    tokens: int | None = Field(default=None, ge=1, description="Maximum tokens allowed, if capped.")

    @model_validator(mode="after")
    def _check_escalate_order(self) -> Self:
        if self.escalate is not None and _TIER_ORDER[self.escalate] <= _TIER_ORDER[self.tier]:
            raise ValueError(
                f"escalate tier {self.escalate!r} must be higher than tier {self.tier!r}"
            )
        return self


_DEFAULT_BUDGET = Budget()


class RunSpec(BaseModel):
    """How a `gen`/`import` rule actually runs: a tool adapter and its arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    use: str = Field(description="The tool adapter to run, e.g. 'cmd'.")
    args: dict[str, Any] = Field(default={}, description="Arguments passed to the tool adapter.")


class RuleSpec(BaseModel):
    """The specification of a rule: how it produces outputs from inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    id: RuleId = Field(description="Namespaced rule id, e.g. 'digital-rtl/rtl_module'.")
    kind: RuleKind = Field(description="How this rule produces its outputs.")
    description: str = Field(
        default="", description="A short, plain-English description of the rule."
    )
    foreach: str | None = Field(
        default=None,
        description="A selector this rule is expanded over, e.g. one instance per block.",
    )
    inputs: tuple[InputSpec, ...] = Field(default=(), description="The inputs this rule consumes.")
    outputs: tuple[str, ...] = Field(
        min_length=1, description="Output path templates this rule produces."
    )
    checks: tuple[str, ...] = Field(
        default=(), description="Check ids run to verify this rule's outputs."
    )
    verified_by: RuleId | str | None = Field(
        default=None, description="A rule or check id that verifies this rule's outputs."
    )
    budget: Budget = Field(default_factory=lambda: _DEFAULT_BUDGET, description="Resource budget.")
    role: str | None = Field(
        default=None, description="The agent role that runs this rule, if any."
    )
    skills: tuple[str, ...] = Field(
        default=(), description="Skill ids available to the agent role."
    )
    gate: str | None = Field(
        default=None, description="A gate id that must approve before this rule runs."
    )
    run: RunSpec | None = Field(
        default=None,
        description="How this rule actually runs (kind 'gen'/'import' only): tool adapter + args.",
    )

    @model_validator(mode="after")
    def _check_outputs_present(self) -> Self:
        if len(self.outputs) < 1:
            raise ValueError("RuleSpec.outputs must have at least one output template")
        for output in self.outputs:
            validate_relative_path(output, field_name="outputs")
        return self

    @model_validator(mode="after")
    def _check_agent_fields(self) -> Self:
        if self.kind == "agent":
            if self.role is None:
                raise ValueError("RuleSpec with kind='agent' requires 'role'")
        else:
            if self.role is not None:
                raise ValueError("'role' is only allowed when kind='agent'")
            if self.skills:
                raise ValueError("'skills' is only allowed when kind='agent'")
            if self.budget != _DEFAULT_BUDGET:
                raise ValueError("a non-default 'budget' is only allowed when kind='agent'")
        return self

    @model_validator(mode="after")
    def _check_run_field(self) -> Self:
        if self.run is not None and self.kind not in ("gen", "import"):
            raise ValueError("'run' is only allowed when kind is 'gen' or 'import'")
        if self.kind == "gen" and self.run is None:
            raise ValueError("RuleSpec with kind='gen' requires 'run'")
        return self


class RuleInstance(BaseModel):
    """A rule bound to concrete parameters, with resolved inputs and outputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    rule_id: RuleId = Field(description="The id of the rule this is an instance of.")
    params: dict[str, str] = Field(
        default={}, description="Concrete parameter values, e.g. block, module."
    )
    inputs: tuple[ArtifactRef, ...] = Field(
        default=(), description="Resolved input artifact references."
    )
    outputs: tuple[ArtifactRef, ...] = Field(
        min_length=1, description="Resolved output artifact references."
    )
    instance_id: str = Field(description="A stable id for this instance, built by make_id().")

    @classmethod
    def make_id(cls, rule_id: str, params: dict[str, str]) -> str:
        """Build the stable instance id for a rule id and its parameters."""
        sorted_params = ",".join(f"{k}={params[k]}" for k in sorted(params))
        return f"{rule_id}[{sorted_params}]"

    @model_validator(mode="after")
    def _check_outputs_and_id(self) -> Self:
        if len(self.outputs) < 1:
            raise ValueError("RuleInstance.outputs must have at least one output")
        expected = self.make_id(self.rule_id, self.params)
        if self.instance_id != expected:
            raise ValueError(
                f"instance_id {self.instance_id!r} does not match make_id() result {expected!r}"
            )
        return self

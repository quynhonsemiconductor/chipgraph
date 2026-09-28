"""Events: the append-only record of what happened during a run."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from chipgraph.core.contracts.types import FailureLabel, Sha256

EventType = Literal[
    "run_start",
    "rule_start",
    "tool_call",
    "check_result",
    "agent_turn",
    "gate_wait",
    "gate_decision",
    "rule_done",
    "rule_fail",
    "run_stop",
]
"""The kind of thing that happened, recorded as one event in a run's journal."""


class Event(BaseModel):
    """A single entry in a run's append-only journal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    run_id: str = Field(description="The id of the run this event belongs to.")
    seq: int = Field(ge=0, description="The 0-based sequence number of this event within the run.")
    ts: AwareDatetime = Field(description="When this event occurred.")
    type: EventType = Field(description="The kind of event this is.")
    rule_instance: str | None = Field(
        default=None, description="The rule instance this event concerns, if any."
    )
    payload: dict[str, Any] = Field(default={}, description="Event-type-specific data.")
    failure_label: FailureLabel | None = Field(
        default=None, description="Why a rule failed; only set when type is 'rule_fail'."
    )

    @model_validator(mode="after")
    def _check_failure_label_only_on_rule_fail(self) -> Self:
        if self.failure_label is not None and self.type != "rule_fail":
            raise ValueError("failure_label is only allowed when type='rule_fail'")
        return self


class RunManifest(BaseModel):
    """A snapshot of everything needed to reproduce or audit a run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    run_id: str = Field(description="The id of this run.")
    started_at: AwareDatetime = Field(description="When this run started.")
    target: str = Field(description="The build target this run was invoked with.")
    chipgraph_version: str = Field(description="The chipgraph version that executed this run.")
    profile_hash: Sha256 = Field(description="Hash of the resolved profile used for this run.")
    profile_sources: tuple[str, ...] = Field(
        default=(),
        description="Sources the profile was assembled from, e.g. '.chipgraph.yml@<commit>'.",
    )
    packs: dict[str, str] = Field(default={}, description="Pack ids to their versions.")
    adapters: dict[str, str] = Field(default={}, description="Adapter ids to their versions.")
    models: dict[str, str] = Field(default={}, description="Model ids to their versions.")
    tools: dict[str, str] = Field(default={}, description="EDA tool ids to their versions.")
    runner: str = Field(default="local", description="The runner used to execute this run.")

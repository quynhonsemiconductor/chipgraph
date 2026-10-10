"""The plan file a Planner writes for one block: `plan/<block>.plan.yml` (DESIGN.md 5.1, 5.3).

A plan splits a block into modules. Each module says what it does, which requirements
it covers, its interface (ports taken from the Design Model, F1), which other modules
it depends on, the files it writes (its write set, F2) and its budget. The plan also
lists the assumptions it rests on (F4), the questions a person must answer, and every
requirement of the block it does not assign, with the reason.

The plan is data a person approves (gate `plan:<block>`) before any node it describes
can run; `plan_check` (`chipgraph.checks.plan_check`) validates it. The JSON Schema is
committed at `schemas/formats/plan.schema.json` (regenerate with
`python -m chipgraph.packs.digital_rtl.plan`).
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chipgraph.core.contracts._paths import validate_relative_path
from chipgraph.core.contracts.types import ModelTier

_NAME_RE = r"^[A-Za-z_][A-Za-z0-9_]*$"
_GLOB_OR_PLACEHOLDER = re.compile(r"[*?\[\]{}]")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanPort(_Model):
    """One port of a module: from the block's interface in the Design Model, or internal."""

    name: str = Field(pattern=_NAME_RE, description="The port name, as in the Design Model.")
    direction: Literal["input", "output", "inout"] = Field(description="The port direction.")
    width: int | str = Field(description="The port width: bits, or the model's expression.")
    internal: bool = Field(
        default=False,
        description=(
            "A helper port between modules of the block, not on the block's interface. "
            "Not allowed on the block's top module."
        ),
    )


class PlanInterface(_Model):
    """A module's interface."""

    ports: tuple[PlanPort, ...] = Field(default=(), description="The module's ports.")


class PlanWrite(_Model):
    """One file a module writes."""

    path: str = Field(description="Repo-relative POSIX path of the file.")
    replaces: bool = Field(
        default=False,
        description="The file exists and this module replaces it (said explicitly).",
    )

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        validate_relative_path(value, field_name="writes.path")
        if _GLOB_OR_PLACEHOLDER.search(value):
            raise ValueError(f"writes.path must be a plain file path, got {value!r}")
        return value


class ModuleBudget(_Model):
    """How much a module's nodes may spend: tries and the model tier."""

    tries: int = Field(default=3, ge=1, description="Tries the module's agent task may use.")
    tier: ModelTier = Field(default="medium", description="The model tier to start with.")


class PlanModule(_Model):
    """One module of the plan: one dynamic node per downstream rule."""

    name: str = Field(pattern=_NAME_RE, description="The module name (an RTL identifier).")
    summary: str = Field(min_length=1, description="What the module does, in a sentence or two.")
    reqs: tuple[str, ...] = Field(
        default=(), description="Requirements it covers: REQ ids or inferred requirement keys."
    )
    interface: PlanInterface = Field(
        default_factory=PlanInterface, description="The module's ports."
    )
    depends_on: tuple[str, ...] = Field(
        default=(), description="Modules of this plan that must be built first."
    )
    writes: tuple[PlanWrite, ...] = Field(
        min_length=1, description="Every file the module's nodes write (its write set)."
    )
    budget: ModuleBudget = Field(
        default_factory=ModuleBudget, description="The module's tries and model tier."
    )

    @property
    def write_paths(self) -> tuple[str, ...]:
        """The paths of `writes`, in order."""
        return tuple(w.path for w in self.writes)


class UnassignedReq(_Model):
    """A requirement of the block that no module covers, and why."""

    req: str = Field(min_length=1, description="The REQ id or inferred requirement key.")
    reason: str = Field(min_length=1, description="Why no module of this plan covers it.")


class Plan(_Model):
    """A block's plan: its modules, their write sets and order, and what a person must read."""

    schema_version: Literal[1] = Field(default=1, description="Schema version of this file.")
    block: str = Field(min_length=1, description="The block this plan is for.")
    top: str = Field(description="The module that is the block's top (its interface).")
    modules: tuple[PlanModule, ...] = Field(min_length=1, description="The modules.")
    assumptions: tuple[str, ...] = Field(
        default=(), description="Assumptions the plan rests on (F4)."
    )
    open_questions: tuple[str, ...] = Field(
        default=(), description="Questions the approver must read and answer."
    )
    unassigned_reqs: tuple[UnassignedReq, ...] = Field(
        default=(), description="Requirements of the block no module covers, with the reason."
    )

    def module(self, name: str) -> PlanModule | None:
        """The module called `name`, if the plan has one."""
        return next((m for m in self.modules if m.name == name), None)


def schema_json() -> str:
    """The JSON Schema of a plan file, as committed under `schemas/formats/`."""
    return json.dumps(Plan.model_json_schema(), indent=2, sort_keys=True) + "\n"


__all__ = [
    "ModuleBudget",
    "Plan",
    "PlanInterface",
    "PlanModule",
    "PlanPort",
    "PlanWrite",
    "UnassignedReq",
    "schema_json",
]

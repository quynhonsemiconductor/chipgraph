"""The core Design Model entities (DESIGN.md 4.2).

One frozen pydantic class per core kind, all sharing `EntityBase`. Every field beyond
the base is optional: extractors fill in what they know, and later tasks (M1-04..M1-07)
narrow things down over time. Anything an extractor cannot type yet goes in `attrs`.

Entity kinds are generic hardware concepts (port, register, clock, ...). Nothing here
names a specific chip project, bus protocol or tool.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.model.json_value import JSONValue
from chipgraph.core.model.provenance import Provenance


class EntityBase(BaseModel):
    """Fields shared by every entity kind, core or pack-defined."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    kind: str = Field(description="The entity kind; overridden per subclass with a Literal.")
    key: str = Field(description="The stable model key, e.g. 'register:timer.CTRL'.")
    name: str = Field(description="A short human-readable name for this entity.")
    source: Provenance = Field(
        default_factory=Provenance, description="Where this fact was learned from."
    )
    attrs: dict[str, JSONValue] = Field(
        default_factory=dict, description="Open-ended extra facts not covered by typed fields."
    )


class EarsParts(BaseModel):
    """A requirement's text, broken into EARS-style structured parts (DESIGN.md 8/M3-08).

    Optional: an extractor that only has the raw sentence leaves these unset.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger: str | None = Field(default=None, description="The triggering event ('when ...').")
    condition: str | None = Field(default=None, description="A qualifying state ('while ...').")
    subject: str | None = Field(default=None, description="The system or object the REQ is about.")
    action: str | None = Field(default=None, description="The required behavior ('shall ...').")


class ProjectEntity(EntityBase):
    """The top-level project or chip this Design Model describes."""

    kind: Literal["project"] = "project"


class BlockEntity(EntityBase):
    """A design block (an IP or subsystem), e.g. 'timer'."""

    kind: Literal["block"] = "block"
    owner: str | None = Field(default=None, description="Who owns this block.")
    path: str | None = Field(default=None, description="Repo-relative directory for this block.")


class ModuleEntity(EntityBase):
    """A single RTL module."""

    kind: Literal["module"] = "module"
    file: str | None = Field(default=None, description="Repo-relative file this module is in.")
    block: str | None = Field(default=None, description="The model key of the owning block.")


class PortEntity(EntityBase):
    """A port on a module."""

    kind: Literal["port"] = "port"
    direction: Literal["input", "output", "inout"] | None = Field(
        default=None, description="Port direction."
    )
    width: int | str | None = Field(
        default=None, description="Port width: an int, or an unresolved expression string."
    )
    clock: str | None = Field(default=None, description="Model key of the clock this port is in.")
    reset: str | None = Field(default=None, description="Model key of the reset this port uses.")
    module: str | None = Field(default=None, description="Model key of the owning module.")


class InterfaceEntity(EntityBase):
    """A bus/protocol interface (APB, AXI, TileLink, ...), described as data."""

    kind: Literal["interface"] = "interface"
    protocol: str | None = Field(default=None, description="The protocol this interface follows.")


class ClockEntity(EntityBase):
    """A clock domain."""

    kind: Literal["clock"] = "clock"
    frequency_hz: float | None = Field(default=None, ge=0, description="Nominal frequency, in Hz.")


class ResetEntity(EntityBase):
    """A reset signal."""

    kind: Literal["reset"] = "reset"
    active_low: bool | None = Field(default=None, description="Whether this reset is active-low.")
    sync: bool | None = Field(default=None, description="Whether this reset is synchronous.")


class ParameterEntity(EntityBase):
    """A module parameter."""

    kind: Literal["parameter"] = "parameter"
    value: str | int | float | bool | None = Field(
        default=None, description="The parameter's value or default value."
    )
    module: str | None = Field(default=None, description="Model key of the owning module.")


class RegisterEntity(EntityBase):
    """A register."""

    kind: Literal["register"] = "register"
    block: str | None = Field(default=None, description="Model key of the owning block.")
    offset: int | str | None = Field(
        default=None, description="Byte offset: an int, or an unresolved expression string."
    )
    width: int | None = Field(default=None, ge=1, description="Register width, in bits.")
    reset_value: int | str | None = Field(default=None, description="Reset value.")
    access: str | None = Field(default=None, description="Access mode, e.g. 'rw', 'ro', 'w1c'.")


class FieldEntity(EntityBase):
    """A bit field within a register."""

    kind: Literal["field"] = "field"
    register: str | None = Field(default=None, description="Model key of the owning register.")
    lsb: int | None = Field(default=None, ge=0, description="Least-significant bit index.")
    msb: int | None = Field(default=None, ge=0, description="Most-significant bit index.")
    access: str | None = Field(default=None, description="Access mode, e.g. 'rw', 'ro', 'w1c'.")
    reset_value: int | str | None = Field(default=None, description="Reset value.")


class InterruptEntity(EntityBase):
    """An interrupt line."""

    kind: Literal["interrupt"] = "interrupt"
    block: str | None = Field(default=None, description="Model key of the owning block.")
    line: int | None = Field(default=None, ge=0, description="Interrupt line number.")


class MemoryRegionEntity(EntityBase):
    """A region of the address map."""

    kind: Literal["memory_region"] = "memory_region"
    base: int | str | None = Field(
        default=None, description="Base address: an int, or an unresolved expression string."
    )
    size: int | str | None = Field(
        default=None, description="Region size in bytes: an int, or an unresolved expression."
    )
    block: str | None = Field(default=None, description="Model key of the owning block.")


class RequirementEntity(EntityBase):
    """A single requirement, usually with a REQ-ID."""

    kind: Literal["requirement"] = "requirement"
    text: str | None = Field(default=None, description="The requirement's full sentence.")
    ears: EarsParts | None = Field(
        default=None, description="Structured EARS parts, when available."
    )


class DecisionEntity(EntityBase):
    """A recorded design decision (not to be confused with `contracts.Decision`)."""

    kind: Literal["decision"] = "decision"
    text: str | None = Field(default=None, description="What was decided.")
    rationale: str | None = Field(default=None, description="Why it was decided.")


class OpenItemEntity(EntityBase):
    """An open question or todo against a spec."""

    kind: Literal["open_item"] = "open_item"
    status: Literal["open", "resolved"] | None = Field(
        default=None, description="Whether this open item is still open."
    )


class TestEntity(EntityBase):
    """A test, mapped back to the requirements it verifies."""

    __test__ = False  # tell pytest this "Test*" class is not a test case to collect

    kind: Literal["test"] = "test"
    requirement_keys: tuple[str, ...] = Field(
        default=(), description="Model keys of the requirements this test verifies."
    )


CORE_ENTITY_CLASSES: tuple[type[EntityBase], ...] = (
    ProjectEntity,
    BlockEntity,
    ModuleEntity,
    PortEntity,
    InterfaceEntity,
    ClockEntity,
    ResetEntity,
    ParameterEntity,
    RegisterEntity,
    FieldEntity,
    InterruptEntity,
    MemoryRegionEntity,
    RequirementEntity,
    DecisionEntity,
    OpenItemEntity,
    TestEntity,
)
"""Every core entity class, in the order they should appear in generated schemas."""


class ExtEntity(EntityBase):
    """A pack-defined entity kind the reader has no typed class registered for.

    Round-trips losslessly: `kind` and every extra fact live in `attrs`/`kind` so a
    store can persist and reload entities from packs it does not know about.
    """

    kind: str = Field(description="The pack-defined kind, e.g. 'pin_spec'.")

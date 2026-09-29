"""Format adapter `chip-yaml`: chipgraph's own generic chip description (`chip.yml`).

`chip.yml` names a chip's buses, blocks, clocks and resets. Numbers that code can work
out may be left as `auto` (DESIGN.md 4.3, "code computes"):

- **base address** `auto`: blocks are placed in file order at the lowest free address
  that is aligned to the block's size (a power of two) and overlaps no fixed or already
  placed block. Needs `size`.
- **interrupt line** `auto`: interrupts get the lowest line number not used by a fixed
  line or an earlier `auto`, in file order.

The model: a `project`; per block a `block` (ip, bus in `attrs`), a `memory_region` and
its `interrupt`s; per bus an `interface` (protocol as data); `clock`s and `reset`s.
Relations: `contains` project→block, block→region and block→interrupt, and `connects`
bus→block for each bus slave. A validation error names the file and the line of the item.
`load_model` also warns about overlapping regions and duplicate interrupt lines.

Regenerate `schemas/formats/chip.schema.json` with `python -m chipgraph.adapters.format`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from chipgraph.adapters.format._yaml_lines import LinedYaml
from chipgraph.adapters.format.qsoc_contract import (
    Region,
    _display_path,
    contains,
    duplicate_line_warnings,
    model_facts,
    overlap_warnings,
)
from chipgraph.core.model import (
    BlockEntity,
    ClockEntity,
    DesignModel,
    EntityBase,
    InterfaceEntity,
    InterruptEntity,
    MemoryRegionEntity,
    ProjectEntity,
    Provenance,
    Relation,
    ResetEntity,
    make_key,
)

_EXTRACTOR = "chip-yaml"

type Auto = Literal["auto"]


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClockSpec(_Spec):
    """A clock."""

    name: str
    frequency_hz: float | None = Field(default=None, ge=0)


class ResetSpec(_Spec):
    """A reset."""

    name: str
    active_low: bool | None = None
    sync: bool | None = None


class InterruptSpec(_Spec):
    """One interrupt output of a block."""

    name: str
    line: int | Auto = Field(default="auto", description="Interrupt line, or 'auto'.")


class BlockSpec(_Spec):
    """A block: an IP instance on a bus."""

    name: str
    ip: str | None = Field(default=None, description="The IP it instantiates.")
    bus: str | None = Field(default=None, description="The bus it is a slave of.")
    base: int | Auto | None = Field(default=None, description="Base address, or 'auto'.")
    size: int | None = Field(default=None, gt=0, description="Size of its window in bytes.")
    interrupts: tuple[InterruptSpec, ...] = ()
    clock: str | None = None
    reset: str | None = None
    owner: str | None = None


class BusSpec(_Spec):
    """A bus; the protocol is data (for example 'APB', 'AXI4')."""

    name: str
    protocol: str
    masters: tuple[str, ...] = ()
    slaves: tuple[str, ...] = ()


class ChipYaml(_Spec):
    """The `chip.yml` schema."""

    project: str
    data_width: int = Field(ge=1)
    addr_width: int = Field(ge=1)
    buses: tuple[BusSpec, ...] = ()
    blocks: tuple[BlockSpec, ...] = ()
    clocks: tuple[ClockSpec, ...] = ()
    resets: tuple[ResetSpec, ...] = ()


class ChipYamlAdapter:
    """`FormatAdapter` for `chip.yml`."""

    name = "chip-yaml"

    def load(self, path: Path) -> Iterable[Mapping[str, object]]:
        """The `FormatAdapter` protocol: every entity and relation as a plain mapping."""
        model, _ = self.load_model(path)
        return model_facts(model)

    def load_model(self, path: Path, root: Path | None = None) -> tuple[DesignModel, list[str]]:
        """Load `path` into a `DesignModel`; raise `ValueError` naming file:line if invalid.

        When `root` is given and `path` is under it, provenance `file` is written relative
        to `root` (POSIX); otherwise it is `path.as_posix()` as before.
        """
        doc = LinedYaml(path)
        file = _display_path(path, root)
        try:
            chip = ChipYaml.model_validate(doc.data or {})
        except ValidationError as exc:
            problems = "; ".join(
                f"{file}:{doc.line(err['loc'])}: {'.'.join(map(str, err['loc']))}: {err['msg']}"
                for err in exc.errors()
            )
            raise ValueError(f"Invalid chip.yml {file}: {problems}") from exc

        def src(*at: str | int) -> Provenance:
            return Provenance(file=file, line=doc.line(at), extractor=_EXTRACTOR)

        entities: list[EntityBase] = []
        relations: list[Relation] = []
        project_key = make_key("project", chip.project)
        entities.append(
            ProjectEntity(
                key=project_key,
                name=chip.project,
                source=src("project"),
                attrs={"data_width": chip.data_width, "addr_width": chip.addr_width},
            )
        )

        bases = resolve_bases(chip.blocks)
        lines = resolve_lines(chip.blocks)
        regions: list[Region] = []
        numbered: list[tuple[str, int, int]] = []
        for i, block in enumerate(chip.blocks):
            block_key = make_key("block", block.name)
            entities.append(
                BlockEntity(
                    key=block_key,
                    name=block.name,
                    owner=block.owner,
                    source=src("blocks", i),
                    attrs={
                        "ip": block.ip,
                        "bus": block.bus,
                        "clock": block.clock,
                        "reset": block.reset,
                    },
                )
            )
            relations.append(contains(project_key, block_key, src("blocks", i)))
            if block.size is not None:
                region_key = make_key("memory_region", block.name)
                base = bases[i]
                entities.append(
                    MemoryRegionEntity(
                        key=region_key,
                        name=block.name,
                        base=base if base is not None else "auto",
                        size=block.size,
                        block=block_key,
                        source=src("blocks", i, "base"),
                        attrs={"auto": block.base == "auto"},
                    )
                )
                relations.append(contains(block_key, region_key, src("blocks", i)))
                if base is not None:
                    regions.append(Region(block.name, base, block.size, doc.line(("blocks", i))))
            for j, irq in enumerate(block.interrupts):
                irq_key = make_key("interrupt", block.name, irq.name)
                number = lines[(i, j)]
                entities.append(
                    InterruptEntity(
                        key=irq_key,
                        name=irq.name,
                        line=number,
                        block=block_key,
                        source=src("blocks", i, "interrupts", j),
                        attrs={"auto": irq.line == "auto"},
                    )
                )
                relations.append(contains(block_key, irq_key, src("blocks", i, "interrupts", j)))
                numbered.append(
                    (f"{block.name}.{irq.name}", number, doc.line(("blocks", i, "interrupts", j)))
                )

        block_keys = {b.name: make_key("block", b.name) for b in chip.blocks}
        for i, bus in enumerate(chip.buses):
            bus_key = make_key("interface", bus.name)
            entities.append(
                InterfaceEntity(
                    key=bus_key,
                    name=bus.name,
                    protocol=bus.protocol,
                    source=src("buses", i),
                    attrs={"masters": list(bus.masters), "slaves": list(bus.slaves)},
                )
            )
            for slave in bus.slaves:
                if slave in block_keys:
                    relations.append(
                        Relation(
                            kind="connects",
                            src=bus_key,
                            dst=block_keys[slave],
                            source=src("buses", i, "slaves"),
                        )
                    )

        for i, clock in enumerate(chip.clocks):
            entities.append(
                ClockEntity(
                    key=make_key("clock", clock.name),
                    name=clock.name,
                    frequency_hz=clock.frequency_hz,
                    source=src("clocks", i),
                )
            )
        for i, reset in enumerate(chip.resets):
            entities.append(
                ResetEntity(
                    key=make_key("reset", reset.name),
                    name=reset.name,
                    active_low=reset.active_low,
                    sync=reset.sync,
                    source=src("resets", i),
                )
            )

        warnings = overlap_warnings(regions, file) + duplicate_line_warnings(numbered, file)
        return DesignModel.build(entities, relations), warnings


def resolve_bases(blocks: tuple[BlockSpec, ...]) -> list[int | None]:
    """Base address per block: fixed ones as given, `auto` ones by the module rule."""
    placed = [(b.base, b.size) for b in blocks if isinstance(b.base, int) and b.size]
    result: list[int | None] = []
    for block in blocks:
        if isinstance(block.base, int):
            result.append(block.base)
            continue
        if block.base != "auto" or not block.size:
            result.append(None)
            continue
        addr = 0
        while clash := next(
            ((b, s) for b, s in placed if addr < b + s and b < addr + block.size), None
        ):
            addr = _align_up(clash[0] + clash[1], block.size)
        placed.append((addr, block.size))
        result.append(addr)
    return result


def resolve_lines(blocks: tuple[BlockSpec, ...]) -> dict[tuple[int, int], int]:
    """Interrupt line per (block index, interrupt index), `auto` ones by the module rule."""
    used = {irq.line for b in blocks for irq in b.interrupts if isinstance(irq.line, int)}
    result: dict[tuple[int, int], int] = {}
    next_free = 0
    for i, block in enumerate(blocks):
        for j, irq in enumerate(block.interrupts):
            if isinstance(irq.line, int):
                result[(i, j)] = irq.line
                continue
            while next_free in used:
                next_free += 1
            result[(i, j)] = next_free
            used.add(next_free)
    return result


def _align_up(addr: int, size: int) -> int:
    return -(-addr // size) * size


def schema_json() -> str:
    """The JSON Schema of `chip.yml`, as committed in `schemas/formats/chip.schema.json`."""
    return json.dumps(ChipYaml.model_json_schema(), indent=2, sort_keys=True) + "\n"


__all__ = [
    "BlockSpec",
    "BusSpec",
    "ChipYaml",
    "ChipYamlAdapter",
    "ClockSpec",
    "InterruptSpec",
    "ResetSpec",
    "resolve_bases",
    "resolve_lines",
    "schema_json",
]

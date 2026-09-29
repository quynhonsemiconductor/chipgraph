"""Format adapter `qsoc-contract`: a QSoC-style inter-block contract into the Design Model.

The contract (`util/qsoc_contract.yml` in QSoC) is the single source of the numbers that
more than one block shares. It maps to the model as follows:

| Contract | Model |
|---|---|
| `meta` | one `project` (widths, clock, chip id in `attrs`) |
| each `memory_map` row | a `block` and its `memory_region` (base, size; port, kind in `attrs`) |
| each `interrupts.lines` item | an `interrupt` (line; peripheral, ports in `attrs`) |
| each `clock_domains.clusters` item | a `clock` (the chip frequency from `meta.clock_mhz`) |
| each `reset_sources` item | a `reset` |

An interrupt's `block` is the memory-map row of the same name, when there is one (the
contract names interrupts by peripheral, not always by row). Relations: `contains` from
the project to each block, and from a block to its region and
interrupts. Values the contract marks `tbd: true` are kept in `attrs` as they are and never
filled in. Unknown top-level keys are reported as warnings. `load_model` also warns about
overlapping memory regions and duplicate interrupt lines.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from chipgraph.adapters.format._yaml_lines import LinedYaml
from chipgraph.core.model import (
    BlockEntity,
    ClockEntity,
    DesignModel,
    EntityBase,
    InterruptEntity,
    JSONValue,
    MemoryRegionEntity,
    ProjectEntity,
    Provenance,
    Relation,
    ResetEntity,
    make_key,
)

_EXTRACTOR = "qsoc-contract"
_KNOWN_TOP = {"meta", "memory_map", "interrupts", "clock_domains", "reset_sources", "tbd"}


class QSocContractAdapter:
    """`FormatAdapter` for a QSoC-style `qsoc_contract.yml`."""

    name = "qsoc-contract"

    def load(self, path: Path) -> Iterable[Mapping[str, object]]:
        """The `FormatAdapter` protocol: every entity and relation as a plain mapping."""
        model, _ = self.load_model(path)
        return model_facts(model)

    def load_model(self, path: Path, root: Path | None = None) -> tuple[DesignModel, list[str]]:
        """Load `path` into a `DesignModel`, with warnings for anything suspicious.

        When `root` is given and `path` is under it, provenance `file` is written relative
        to `root` (POSIX); otherwise it is `path.as_posix()` as before.
        """
        doc = LinedYaml(path)
        data: dict[str, Any] = doc.data if isinstance(doc.data, dict) else {}
        file = _display_path(path, root)
        warnings: list[str] = []
        entities: list[EntityBase] = []
        relations: list[Relation] = []

        def src(*at: str | int) -> Provenance:
            return Provenance(file=file, line=doc.line(at), extractor=_EXTRACTOR)

        for key in sorted(set(data) - _KNOWN_TOP):
            warnings.append(f"{file}:{doc.line((key,))}: unknown top-level key {key!r} ignored")

        meta: dict[str, Any] = data.get("meta") or {}
        project_name = str(meta.get("project", path.stem))
        project_key = make_key("project", project_name)
        entities.append(
            ProjectEntity(
                key=project_key,
                name=project_name,
                source=src("meta", "project"),
                attrs={k: to_json(v) for k, v in meta.items() if k != "project"},
            )
        )

        block_keys: dict[str, str] = {}
        regions: list[Region] = []
        for i, row in enumerate(data.get("memory_map") or []):
            name = str(row["name"])
            block_key = make_key("block", name)
            region_key = make_key("memory_region", name)
            block_keys[name] = block_key
            extra = {k: to_json(v) for k, v in row.items() if k not in ("name", "base", "size")}
            entities.append(BlockEntity(key=block_key, name=name, source=src("memory_map", i)))
            entities.append(
                MemoryRegionEntity(
                    key=region_key,
                    name=name,
                    base=int_or_str(row.get("base")),
                    size=int_or_str(row.get("size")),
                    block=block_key,
                    source=src("memory_map", i),
                    attrs=extra,
                )
            )
            relations.append(contains(project_key, block_key, src("memory_map", i)))
            relations.append(contains(block_key, region_key, src("memory_map", i)))
            base, size = row.get("base"), row.get("size")
            if isinstance(base, int) and isinstance(size, int):
                regions.append(Region(name, base, size, doc.line(("memory_map", i))))

        interrupts: dict[str, Any] = data.get("interrupts") or {}
        numbered: list[tuple[str, int, int]] = []  # name, line number, source line
        for i, item in enumerate(interrupts.get("lines") or []):
            peripheral = str(item.get("peripheral", f"line{i}"))
            number = item.get("line")
            key = make_key("interrupt", peripheral)
            owner = block_keys.get(peripheral)
            entities.append(
                InterruptEntity(
                    key=key,
                    name=peripheral,
                    line=number if isinstance(number, int) else None,
                    block=owner,
                    source=src("interrupts", "lines", i),
                    attrs={k: to_json(v) for k, v in item.items() if k != "line"},
                )
            )
            if owner is not None:
                relations.append(contains(owner, key, src("interrupts", "lines", i)))
            if isinstance(number, int):
                numbered.append((peripheral, number, doc.line(("interrupts", "lines", i))))

        frequency = meta.get("clock_mhz")
        clocks: dict[str, Any] = data.get("clock_domains") or {}
        for i, cluster in enumerate(clocks.get("clusters") or []):
            name = str(cluster["name"])
            entities.append(
                ClockEntity(
                    key=make_key("clock", name),
                    name=name,
                    frequency_hz=float(frequency) * 1e6 if isinstance(frequency, int) else None,
                    source=src("clock_domains", "clusters", i),
                    attrs={k: to_json(v) for k, v in cluster.items() if k != "name"},
                )
            )

        for i, name in enumerate(data.get("reset_sources") or []):
            entities.append(
                ResetEntity(
                    key=make_key("reset", str(name)),
                    name=str(name),
                    source=src("reset_sources", i),
                )
            )

        warnings.extend(overlap_warnings(regions, file))
        warnings.extend(duplicate_line_warnings(numbered, file))
        return DesignModel.build(entities, relations), warnings


class Region:
    """A memory region with a known base and size, for overlap checks."""

    __slots__ = ("base", "line", "name", "size")

    def __init__(self, name: str, base: int, size: int, line: int) -> None:
        self.name, self.base, self.size, self.line = name, base, size, line


def overlap_warnings(regions: list[Region], file: str) -> list[str]:
    """One warning for every pair of memory regions that share an address."""
    found: list[str] = []
    ordered = sorted(regions, key=lambda r: (r.base, r.name))
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if b.base >= a.base + a.size:
                break
            found.append(
                f"{file}:{b.line}: memory regions {a.name!r} "
                f"[{a.base:#x}, {a.base + a.size:#x}) (line {a.line}) and {b.name!r} "
                f"[{b.base:#x}, {b.base + b.size:#x}) are overlapping"
            )
    return found


def duplicate_line_warnings(numbered: list[tuple[str, int, int]], file: str) -> list[str]:
    """One warning for every interrupt line number used more than once."""
    found: list[str] = []
    first: dict[int, tuple[str, int]] = {}
    for name, number, line in numbered:
        if number in first:
            other, other_line = first[number]
            found.append(
                f"{file}:{line}: duplicate interrupt line {number}: {name!r} and "
                f"{other!r} (line {other_line})"
            )
        else:
            first[number] = (name, line)
    return found


def model_facts(model: DesignModel) -> list[Mapping[str, object]]:
    """Every entity (sorted by key) and relation of `model` as plain mappings."""
    facts: list[Mapping[str, object]] = [
        model.entities[key].model_dump(mode="json") for key in sorted(model.entities)
    ]
    facts.extend(r.model_dump(mode="json") for r in model.relations)
    return facts


def contains(src: str, dst: str, source: Provenance) -> Relation:
    return Relation(kind="contains", src=src, dst=dst, source=source)


def _display_path(path: Path, root: Path | None) -> str:
    """`path` relative to `root` (POSIX) when it is under it, else its POSIX form."""
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def int_or_str(value: object) -> int | str | None:
    if value is None or isinstance(value, int):
        return value
    return str(value)


def to_json(value: object) -> JSONValue:
    """A YAML value as JSON: containers kept, anything else not JSON as a string."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [to_json(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in value.items()}
    return str(value)


__all__ = ["QSocContractAdapter"]

"""The interface a testbench is written against, without the RTL (DESIGN.md 5.3 F1, 4.4).

`interface_for(module, ...)` is pure: it reads the Design Model it is given and, only
when it needs one, asks `declaration` for the module's declaration. The port list comes
from the first of these that has it:

1. ``spec``: the block's spec ports in the Design Model (`port:spec.<block>.<name>`);
2. ``model_rtl``: the module's RTL port entities already in the Design Model (they came
   from declarations, by `chipgraph ingest`);
3. ``rtl_declaration``: only when neither exists (a new module, a model not ingested, a
   port list missing), the module's declaration read from the RTL with pyslang
   (`chipgraph.packs.dv.declaration`: the header only, never the body).

Every port carries its `source`. When the spec ports are used and RTL ports are known
(2 or 3), each spec port that the RTL declares differently gets the note `rtl differs`,
with the RTL's direction and width only when they came from the model (2), never when
they came from reading the RTL (3); ports only the RTL has are counted in `notes`. The
difference itself is the `ports_diff` check's to report: nothing is resolved here.

`spec_slice(model, block)` is the block's spec side of the Design Model for a role that
must not see RTL: requirements (id, text, where), registers and fields, interrupts,
memory regions, clock and reset. No module, RTL port or RTL file is in it.

`project_interface(...)` wires `interface_for` to a project: the module (instance param
`module`, else the block's top module in the model, else the `top` template of the
profile's `tb_static` adapter) and the RTL files a declaration may come from (that
module's file in the model, the `tb_static` adapter's `files`/`filelist`, else the
profile layout's `filelist` and `rtl` entries for the block). The engine calls it for a
testbench task's context and `tb_static` calls it for its port check, so both see the
same interface.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.adapters.tool.filelist import FilelistError, read_filelist
from chipgraph.core.model.entities import (
    BlockEntity,
    FieldEntity,
    InterruptEntity,
    MemoryRegionEntity,
    ModuleEntity,
    PortEntity,
    RegisterEntity,
    RequirementEntity,
    ResetEntity,
)
from chipgraph.core.model.model import DesignModel
from chipgraph.packs.dv.declaration import ModuleDeclaration, extract_declaration

PortSource = Literal["spec", "model_rtl", "rtl_declaration"]

RTL_DIFFERS = "rtl differs"
"""The note on a spec port the RTL declares differently (see `ports_diff`)."""
MISSING_IN_RTL = "missing in rtl"
"""The note on a spec port the RTL does not declare."""

CLOCK_PATTERNS = (r"^(?:i_)?clk", r"_clk(?:_i)?$", r"^clock")
"""Port names taken as a clock when the model names none."""
RESET_PATTERNS = (r"^(?:i_)?rst", r"_rst(?:_n|_ni)?$", r"^reset")
"""Port names taken as a reset when the model names none."""

LayoutValue = str | tuple[str, ...] | list[str]


class InterfacePort(BaseModel):
    """One port of the interface, with where it was learnt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    name: str = Field(description="The port name: `dut.<name>` in a cocotb test.")
    direction: str | None = Field(default=None, description="input, output, inout (or ref).")
    width: int | str | None = Field(default=None, description="Bits, or an expression.")
    type: str = Field(default="", description="Declared type, from a declaration only.")
    packed: str = Field(default="", description="Packed dimensions, from a declaration only.")
    unpacked: str = Field(default="", description="Unpacked dimensions, from a declaration.")
    description: str = Field(default="", description="The spec's description of the port.")
    source: PortSource = Field(description="Where the port was learnt.")
    note: str | None = Field(default=None, description="`rtl differs` / `missing in rtl`.")
    rtl: dict[str, Any] | None = Field(
        default=None,
        description="The RTL's direction and width when they differ (model RTL ports only).",
    )


class InterfaceParameter(BaseModel):
    """A parameter of the module's header (from a declaration only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    name: str = Field(description="The parameter name.")
    kind: str = Field(description="'parameter', 'localparam' or 'type'.")
    type: str = Field(default="", description="Its declared type.")
    default: str | None = Field(default=None, description="Its default expression.")
    source: PortSource = Field(default="rtl_declaration", description="Where it was learnt.")


class Interface(BaseModel):
    """The ports (and header parameters) a testbench may drive and sample."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    module: str | None = Field(default=None, description="The module under test (toplevel).")
    block: str | None = Field(default=None, description="Its block.")
    source: PortSource | None = Field(
        default=None, description="Where the port list came from; None when none was found."
    )
    ports: tuple[InterfacePort, ...] = Field(default=(), description="The ports, in order.")
    parameters: tuple[InterfaceParameter, ...] = Field(
        default=(), description="The module header's parameters (from a declaration)."
    )
    clock: str | None = Field(default=None, description="The clock port, if known.")
    reset: str | None = Field(default=None, description="The reset port, if known.")
    reset_active_low: bool | None = Field(default=None, description="Whether reset is low.")
    notes: tuple[str, ...] = Field(default=(), description="What the reader should know.")
    truncated: bool = Field(default=False, description="Whether the declaration was cut.")

    @property
    def port_names(self) -> frozenset[str]:
        """Every port name."""
        return frozenset(p.name for p in self.ports)

    def view(self) -> dict[str, Any]:
        """The interface as a task's context shows it: unset port fields left out."""
        out = self.model_dump(mode="json", exclude={"schema_version", "ports", "parameters"})
        out["ports"] = [
            p.model_dump(mode="json", exclude_defaults=True, exclude={"schema_version"})
            | {"source": p.source}
            for p in self.ports
        ]
        out["parameters"] = [
            p.model_dump(mode="json", exclude_defaults=True, exclude={"schema_version"})
            | {"source": p.source}
            for p in self.parameters
        ]
        return out


DeclarationLoader = Callable[[str | None], ModuleDeclaration | None]
"""Reads the declaration of a module (None: the only one) from the RTL, on demand."""


# --- the model ---------------------------------------------------------------------------


def _spec_ports(model: DesignModel, block: str) -> list[PortEntity]:
    prefix = f"port:spec.{block}."
    ports = [p for p in model.by_kind("port") if isinstance(p, PortEntity)]
    found = [p for p in ports if p.key.startswith(prefix)]
    return sorted(found, key=lambda p: (p.source.line or 0, p.key))


def _module(model: DesignModel, name: str) -> ModuleEntity | None:
    for entity in model.by_kind("module"):
        if isinstance(entity, ModuleEntity) and entity.name == name:
            return entity
    return None


def _rtl_ports(model: DesignModel, module: str) -> list[PortEntity]:
    key = f"module:{module}"
    ports = [p for p in model.by_kind("port") if isinstance(p, PortEntity) and p.module == key]
    return sorted(ports, key=lambda p: (p.source.line or 0, p.key))


def top_module(model: DesignModel, block: str) -> str | None:
    """The block's top RTL module in the model: the one module it owns that no other
    module it owns instantiates (None when there is not exactly one)."""
    key = f"block:{block}"
    owned = [m for m in model.by_kind("module") if isinstance(m, ModuleEntity) and m.block == key]
    inner = {r.dst for m in owned for r in model.get_relations(src=m.key, kind="instantiates")}
    roots = [m for m in owned if m.key not in inner]
    return roots[0].name if len(roots) == 1 else None


def _block_of(model: DesignModel, module: str) -> str | None:
    entity = _module(model, module)
    if entity is None or entity.block is None:
        return None
    return entity.block.partition(":")[2] or None


def _block_entity(model: DesignModel, block: str) -> BlockEntity | None:
    entity = model.get(f"block:{block}")
    return entity if isinstance(entity, BlockEntity) else None


# --- clock and reset -----------------------------------------------------------------------


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return any(re.search(p, name, re.IGNORECASE) for p in patterns)


def _clock_reset(
    model: DesignModel | None, block: str | None, names: list[str]
) -> tuple[str | None, str | None, bool | None]:
    clock = reset = None
    active_low: bool | None = None
    if model is not None and block is not None:
        entity = _block_entity(model, block)
        if entity is not None:
            c, r = entity.attrs.get("clock"), entity.attrs.get("reset")
            clock = c if isinstance(c, str) and c in names else None
            reset = r if isinstance(r, str) and r in names else None
            if reset is not None:
                found = model.get(f"reset:{reset}")
                if isinstance(found, ResetEntity):
                    active_low = found.active_low
    if clock is None:
        clock = next((n for n in names if _matches(n, CLOCK_PATTERNS)), None)
    if reset is None:
        reset = next((n for n in names if n != clock and _matches(n, RESET_PATTERNS)), None)
    if reset is not None and active_low is None:
        active_low = re.search(r"(?:_n|_ni|_b|n)$", reset.lower()) is not None
    return clock, reset, active_low


# --- the provider --------------------------------------------------------------------------


def _from_declaration(decl: ModuleDeclaration) -> list[InterfacePort]:
    return [
        InterfacePort(
            name=p.name,
            direction=p.direction,
            width=p.width,
            type=p.type,
            packed=p.packed,
            unpacked=p.unpacked,
            source="rtl_declaration",
        )
        for p in decl.ports
    ]


def _parameters(decl: ModuleDeclaration | None) -> tuple[InterfaceParameter, ...]:
    if decl is None:
        return ()
    return tuple(
        InterfaceParameter(name=p.name, kind=p.kind, type=p.type, default=p.default)
        for p in decl.parameters
    )


def _differs(spec: PortEntity, direction: str | None, width: int | str | None) -> bool:
    if spec.direction is not None and direction is not None and spec.direction != direction:
        return True
    return isinstance(spec.width, int) and isinstance(width, int) and spec.width != width


def _spec_interface(
    spec_ports: list[PortEntity],
    rtl: list[PortEntity] | None,
    decl: ModuleDeclaration | None,
) -> tuple[list[InterfacePort], list[str]]:
    """The spec ports, each compared with the RTL's when the RTL's are known."""
    known: dict[str, tuple[str | None, int | str | None]] | None = None
    from_model = rtl is not None
    if rtl is not None:
        known = {p.name: (p.direction, p.width) for p in rtl}
    elif decl is not None:
        known = {p.name: (p.direction, p.width) for p in decl.ports}
    ports: list[InterfacePort] = []
    for spec in spec_ports:
        desc = spec.attrs.get("description")
        note: str | None = None
        rtl_value: dict[str, Any] | None = None
        if known is not None:
            if spec.name not in known:
                note = MISSING_IN_RTL
            elif _differs(spec, *known[spec.name]):
                note = RTL_DIFFERS
                if from_model:
                    rtl_value = {"direction": known[spec.name][0], "width": known[spec.name][1]}
        ports.append(
            InterfacePort(
                name=spec.name,
                direction=spec.direction,
                width=spec.width,
                description=desc if isinstance(desc, str) else "",
                source="spec",
                note=note,
                rtl=rtl_value,
            )
        )
    notes: list[str] = []
    if known is not None:
        extra = sorted(set(known) - {p.name for p in spec_ports})
        if extra and from_model:
            notes.append(
                "the RTL has ports the spec does not list (not part of this interface; "
                f"ports_diff reports them): {', '.join(extra)}"
            )
        elif extra:
            notes.append(
                f"the RTL declares {len(extra)} port(s) the spec does not list (not part of "
                "this interface; ports_diff reports them)"
            )
    if any(p.note for p in ports):
        notes.append(
            f"ports marked '{RTL_DIFFERS}' or '{MISSING_IN_RTL}' disagree with the RTL; the "
            "spec is what the test checks, ports_diff reports the difference"
        )
    return ports, notes


def interface_for(
    module: str | None,
    *,
    model: DesignModel | None = None,
    block: str | None = None,
    declaration: DeclarationLoader | ModuleDeclaration | None = None,
) -> Interface:
    """The interface of `module` (or of `block`'s top module), learnt without the RTL body.

    `declaration` is called (with the module name, or None for "the only module") only
    when the model has no RTL ports of the module: for the port list when the spec has
    none either, else to compare the spec ports with.
    """
    if model is not None and block is None and module is not None:
        block = _block_of(model, module)
    if model is not None and module is None and block is not None:
        module = top_module(model, block)

    def load() -> ModuleDeclaration | None:
        if declaration is None or isinstance(declaration, ModuleDeclaration):
            return declaration
        return declaration(module)

    spec_ports = _spec_ports(model, block) if model is not None and block is not None else []
    rtl_ports = _rtl_ports(model, module) if model is not None and module is not None else []
    decl: ModuleDeclaration | None = None
    notes: list[str] = []
    source: PortSource | None
    if spec_ports:
        source = "spec"
        if not rtl_ports:
            decl = load()
        ports, notes = _spec_interface(spec_ports, rtl_ports or None, decl)
    elif rtl_ports:
        source = "model_rtl"
        ports = [
            InterfacePort(name=p.name, direction=p.direction, width=p.width, source="model_rtl")
            for p in rtl_ports
        ]
        notes.append("the spec lists no ports for this block: the RTL's declared ports are used")
    else:
        decl = load()
        if decl is not None:
            source = "rtl_declaration"
            ports = _from_declaration(decl)
            notes.append(
                "neither the spec nor the Design Model lists this module's ports: they were "
                "read from its declaration (header only)"
            )
        else:
            source = None
            ports = []
            notes.append(
                "no interface found: the spec lists no ports, the Design Model has none "
                "(run `chipgraph ingest`), and no RTL declaration of the module was found; "
                "report needs_human"
            )
    if decl is not None and module is None:
        module = decl.module
    clock, reset, active_low = _clock_reset(model, block, [p.name for p in ports])
    return Interface(
        module=module,
        block=block,
        source=source,
        ports=tuple(ports),
        parameters=_parameters(decl),
        clock=clock,
        reset=reset,
        reset_active_low=active_low,
        notes=tuple(notes),
        truncated=decl.truncated if decl is not None else False,
    )


# --- the spec slice ------------------------------------------------------------------------


def _where(entity: Any) -> str | None:
    src = entity.source
    if src.file is None:
        return None
    return f"{src.file}:{src.line}" if src.line else src.file


def _requirement_keys(model: DesignModel, block: str) -> list[str]:
    key = f"block:{block}"
    keys = {
        r.key
        for r in model.by_kind("requirement")
        if isinstance(r, RequirementEntity) and r.attrs.get("block") == key
    }
    return sorted(keys)


def block_requirements(model: DesignModel, block: str) -> list[dict[str, Any]]:
    """The block's requirements: id, text and where the spec states them."""
    found: list[dict[str, Any]] = []
    for key in _requirement_keys(model, block):
        req = model.get(key)
        if not isinstance(req, RequirementEntity):
            continue
        found.append(
            {
                "id": req.name,
                "text": req.text or "",
                "where": _where(req),
                "id_source": req.attrs.get("id_source"),
            }
        )
    return found


def spec_slice(model: DesignModel, block: str) -> dict[str, Any]:
    """The block's spec side of the Design Model (see the module docstring)."""
    key = f"block:{block}"
    entity = _block_entity(model, block)
    registers: list[dict[str, Any]] = []
    for reg in sorted(
        (r for r in model.by_kind("register") if isinstance(r, RegisterEntity) and r.block == key),
        key=lambda r: (str(r.offset), r.name),
    ):
        fields = [
            {
                "name": f.name,
                "msb": f.msb,
                "lsb": f.lsb,
                "access": f.access,
                "reset": f.reset_value,
            }
            for f in sorted(
                (
                    f
                    for f in model.by_kind("field")
                    if isinstance(f, FieldEntity) and f.register == reg.key
                ),
                key=lambda f: (f.lsb or 0, f.name),
            )
        ]
        registers.append(
            {
                "name": reg.name,
                "offset": reg.offset,
                "access": reg.access,
                "reset": reg.reset_value,
                "width": reg.width,
                "fields": fields,
                "where": _where(reg),
            }
        )
    interrupts = [
        {"name": i.name, "line": i.line}
        for i in model.by_kind("interrupt")
        if isinstance(i, InterruptEntity) and i.block == key
    ]
    regions = [
        {"name": m.name, "base": m.base, "size": m.size}
        for m in model.by_kind("memory_region")
        if isinstance(m, MemoryRegionEntity) and m.block == key
    ]
    attrs = entity.attrs if entity is not None else {}
    return {
        "block": block,
        "bus": attrs.get("bus"),
        "clock": attrs.get("clock"),
        "reset": attrs.get("reset"),
        "requirements": block_requirements(model, block),
        "registers": registers,
        "interrupts": interrupts,
        "memory_regions": regions,
    }


# --- a project's sources -------------------------------------------------------------------


def _fill(template: str, block: str | None) -> str:
    if block is None:
        return template
    return template.replace("{block}", block).replace("{BLOCK}", block.upper())


def _templates(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v]
    return []


def _filelist_sources(root: Path, rel: str) -> list[Path]:
    path = root / rel
    if not path.is_file():
        return []
    found: list[Path] = []
    styles: tuple[Literal["filelist", "cwd"], ...] = ("filelist", "cwd")
    for style in styles:
        try:
            listed = read_filelist(path, relative_to=style, root=root)
        except FilelistError:
            continue
        found.extend(p for p in listed.sources if p.is_file() and p not in found)
    return found


def rtl_files(
    root: Path,
    *,
    model: DesignModel | None,
    module: str | None,
    block: str | None,
    args: Mapping[str, Any],
    layout: Mapping[str, Any],
) -> list[Path]:
    """The RTL files a declaration of `module` may be read from, in order."""
    files: list[Path] = []

    def add(path: Path) -> None:
        if path.is_file() and path not in files:
            files.append(path)

    if model is not None and module is not None:
        entity = _module(model, module)
        if entity is not None and entity.file:
            add(root / entity.file)
    for template in _templates(args.get("files")):
        for path in sorted(root.glob(_fill(template, block))):
            add(path)
    filelists = _templates(args.get("filelist")) or _templates(layout.get("filelist"))
    for template in filelists:
        for path in _filelist_sources(root, _fill(template, block)):
            add(path)
    for template in _templates(layout.get("rtl")):
        for path in sorted(root.glob(_fill(template, block))):
            add(path)
    return files


def project_interface(
    root: Path,
    *,
    model: DesignModel | None,
    block: str | None,
    params: Mapping[str, str],
    args: Mapping[str, Any],
    layout: Mapping[str, Any],
) -> Interface:
    """The interface of a testbench task in a project (see the module docstring).

    `args` are the profile's `tb_static` adapter args (`top`, `files`, `filelist`),
    `layout` the profile's layout for the block, `params` the rule instance's.
    """
    module = params.get("module") or None
    if module is None and model is not None and block is not None:
        module = top_module(model, block)
    if module is None:
        top = args.get("top")
        module = _fill(top, block) if isinstance(top, str) and top else None

    def load(name: str | None) -> ModuleDeclaration | None:
        files = rtl_files(root, model=model, module=name, block=block, args=args, layout=layout)
        return extract_declaration(files, name)

    return interface_for(module, model=model, block=block, declaration=load)


__all__ = [
    "MISSING_IN_RTL",
    "RTL_DIFFERS",
    "DeclarationLoader",
    "Interface",
    "InterfaceParameter",
    "InterfacePort",
    "PortSource",
    "block_requirements",
    "interface_for",
    "project_interface",
    "rtl_files",
    "spec_slice",
    "top_module",
]

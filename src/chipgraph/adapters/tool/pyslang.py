"""RTL extractor using pyslang 11 as the parser.

Reads a Verilog/SystemVerilog design via filelists or source paths, extracts hierarchy,
ports (with directions and widths), parameters, instances, clock/reset signals (detected
by name patterns from the profile), and optionally FSM facts. Returns a `DesignModel`
with entities (module, port, parameter, clock, reset) and relations (contains, instantiates).

Diagnostics (parse errors, warnings) are returned alongside, never raised.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple

from chipgraph.adapters.tool.filelist import Filelist, FilelistError, read_filelist
from chipgraph.core.model.entities import (
    ClockEntity,
    EntityBase,
    ModuleEntity,
    ParameterEntity,
    PortEntity,
    ResetEntity,
)
from chipgraph.core.model.keys import make_key
from chipgraph.core.model.model import DesignModel
from chipgraph.core.model.provenance import Provenance
from chipgraph.core.model.relations import Relation

# pyslang 11 ships stubs that do not parse (a parameter named `with` in syntax.pyi), and
# mypy reads any stub it can reach. Importing through importlib keeps it untyped (Any).
ast: Any = importlib.import_module("pyslang.ast")
_driver: Any = importlib.import_module("pyslang.driver")
CommandLineOptions: Any = _driver.CommandLineOptions
Driver: Any = _driver.Driver


class ParseDiagnostic(NamedTuple):
    """A parse error or warning with file:line context."""

    file: str | None
    line: int | None
    severity: str  # "error" or "warning"
    message: str


@dataclass(frozen=True, slots=True)
class PyslangExtractorOptions:
    """Options for pyslang extraction."""

    timescale: str = "1ns/1ps"
    """Default timescale if source files lack one."""

    clock_patterns: tuple[str, ...] = (r"^i_clk", r"^clk")
    """Regex patterns (anchored) to detect clock ports by name."""

    reset_patterns: tuple[str, ...] = (r"^i_rst", r"^rst")
    """Regex patterns (anchored) to detect reset ports by name."""

    defines: dict[str, str | None] | None = None
    """Additional preprocessor definitions."""

    tops: tuple[str, ...] | None = None
    """Specific top modules to elaborate. If None, all are elaborated."""

    root: Path | None = None
    """Project root: provenance paths under it are written relative to it."""

    block: str | None = None
    """The model key prefix for `contains` relations (e.g., 'block:timer')."""


class PyslangExtractor:
    """Extracts RTL facts from a filelist using pyslang 11 as the elaborator."""

    name = "pyslang"

    def extract(self, path: Path) -> Iterable[Mapping[str, object]]:
        """The `Extractor` protocol: `path` is a filelist (paths relative to it) or one
        source file; returns every entity and relation of the model as plain mappings.
        """
        source: Path | Filelist = read_filelist(path) if path.suffix == ".f" else path
        model, _ = self.extract_model(source if isinstance(source, Filelist) else [source])
        facts: list[Mapping[str, object]] = [
            e.model_dump(mode="json") for e in sorted(model.entities.values(), key=_by_key)
        ]
        facts.extend(r.model_dump(mode="json") for r in model.relations)
        return facts

    @classmethod
    def extract_model(
        cls,
        filelist_or_sources: Path | Filelist | Iterable[Path],
        *,
        tops: tuple[str, ...] | None = None,
        block: str | None = None,
        timescale: str = "1ns/1ps",
        defines: dict[str, str | None] | None = None,
        clock_patterns: tuple[str, ...] = (r"^i_clk", r"^clk"),
        reset_patterns: tuple[str, ...] = (r"^i_rst", r"^rst"),
        root: Path | None = None,
    ) -> tuple[DesignModel, tuple[ParseDiagnostic, ...]]:
        """Extract RTL hierarchy, ports, parameters from a filelist or source paths.

        Args:
            filelist_or_sources: A Path to a filelist, a Filelist object, or an Iterable
                of source file Paths.
            tops: Specific top-level modules to elaborate; if None, all are used.
            block: Model key prefix for `contains` relations (e.g., "block:timer").
            timescale: Default timescale (e.g., "1ns/1ps").
            defines: Additional preprocessor definitions.
            clock_patterns: Regex patterns to detect clock ports by name.
            reset_patterns: Regex patterns to detect reset ports by name.

        Returns:
            A tuple of (DesignModel, diagnostics). Diagnostics are never raised;
            parse errors are returned alongside the partial model.
        """
        opts = PyslangExtractorOptions(
            timescale=timescale,
            clock_patterns=clock_patterns,
            reset_patterns=reset_patterns,
            defines=defines,
            tops=tops,
            block=block,
            root=root,
        )

        # Parse filelist if a Path is given.
        filelist = cls._read_filelist(filelist_or_sources)

        # Run pyslang elaboration.
        diagnostics, root_sym, src_mgr = cls._elaborate(filelist, opts)

        # Walk the hierarchy and extract entities and relations.
        entities, relations = cls._walk_hierarchy(root_sym, src_mgr, filelist, opts)

        # Build and return the model.
        model = DesignModel.build(entities, relations)
        return model, tuple(diagnostics)

    @classmethod
    def _read_filelist(cls, filelist_or_sources: Path | Filelist | Iterable[Path]) -> Filelist:
        """Normalize input to a Filelist."""
        if isinstance(filelist_or_sources, Filelist):
            return filelist_or_sources
        if isinstance(filelist_or_sources, Path):
            try:
                return read_filelist(filelist_or_sources)
            except FilelistError as exc:
                raise ValueError(f"failed to read filelist: {exc}") from exc
        sources = tuple(Path(p) for p in filelist_or_sources)
        return Filelist(sources=sources)

    @classmethod
    def _elaborate(
        cls, filelist: Filelist, opts: PyslangExtractorOptions
    ) -> tuple[list[ParseDiagnostic], object, object]:
        """Run pyslang elaboration on the filelist."""
        diags: list[ParseDiagnostic] = []

        driver = Driver()
        driver.addStandardArgs()

        # Build command line arguments as space-separated string.
        args = ["slang", "--timescale", opts.timescale, "--ignore-unknown-modules", "-Wnone"]

        # Add sources.
        for src in filelist.sources:
            args.append(str(src))

        # Add include directories.
        for inc in filelist.incdirs:
            args.append(f"+incdir+{inc}")

        # Add defines from filelist.
        for name, value in filelist.defines.items():
            if value is not None:
                args.append(f"+define+{name}={value}")
            else:
                args.append(f"+define+{name}")

        # Add defines from options.
        if opts.defines:
            for name, value in opts.defines.items():
                if value is not None:
                    args.append(f"+define+{name}={value}")
                else:
                    args.append(f"+define+{name}")

        # Add top modules if specified.
        if opts.tops:
            for top in opts.tops:
                args.append("--top")
                args.append(top)

        # Parse command line.
        cmdline = " ".join(args)
        if not driver.parseCommandLine(cmdline, CommandLineOptions()):
            msg = "failed to parse command line"
            diags.append(ParseDiagnostic(file=None, line=None, severity="error", message=msg))
            return diags, None, None

        driver.processOptions()

        # Parse sources.
        if not driver.parseAllSources():
            msg = "failed to parse sources"
            diags.append(ParseDiagnostic(file=None, line=None, severity="error", message=msg))
            return diags, None, None

        # Elaborate.
        comp = driver.createCompilation()
        src_mgr = comp.sourceManager

        # Collect diagnostics.
        for diag in comp.getAllDiagnostics():
            severity = "error" if diag.isError() else "warning"
            file = src_mgr.getFileName(diag.location) if diag.location else None
            line = src_mgr.getLineNumber(diag.location) if diag.location else None
            # Get diagnostic message - just use the message string.
            msg_str = diag.message if hasattr(diag, "message") else str(diag)
            diags.append(ParseDiagnostic(file=file, line=line, severity=severity, message=msg_str))

        root_sym = comp.getRoot()
        return diags, root_sym, src_mgr

    @classmethod
    def _walk_hierarchy(
        cls,
        root_sym: Any,
        src_mgr: Any,
        filelist: Filelist,
        opts: PyslangExtractorOptions,
    ) -> tuple[list[EntityBase], list[Relation]]:
        """Walk the hierarchy and extract entities and relations."""
        entities: list[EntityBase] = []
        relations: list[Relation] = []
        seen_modules: set[str] = set()

        # Compile clock and reset pattern matchers.
        clock_matchers = [re.compile(p) for p in opts.clock_patterns]
        reset_matchers = [re.compile(p) for p in opts.reset_patterns]

        def matches_any(name: str, matchers: list[re.Pattern[str]]) -> bool:
            return any(m.match(name) for m in matchers)

        # Walk top instances.
        if root_sym is None or not hasattr(root_sym, "topInstances"):
            return entities, relations

        for top_inst in root_sym.topInstances:
            _walk_instance(
                top_inst,
                src_mgr,
                entities,
                relations,
                seen_modules,
                clock_matchers,
                reset_matchers,
                opts,
                is_top=True,
            )

        return entities, relations


def _walk_instance(
    inst: Any,
    src_mgr: Any,
    entities: list[EntityBase],
    relations: list[Relation],
    seen_modules: set[str],
    clock_matchers: list[re.Pattern[str]],
    reset_matchers: list[re.Pattern[str]],
    opts: PyslangExtractorOptions,
    *,
    is_top: bool = False,
) -> None:
    """Recursively walk an instance and its children.

    Clock and reset entities come only from the top module's ports: a port of a child
    module that matches the patterns is marked (`attrs["clock_like"]` /
    `attrs["reset_like"]`), but which top clock drives it needs connection tracing,
    which is not done here.
    """
    if not hasattr(inst, "body") or not hasattr(inst, "name"):
        return

    body = inst.body
    definition = getattr(body, "definition", None)
    def_name = definition.name if definition is not None else "unknown"

    def locate(sym: Any) -> tuple[str | None, int | None]:
        loc = getattr(sym, "location", None)
        if not loc:
            return None, None
        return _display_path(src_mgr.getFileName(loc), opts.root), src_mgr.getLineNumber(loc)

    # A module is described once, from its definition: ports and parameters of later
    # instances of the same module are not repeated (the first elaboration wins, so a
    # parameterised module keeps the widths of its first instance).
    module_key = make_key("module", def_name)
    first_visit = module_key not in seen_modules
    file, line = locate(definition if definition is not None else inst)
    if first_visit:
        seen_modules.add(module_key)
        module_entity = ModuleEntity(
            key=module_key,
            name=def_name,
            file=file,
            block=opts.block,
            source=Provenance(file=file, line=line, extractor="pyslang"),
        )
        entities.append(module_entity)

    # Extract ports.
    if first_visit and hasattr(body, "portList"):
        for port_sym in body.portList:
            if not hasattr(port_sym, "kind"):
                continue
            if port_sym.kind != ast.SymbolKind.Port:
                continue

            port_name = port_sym.name if hasattr(port_sym, "name") else None
            if not port_name:
                continue
            file, line = locate(port_sym)

            # Determine direction.
            direction: Literal["input", "output", "inout"] | None = None
            if hasattr(port_sym, "direction"):
                # Extract direction from enum like "ArgumentDirection.Out" -> "Out"
                dir_str = str(port_sym.direction).split(".")[-1].lower()
                # Map direction names to standard port directions
                if dir_str == "in":
                    direction = "input"
                elif dir_str == "out":
                    direction = "output"
                elif dir_str in ("inout", "ref"):
                    direction = "inout"

            # Determine width.
            width = None
            if hasattr(port_sym, "type"):
                type_str = str(port_sym.type)
                # Try to extract width from type string (e.g., "logic[31:0]").
                match = re.search(r"\[(\d+):0\]", type_str)
                width = int(match.group(1)) + 1 if match else 1

            # Detect clock and reset by name.
            clock_key = None
            reset_key = None
            port_attrs: dict[str, bool] = {}
            is_clock = any(m.match(port_name) for m in clock_matchers)
            is_reset = any(m.match(port_name) for m in reset_matchers)
            if is_clock and is_top:
                clock_key = make_key("clock", port_name)
                entities.append(
                    ClockEntity(
                        key=clock_key,
                        name=port_name,
                        source=Provenance(file=file, line=line, extractor="pyslang"),
                    )
                )
            elif is_clock:
                port_attrs["clock_like"] = True
            if is_reset and is_top:
                active_low = "_n" in port_name or "_inv" in port_name
                reset_key = make_key("reset", port_name)
                entities.append(
                    ResetEntity(
                        key=reset_key,
                        name=port_name,
                        active_low=active_low,
                        source=Provenance(file=file, line=line, extractor="pyslang"),
                    )
                )
            elif is_reset:
                port_attrs["reset_like"] = True

            port_key = make_key("port", def_name, port_name)
            port_entity = PortEntity(
                key=port_key,
                name=port_name,
                direction=direction,
                width=width,
                clock=clock_key,
                reset=reset_key,
                module=module_key,
                attrs=dict(port_attrs),
                source=Provenance(file=file, line=line, extractor="pyslang"),
            )
            entities.append(port_entity)

    # Extract parameters.
    if first_visit and hasattr(body, "parameters"):
        for param_sym in body.parameters:
            if not hasattr(param_sym, "name"):
                continue
            param_name = param_sym.name
            file, line = locate(param_sym)
            param_value = None
            if hasattr(param_sym, "value"):
                param_value = str(param_sym.value)

            param_key = make_key("parameter", def_name, param_name)
            param_entity = ParameterEntity(
                key=param_key,
                name=param_name,
                value=param_value,
                module=module_key,
                source=Provenance(file=file, line=line, extractor="pyslang"),
            )
            entities.append(param_entity)

    # Extract instantiations and recurse.
    if hasattr(body, "visit"):

        def visit_fn(sym: object) -> object:
            if not hasattr(sym, "kind"):
                return ast.VisitAction.Advance
            if sym.kind == ast.SymbolKind.Instance:
                child_inst = sym
                if hasattr(child_inst, "name") and hasattr(child_inst, "body"):
                    child_def_name = (
                        child_inst.body.definition.name
                        if hasattr(child_inst.body, "definition")
                        else "unknown"
                    )
                    child_module_key = make_key("module", child_def_name)

                    # Create instantiates relation.
                    rel_attrs = {}
                    if hasattr(child_inst, "name"):
                        rel_attrs["instance_name"] = child_inst.name
                    if file:
                        rel_attrs["file"] = file
                    if line:
                        rel_attrs["line"] = line

                    relation = Relation(
                        kind="instantiates",
                        src=module_key,
                        dst=child_module_key,
                        attrs=rel_attrs,
                        source=Provenance(file=file, line=line, extractor="pyslang"),
                    )
                    relations.append(relation)

                    # Recurse.
                    _walk_instance(
                        child_inst,
                        src_mgr,
                        entities,
                        relations,
                        seen_modules,
                        clock_matchers,
                        reset_matchers,
                        opts,
                    )
                    return ast.VisitAction.Skip
            return ast.VisitAction.Advance

        with suppress(Exception):
            # Silently skip if visit fails on this subtree.
            body.visit(visit_fn)


def _display_path(raw: str, root: Path | None) -> str:
    """`raw` relative to `root` (POSIX) when it is under it, else absolute."""
    path = Path(raw).resolve()
    if root is not None:
        with suppress(ValueError):
            return path.relative_to(root.resolve()).as_posix()
    return path.as_posix()


def _by_key(entity: EntityBase) -> str:
    return entity.key

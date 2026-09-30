"""Elaborate a block's RTL with pyslang and expose the facts the CDC check needs.

The Design Model does not hold nets or connections (see `adapters/tool/pyslang.py`), so
the structural CDC check must look at the RTL itself. This module runs a real pyslang
*elaboration* (unlike `naming`/`connect`, which parse a single file's syntax tree) so
that named references resolve to symbols, instance port connections are known, and one
level of hierarchy can be followed.

Everything here is pure and deterministic and never raises on malformed input: parse or
elaboration errors come back as `Diagnostic`s alongside a best-effort model, never thrown.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyslang import Bag
from pyslang.ast import (
    Compilation,
    CompilationFlags,
    CompilationOptions,
    EdgeKind,
    ExpressionKind,
    StatementKind,
    SymbolKind,
    TimingControlKind,
    VisitAction,
)
from pyslang.syntax import SyntaxTree


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A parse/elaboration error, with a repo-relative file and 1-based line when known."""

    file: str | None
    line: int | None
    message: str


@dataclass(frozen=True, slots=True)
class Loc:
    """A source location: repo-relative file and 1-based line."""

    file: str | None
    line: int | None


@dataclass(slots=True)
class Register:
    """A variable assigned by an `always_ff`, with the clock root it is driven by."""

    path: str
    """The elaborated hierarchical path, a stable identity (e.g. `top.a_q`)."""

    name: str
    clock_root: str | None
    """The module input clock port name the driving `always_ff` samples, or None."""

    loc: Loc
    reads: frozenset[str] = frozenset()
    """Hierarchical paths this register's next-state expression reads (before comb expansion)."""


@dataclass(slots=True)
class CombNode:
    """A combinationally-driven signal (an `assign` LHS or `always_comb` target)."""

    path: str
    reads: set[str] = field(default_factory=set)


@dataclass(slots=True)
class SubInstance:
    """A child module instance and its named port connections (one hierarchy level)."""

    name: str
    module: str
    loc: Loc
    is_sync: bool
    inputs: dict[str, frozenset[str]] = field(default_factory=dict)
    """Input port name -> hierarchical paths the connected expression reads (parent scope)."""

    outputs: dict[str, str] = field(default_factory=dict)
    """Output port name -> the single parent hierarchical path it drives, when it is one."""

    clock_inputs: dict[str, str | None] = field(default_factory=dict)
    """Clock-like input port name -> parent path it is connected to, when resolvable."""


@dataclass(slots=True)
class ModuleFacts:
    """The CDC-relevant facts of one elaborated module instance."""

    name: str
    registers: dict[str, Register] = field(default_factory=dict)
    comb: dict[str, CombNode] = field(default_factory=dict)
    sub_instances: list[SubInstance] = field(default_factory=list)
    clock_roots: set[str] = field(default_factory=set)
    """Input port names used as a clock by some `always_ff` in this module."""


@dataclass(slots=True)
class Elaboration:
    """The result of elaborating a block: per-top-module facts and any diagnostics."""

    modules: dict[str, ModuleFacts] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()


def elaborate(
    sources: Iterable[Path],
    *,
    sync_cells: frozenset[str],
    clock_matchers: Iterable[Any],
    repo_root: Path | None = None,
    tops: Iterable[str] | None = None,
) -> Elaboration:
    """Elaborate `sources` together and extract CDC facts for each top module.

    `sync_cells` are the module names to treat as synchronisers; `clock_matchers` are
    compiled regexes that recognise a clock-like port by name. `tops`, when given, limits
    which modules become analysis roots; otherwise every uninstantiated module is a top.
    """
    comp = Compilation(_ignore_unknown_modules())
    diags: list[Diagnostic] = []
    for src in sorted(set(sources)):
        try:
            text = src.read_text(errors="ignore")
        except OSError as exc:
            diags.append(Diagnostic(file=_rel(src, repo_root), line=None, message=str(exc)))
            continue
        comp.addSyntaxTree(SyntaxTree.fromText(text, src.as_posix()))

    sm = comp.sourceManager
    diags.extend(_diagnostics(comp, sm, repo_root))

    root = comp.getRoot()
    top_names = set(tops) if tops is not None else None
    matchers = list(clock_matchers)
    modules: dict[str, ModuleFacts] = {}
    for top in getattr(root, "topInstances", ()):
        def_name = _definition_name(top)
        if top_names is not None and def_name not in top_names:
            continue
        facts = _extract_module(top, sm, sync_cells, matchers, repo_root)
        modules.setdefault(facts.name, facts)
    return Elaboration(modules=modules, diagnostics=tuple(diags))


# --------------------------------------------------------------------------------------
# per-module extraction
# --------------------------------------------------------------------------------------


def _ignore_unknown_modules() -> Bag:
    """Compilation options that treat a module instantiated but not in scope as a blackbox.

    A per-block scope holds only the block's own files; the child IP a wrapper
    instantiates lives in another block. Ignoring unknown modules (as the pyslang
    extractor does) lets the block elaborate, with the missing child as an unanalysed
    blackbox (its output resolves to `not_analysed`, an `info`), instead of an error.
    """
    opts = CompilationOptions()
    opts.flags = CompilationFlags.IgnoreUnknownModules
    return Bag([opts])


def _extract_module(
    inst: Any,
    sm: Any,
    sync_cells: frozenset[str],
    clock_matchers: list[Any],
    repo_root: Path | None,
) -> ModuleFacts:
    facts = ModuleFacts(name=_definition_name(inst))
    for member in inst.body:
        kind = getattr(member, "kind", None)
        if kind == SymbolKind.ProceduralBlock:
            _extract_procedural(member, sm, facts, repo_root)
        elif kind == SymbolKind.ContinuousAssign:
            _extract_continuous_assign(member, facts)
        elif kind == SymbolKind.Instance:
            sub = _extract_instance(member, sm, sync_cells, clock_matchers, repo_root)
            if sub is not None:
                facts.sub_instances.append(sub)
    return facts


def _extract_procedural(block: Any, sm: Any, facts: ModuleFacts, repo_root: Path | None) -> None:
    """Extract registers (always_ff) or comb nodes (always_comb/always) from a block."""
    proc_kind = str(getattr(block, "procedureKind", ""))
    body = getattr(block, "body", None)
    if body is None:
        return

    is_ff = proc_kind.endswith("AlwaysFF")
    clock_root: str | None = None
    inner = body
    if getattr(body, "kind", None) == StatementKind.Timed:
        if is_ff:
            clock_root = _clock_root_of(getattr(body, "timing", None))
            if clock_root is not None:
                facts.clock_roots.add(clock_root)
        inner = getattr(body, "stmt", body)

    for lhs, reads, loc in _assignments(inner, sm, repo_root):
        if is_ff:
            existing = facts.registers.get(lhs.path)
            merged_reads = (existing.reads if existing else frozenset()) | reads
            facts.registers[lhs.path] = Register(
                path=lhs.path,
                name=lhs.name,
                clock_root=(
                    clock_root if existing is None else (existing.clock_root or clock_root)
                ),
                loc=existing.loc if existing is not None else loc,
                reads=merged_reads,
            )
        else:
            node = facts.comb.setdefault(lhs.path, CombNode(path=lhs.path))
            node.reads |= reads


def _extract_continuous_assign(member: Any, facts: ModuleFacts) -> None:
    assignment = getattr(member, "assignment", None)
    if assignment is None or getattr(assignment, "kind", None) != ExpressionKind.Assignment:
        return
    lhs = _lvalue_target(getattr(assignment, "left", None))
    if lhs is None:
        return
    reads = _named_reads(getattr(assignment, "right", None))
    node = facts.comb.setdefault(lhs.path, CombNode(path=lhs.path))
    node.reads |= reads


def _extract_instance(
    inst: Any,
    sm: Any,
    sync_cells: frozenset[str],
    clock_matchers: list[Any],
    repo_root: Path | None,
) -> SubInstance | None:
    module = _definition_name(inst)
    if not module:
        return None
    sub = SubInstance(
        name=getattr(inst, "name", "") or "",
        module=module,
        loc=_loc(inst, sm, repo_root),
        is_sync=module in sync_cells,
    )
    body = getattr(inst, "body", None)
    port_list = getattr(body, "portList", ()) if body is not None else ()
    for port in port_list:
        try:
            conn = inst.getPortConnection(port)
        except Exception:  # pragma: no cover - defensive
            conn = None
        if conn is None:
            continue
        expr = getattr(conn, "expression", None)
        direction = str(getattr(port, "direction", "")).split(".")[-1]
        pname = getattr(port, "name", "") or ""
        if direction == "In":
            reads = _named_reads(expr)
            sub.inputs[pname] = reads
            if any(m.match(pname) for m in clock_matchers):
                sub.clock_inputs[pname] = next(iter(sorted(reads)), None)
        elif direction == "Out":
            target = _lvalue_target(expr)
            if target is not None:
                sub.outputs[pname] = target.path
    return sub


# --------------------------------------------------------------------------------------
# expression / statement walking
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Target:
    path: str
    name: str


def _assignments(
    stmt: Any, sm: Any, repo_root: Path | None
) -> list[tuple[_Target, frozenset[str], Loc]]:
    """Every assignment reachable from `stmt`: its LHS target, RHS reads and location."""
    out: list[tuple[_Target, frozenset[str], Loc]] = []

    def visit(node: Any) -> Any:
        if getattr(node, "kind", None) == ExpressionKind.Assignment:
            target = _lvalue_target(getattr(node, "left", None))
            if target is not None:
                reads = _named_reads(getattr(node, "right", None))
                out.append((target, reads, _loc(node, sm, repo_root)))
        return VisitAction.Advance

    _safe_visit(stmt, visit)
    return out


def _lvalue_target(expr: Any) -> _Target | None:
    """The single named variable an l-value writes (peeling selects to one name)."""
    names = _named_symbols(expr)
    if len(names) != 1:
        return None
    path, name = names[0]
    return _Target(path=path, name=name)


def _named_reads(expr: Any) -> frozenset[str]:
    """The hierarchical paths of every `NamedValue` read in `expr`."""
    return frozenset(path for path, _ in _named_symbols(expr))


def _named_symbols(expr: Any) -> list[tuple[str, str]]:
    """Every `(hierarchical_path, name)` referenced by a `NamedValue` in `expr`."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def visit(node: Any) -> Any:
        if getattr(node, "kind", None) == ExpressionKind.NamedValue:
            sym = getattr(node, "symbol", None)
            path = getattr(sym, "hierarchicalPath", None)
            if isinstance(path, str) and path not in seen:
                seen.add(path)
                out.append((path, getattr(sym, "name", "") or ""))
        return VisitAction.Advance

    _safe_visit(expr, visit)
    return out


def _clock_root_of(timing: Any) -> str | None:
    """The clock a posedge/negedge event samples: the signal name of its first edge event.

    A reset event in the same list (`negedge rst_n`) is left for the caller's domain
    logic to ignore -- a reset name never becomes a register's domain root because it is
    not itself a clock any `always_ff` is keyed on for its data.
    """
    if timing is None:
        return None
    kind = getattr(timing, "kind", None)
    events: list[Any] = []
    if kind == TimingControlKind.SignalEvent:
        events = [timing]
    elif kind == TimingControlKind.EventList:
        events = list(getattr(timing, "events", ()))
    for ev in events:
        edge = getattr(ev, "edge", None)
        if edge not in (EdgeKind.PosEdge, EdgeKind.NegEdge):
            continue
        expr = getattr(ev, "expr", None)
        if getattr(expr, "kind", None) == ExpressionKind.NamedValue:
            sym = getattr(expr, "symbol", None)
            name = getattr(sym, "name", None)
            if isinstance(name, str) and name:
                return name
    return None


def _safe_visit(node: Any, visit: Any) -> None:
    if node is None:
        return
    with suppress(Exception):  # pragma: no cover - defensive against odd nodes
        node.visit(visit)


def _definition_name(inst: Any) -> str:
    body = getattr(inst, "body", None)
    definition = getattr(body, "definition", None)
    name = getattr(definition, "name", None)
    return name if isinstance(name, str) else ""


def _loc(node: Any, sm: Any, repo_root: Path | None) -> Loc:
    loc = getattr(node, "location", None)
    if not loc:
        source_range = getattr(node, "sourceRange", None)
        loc = getattr(source_range, "start", None) if source_range is not None else None
    if not loc:
        return Loc(file=None, line=None)
    try:
        raw = sm.getFileName(loc)
        line = sm.getLineNumber(loc)
    except Exception:  # pragma: no cover - defensive
        return Loc(file=None, line=None)
    return Loc(file=_rel_str(raw, repo_root), line=int(line) if line else None)


def _diagnostics(comp: Any, sm: Any, repo_root: Path | None) -> list[Diagnostic]:
    """Genuine syntax errors of the scope, as `Diagnostic`s (never raised).

    Only `Parser`/`Lexer` subsystem errors count -- those are malformed SystemVerilog.
    A partial per-block scope is elaborated without every package or child module it
    needs, so `Lookup`/`Elaboration` diagnostics (an unknown package, an unresolved name
    from a file not in scope) are expected and ignored, exactly as `naming`'s single-file
    parse ignores `Preprocessor` diagnostics. Unknown *modules* are already tolerated by
    the `IgnoreUnknownModules` flag.
    """
    out: list[Diagnostic] = []
    try:
        all_diags = comp.getAllDiagnostics()
    except Exception:  # pragma: no cover - defensive
        return out
    for diag in all_diags:
        if not diag.isError():
            continue
        try:
            subsystem = str(diag.code.getSubsystem()).rsplit(".", 1)[-1]
        except Exception:  # pragma: no cover - defensive
            subsystem = ""
        if subsystem not in ("Parser", "Lexer"):
            continue
        loc = getattr(diag, "location", None)
        file = _rel_str(sm.getFileName(loc), repo_root) if loc else None
        line = sm.getLineNumber(loc) if loc else None
        out.append(Diagnostic(file=file, line=int(line) if line else None, message=str(diag.code)))
    return out


def _rel(path: Path, repo_root: Path | None) -> str:
    result = _rel_str(path.as_posix(), repo_root)
    return result if result is not None else path.as_posix()


def _rel_str(raw: str | None, repo_root: Path | None) -> str | None:
    if raw is None:
        return None
    if repo_root is None:
        return raw
    try:
        return Path(raw).resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return raw


__all__ = [
    "CombNode",
    "Diagnostic",
    "Elaboration",
    "Loc",
    "ModuleFacts",
    "Register",
    "SubInstance",
    "elaborate",
]

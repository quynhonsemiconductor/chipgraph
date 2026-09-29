"""Classify the identifiers a SystemVerilog file *declares*, using pyslang's syntax tree.

This is the point of the `naming` check: pyslang knows whether a name is a module, a
port, a parameter, a signal or a memory; a regex over text only guesses. We parse a file
into a syntax tree (no elaboration, so a single file with missing packages still parses)
and walk it, yielding one `Decl` per declared identifier with its object kind and line.

Only *declarations* are yielded. A reference like `.clk_i(...)` (a connection to a
vendored module's port), `pkg::Member`, `$clog2` or `` `MACRO`` names something the file
does not own, so it is never classified here -- matching the reference checker's rule
that we only name what we declare.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# pyslang 11 ships stubs that do not parse under mypy (a parameter named `with`); mypy
# reads any stub it can reach. Importing through importlib keeps pyslang untyped (Any),
# the same technique as `chipgraph.adapters.tool.pyslang`.
_syntax: Any = importlib.import_module("pyslang.syntax")
SyntaxTree: Any = _syntax.SyntaxTree
SyntaxKind: Any = _syntax.SyntaxKind
SyntaxNode: Any = _syntax.SyntaxNode


@dataclass(frozen=True, slots=True)
class Decl:
    """One declared identifier: its object kind, its name, and its 1-based line."""

    kind: str
    name: str
    line: int


@dataclass(frozen=True, slots=True)
class ParseFailure:
    """A parse error, with a 1-based line when the parser could locate one."""

    line: int | None
    message: str


def parse_text(text: str, name: str = "source") -> Any:
    """Parse `text` into a pyslang syntax tree (syntax only, no elaboration)."""
    return SyntaxTree.fromText(text, name)


def parse_file(path: Path) -> Any:
    """Parse the file at `path` into a pyslang syntax tree (syntax only, no elaboration)."""
    return SyntaxTree.fromText(path.read_text(errors="ignore"), path.as_posix())


def parse_errors(tree: Any) -> list[ParseFailure]:
    """Real syntax errors of `tree`, as `ParseFailure`s (never raised).

    Only `Parser`- and `Lexer`-subsystem error diagnostics count: those are genuine
    malformed SystemVerilog. `Preprocessor` diagnostics (a `` `include`` we cannot open,
    an `` `UNKNOWN_MACRO`` directive defined in a package we were not given) are expected
    when a single file is parsed without its include paths -- the whole point of a
    syntax-only, single-file parse -- and are ignored, matching the reference checker,
    which does no preprocessing at all.
    """
    sm = tree.sourceManager
    out: list[ParseFailure] = []
    for diag in tree.diagnostics:
        if not diag.isError():
            continue
        subsystem = str(diag.code.getSubsystem()).rsplit(".", 1)[-1]
        if subsystem not in ("Parser", "Lexer"):
            continue
        loc = getattr(diag, "location", None)
        line = sm.getLineNumber(loc) if loc else None
        out.append(ParseFailure(line=line, message=str(diag.code)))
    return out


def declared_identifiers(tree: Any) -> list[Decl]:
    """Every identifier declared in `tree`, classified by object kind, sorted by line.

    The walk dispatches on syntax node kind. It handles: module/interface/program/package
    headers; ANSI and non-ANSI port declarations; parameter vs localparam; enum members;
    variable/net declarations (a declarator with an unpacked dimension is a `memory`, else
    a `signal`); genvars; and hierarchy instantiations.
    """
    sm = tree.sourceManager

    def line_of(token: Any) -> int:
        return int(sm.getLineNumber(token.location))

    out: list[Decl] = []

    def emit(kind: str, token: Any) -> None:
        name = token.valueText
        if name:
            out.append(Decl(kind=kind, name=name, line=line_of(token)))

    K = SyntaxKind
    # Only a `module` declaration is checked for the module-name rule. Interface, program
    # and package names have no rule in the source document and are not checked by the
    # reference, so we do not classify them here.
    header_kinds = {K.ModuleHeader}

    def visit(node: Any) -> None:
        kind = getattr(node, "kind", None)
        if kind is None:
            return

        if kind in header_kinds:
            _emit_header_name(node, "module", emit)
        elif kind == K.ImplicitAnsiPort or kind == K.ExplicitAnsiPort:
            _emit_declarator(node, "port", emit)
        elif kind == K.PortDeclaration:
            _emit_declarators(node, "port", emit)
        elif kind == K.ParameterDeclaration:
            _emit_parameter(node, emit)
        elif kind == K.EnumType:
            _emit_enum_members(node, emit)
        elif kind == K.DataDeclaration:
            _emit_signal_declarators(node, emit, is_net=False)
        elif kind == K.NetDeclaration:
            _emit_signal_declarators(node, emit, is_net=True)
        elif kind == K.GenvarDeclaration:
            _emit_genvars(node, emit)
        elif kind == K.HierarchicalInstance:
            _emit_instance(node, emit)

    _walk(tree.root, visit)
    out.sort(key=lambda d: (d.line, d.kind, d.name))
    return out


# --------------------------------------------------------------------------------------
# per-kind extraction helpers
# --------------------------------------------------------------------------------------


def _walk(node: Any, visit: Any) -> None:
    """Depth-first walk, calling `visit(node)` on every syntax node."""
    if node is None:
        return
    visit(node)
    if isinstance(node, SyntaxNode):
        for i in range(len(node)):
            try:
                child = node[i]
            except Exception:  # pragma: no cover - defensive against odd child slots
                child = None
            _walk(child, visit)


def _emit_header_name(node: Any, kind: str, emit: Any) -> None:
    name = getattr(node, "name", None)
    if name is not None and not name.isMissing:
        emit(kind, name)


def _iter_declarators(node: Any, attr: str) -> Iterator[Any]:
    """Yield the `Declarator` items of `node.<attr>`, skipping the separator commas.

    A pyslang separated list (`declarators`, `members`) iterates its comma tokens too;
    we keep only the `Declarator` nodes.
    """
    for item in getattr(node, attr, []):
        if getattr(item, "kind", None) == SyntaxKind.Declarator and not item.name.isMissing:
            yield item


def _emit_declarator(node: Any, kind: str, emit: Any) -> None:
    dec = getattr(node, "declarator", None)
    if dec is not None and not dec.name.isMissing:
        emit(kind, dec.name)


def _emit_declarators(node: Any, kind: str, emit: Any) -> None:
    for dec in _iter_declarators(node, "declarators"):
        emit(kind, dec.name)


def _emit_parameter(node: Any, emit: Any) -> None:
    keyword = getattr(node, "keyword", None)
    is_local = keyword is not None and str(keyword.kind).endswith("LocalParamKeyword")
    kind = "localparam" if is_local else "parameter"
    for dec in _iter_declarators(node, "declarators"):
        emit(kind, dec.name)


def _emit_enum_members(node: Any, emit: Any) -> None:
    for dec in _iter_declarators(node, "members"):
        emit("enum_value", dec.name)


_SIGNAL_TYPE_KINDS = frozenset({"LogicType", "RegType"})
"""DataDeclaration types the check treats as signals: `logic` and `reg`. A declaration of
a user-defined type (`my_struct_t x;`) or an integer type (`int i;`) is not a signal, so
it is left alone -- matching the reference, which only matches `reg`/`wire`/`logic`."""


def _emit_signal_declarators(node: Any, emit: Any, *, is_net: bool) -> None:
    if not is_net:
        # A DataDeclaration is only a signal when its type is `logic` or `reg`. Anything
        # else (a named type, `int`, a struct) is not what this rule governs.
        type_kind = str(getattr(getattr(node, "type", None), "kind", "")).rsplit(".", 1)[-1]
        if type_kind not in _SIGNAL_TYPE_KINDS:
            return
    for dec in _iter_declarators(node, "declarators"):
        # An unpacked dimension on the declarator (e.g. `logic [7:0] mem [0:15]`) makes
        # this a memory; a plain declarator is a signal.
        kind = "memory" if len(getattr(dec, "dimensions", [])) > 0 else "signal"
        emit(kind, dec.name)


def _emit_genvars(node: Any, emit: Any) -> None:
    for ident in getattr(node, "identifiers", []):
        if getattr(ident, "kind", None) != SyntaxKind.IdentifierName:
            continue
        token = getattr(ident, "identifier", None)
        if token is not None and not token.isMissing:
            emit("genvar", token)


def _emit_instance(node: Any, emit: Any) -> None:
    decl = getattr(node, "decl", None)
    if decl is None:
        return
    name = getattr(decl, "name", None)
    if name is not None and not name.isMissing:
        emit("instance", name)


__all__ = [
    "Decl",
    "ParseFailure",
    "declared_identifiers",
    "parse_errors",
    "parse_file",
    "parse_text",
]

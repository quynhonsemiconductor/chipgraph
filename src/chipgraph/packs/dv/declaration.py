"""A module's declaration, and nothing else, read from an RTL file with pyslang 12.

The testbench Author never sees the RTL (DESIGN.md 5.1, 5.3 F1). When a module's port
list is neither in its spec nor in the Design Model (a new module, a model not ingested
yet), the engine reads the RTL itself and keeps **only the module header**: the module
name, its parameter port list (name, kind, type, default expression) and its ports
(name, direction, net or variable type, packed and unpacked dimensions). For a non-ANSI
header (`module m(a, b); input [7:0] a; ...`) the body's port declarations are read too,
since they are the ports' declarations; every other body item is skipped unread.

It works on the syntax tree, never on the text: the `ModuleDeclaration`'s
`ModuleHeaderSyntax` (and, non-ANSI, its `PortDeclaration` members). Text is rebuilt
from tokens only, without their trivia, so a comment, a preprocessor directive or a
disabled `ifdef` branch never comes through, and no line number is kept. Macros used in
the header appear expanded. Output is capped (`MAX_PORTS`, `MAX_PARAMETERS`,
`MAX_TEXT`): a declaration cut short says so (`truncated`).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pyslang.parsing import Token, TokenKind
from pyslang.syntax import SyntaxKind, SyntaxNode, SyntaxTree

MAX_PORTS = 256
"""Ports kept per declaration; more set `truncated`."""
MAX_PARAMETERS = 64
"""Parameters kept per declaration; more set `truncated`."""
MAX_TEXT = 120
"""Characters kept of any one rebuilt text (a type, a dimension, a default)."""
MAX_FILE_BYTES = 4_000_000
"""RTL files larger than this are not parsed."""

Direction = Literal["input", "output", "inout", "ref"]

_DIRECTIONS: dict[str, Direction] = {
    "input": "input",
    "output": "output",
    "inout": "inout",
    "ref": "ref",
}


class DeclaredParameter(BaseModel):
    """One parameter of a module header."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    name: str = Field(description="The parameter name.")
    kind: Literal["parameter", "localparam", "type"] = Field(
        description="'parameter', 'localparam', or 'type' for a type parameter."
    )
    type: str = Field(default="", description="Its declared type ('' when implicit).")
    default: str | None = Field(default=None, description="Its default expression, as tokens.")


class DeclaredPort(BaseModel):
    """One port of a module declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    name: str = Field(description="The port name.")
    direction: Direction | None = Field(default=None, description="Its direction, if declared.")
    type: str = Field(default="", description="Net/variable type and signing, e.g. 'logic'.")
    packed: str = Field(default="", description="Packed dimensions, e.g. '[7:0]'.")
    unpacked: str = Field(default="", description="Unpacked dimensions, e.g. '[2]'.")
    width: int | None = Field(
        default=None, description="Bits, when every packed dimension is a literal range."
    )


class ModuleDeclaration(BaseModel):
    """A module's header: its name, parameters and ports; never anything of its body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    module: str = Field(description="The module name.")
    parameters: tuple[DeclaredParameter, ...] = Field(default=(), description="Its parameters.")
    ports: tuple[DeclaredPort, ...] = Field(default=(), description="Its ports, in order.")
    truncated: bool = Field(default=False, description="Whether the caps cut it short.")


# --- tokens to text ---------------------------------------------------------------------


def _tokens(node: Any) -> Iterator[Token]:
    if node is None:
        return
    if isinstance(node, Token):
        yield node
        return
    for child in node:
        yield from _tokens(child)


def _wordy(text: str) -> bool:
    return bool(text) and (text[0].isalnum() or text[0] in "_$`'\\")


def _text(node: Any) -> str:
    """The node's tokens joined without trivia: no comment or directive ever comes along."""
    parts: list[str] = []
    previous: Token | None = None
    for token in _tokens(node):
        raw = token.rawText
        if token.isMissing or not raw:
            continue
        if (
            previous is not None
            and _wordy(raw)
            and _wordy(previous.rawText[-1:])
            and previous.kind != TokenKind.IntegerBase
            and not raw.startswith("'")
        ):
            parts.append(" ")
        parts.append(raw)
        previous = token
    text = "".join(parts)
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _nodes(items: Any) -> list[Any]:
    """The syntax nodes of a separated list (its comma tokens dropped)."""
    if items is None:
        return []
    return [item for item in items if isinstance(item, SyntaxNode)]


# --- widths ------------------------------------------------------------------------------


def _literal(node: Any) -> int | None:
    tokens = [t for t in _tokens(node) if not t.isMissing]
    if len(tokens) == 1 and tokens[0].kind == TokenKind.IntegerLiteral:
        try:
            return int(tokens[0].rawText.replace("_", ""))
        except ValueError:
            return None
    return None


def _width(dimensions: Sequence[Any]) -> int | None:
    """The bits of packed `dimensions` when each is `[a:b]` with literal bounds; else None.

    No packed dimension is one bit.
    """
    width = 1
    for dim in dimensions:
        spec = getattr(dim, "specifier", None)
        selector = getattr(spec, "selector", None) if spec is not None else None
        if selector is None or selector.kind != SyntaxKind.SimpleRangeSelect:
            return None
        left, right = _literal(selector.left), _literal(selector.right)
        if left is None or right is None:
            return None
        width *= abs(left - right) + 1
    return width


# --- ports ------------------------------------------------------------------------------


def _direction(token: Any) -> Direction | None:
    if token is None or getattr(token, "isMissing", True):
        return None
    return _DIRECTIONS.get(str(token.rawText))


def _header_parts(header: Any) -> tuple[Direction | None, str, list[Any], bool]:
    """Direction, type text, packed dimensions of a port header, and whether its type is
    a built-in one (a named type, `pkg::t`, has a width only its definition knows)."""
    direction = _direction(getattr(header, "direction", None))
    data_type = getattr(header, "dataType", None)
    dims = _nodes(getattr(data_type, "dimensions", None)) if data_type is not None else []
    words: list[str] = []
    if header.kind == SyntaxKind.NetPortHeader:
        words.append(str(header.netType.rawText) if not header.netType.isMissing else "")
    elif header.kind == SyntaxKind.VariablePortHeader:
        var = header.varKeyword
        if var is not None and not var.isMissing and var.rawText:
            words.append(str(var.rawText))
    elif header.kind == SyntaxKind.InterfacePortHeader:
        words.append(_text(header))
        return direction, " ".join(w for w in words if w), [], False
    builtin = True
    if data_type is not None:
        keyword = getattr(data_type, "keyword", None)
        if keyword is not None and not keyword.isMissing and keyword.rawText:
            words.append(str(keyword.rawText))
        elif data_type.kind != SyntaxKind.ImplicitType:
            builtin = False
            name = getattr(data_type, "name", None)
            words.append(_text(name if name is not None else data_type))
        signing = getattr(data_type, "signing", None)
        if signing is not None and not signing.isMissing and signing.rawText:
            words.append(str(signing.rawText))
    return direction, " ".join(w for w in words if w), dims, builtin


def _port(header: Any, declarator: Any, inherited: DeclaredPort | None) -> DeclaredPort | None:
    name_token = getattr(declarator, "name", None)
    if name_token is None or name_token.isMissing or not name_token.rawText:
        return None
    if header is None and inherited is not None:  # `input [7:0] a, b`: b keeps a's header
        direction, type_text, packed, width = (
            inherited.direction,
            inherited.type,
            inherited.packed,
            inherited.width,
        )
    else:
        direction, type_text, dims, builtin = (
            _header_parts(header) if header is not None else (None, "", [], True)
        )
        packed = "".join(_text(d) for d in dims)
        width = _width(dims) if builtin else None
    unpacked = "".join(_text(d) for d in _nodes(getattr(declarator, "dimensions", None)))
    return DeclaredPort(
        name=str(name_token.rawText),
        direction=direction,
        type=type_text[:MAX_TEXT],
        packed=packed[:MAX_TEXT],
        unpacked=unpacked[:MAX_TEXT],
        width=width,
    )


def _ansi_ports(port_list: Any) -> Iterator[DeclaredPort]:
    previous: DeclaredPort | None = None
    for item in _nodes(port_list.ports):
        if item.kind == SyntaxKind.ImplicitAnsiPort:
            header = item.header
            implicit = (
                header.kind == SyntaxKind.VariablePortHeader
                and _direction(header.direction) is None
                and header.dataType.kind == SyntaxKind.ImplicitType
                and not _nodes(header.dataType.dimensions)
            )
            port = _port(None if implicit else header, item.declarator, previous)
        elif item.kind == SyntaxKind.ExplicitAnsiPort:  # `.name(expr)`: the name only
            name = item.name
            port = (
                DeclaredPort(name=str(name.rawText), direction=_direction(item.direction))
                if name is not None and not name.isMissing
                else None
            )
        else:
            port = None
        if port is not None:
            previous = port
            yield port


def _non_ansi_ports(port_list: Any, members: Iterable[Any]) -> Iterator[DeclaredPort]:
    """A non-ANSI header lists names; the body's `PortDeclaration`s declare them."""
    declared: dict[str, DeclaredPort] = {}
    for member in members:
        if member.kind != SyntaxKind.PortDeclaration:
            continue  # any other body item is never looked at
        for declarator in _nodes(member.declarators):
            port = _port(member.header, declarator, None)
            if port is not None:
                declared.setdefault(port.name, port)
    for item in _nodes(port_list.ports):
        if item.kind == SyntaxKind.ImplicitNonAnsiPort:
            expr = item.expr
            names = [t for t in _tokens(expr) if t.kind == TokenKind.Identifier]
            if len(names) == 1:
                name = str(names[0].rawText)
                yield declared.get(name, DeclaredPort(name=name))
        elif item.kind == SyntaxKind.ExplicitNonAnsiPort:
            name = item.name
            if name is not None and not name.isMissing:
                yield DeclaredPort(name=str(name.rawText))


# --- parameters --------------------------------------------------------------------------


def _parameters(param_list: Any) -> Iterator[DeclaredParameter]:
    if param_list is None:
        return
    for decl in _nodes(param_list.declarations):
        keyword = getattr(decl, "keyword", None)
        local = keyword is not None and keyword.kind == TokenKind.LocalParamKeyword
        if decl.kind == SyntaxKind.TypeParameterDeclaration:
            for item in _nodes(decl.declarators):
                init = getattr(item, "assignment", None)
                yield DeclaredParameter(
                    name=str(item.name.rawText),
                    kind="type",
                    default=_text(getattr(init, "type", None)) if init is not None else None,
                )
        elif decl.kind == SyntaxKind.ParameterDeclaration:
            type_text = _text(decl.type)
            for item in _nodes(decl.declarators):
                init = getattr(item, "initializer", None)
                yield DeclaredParameter(
                    name=str(item.name.rawText),
                    kind="localparam" if local else "parameter",
                    type=type_text,
                    default=_text(init.expr) if init is not None else None,
                )


# --- the declaration -----------------------------------------------------------------------


def _modules(root: Any) -> Iterator[Any]:
    """Every top-level module declaration of a tree (one whose root is a module, too)."""
    if root.kind == SyntaxKind.ModuleDeclaration:
        yield root
        return
    for member in getattr(root, "members", None) or ():
        if isinstance(member, SyntaxNode) and member.kind == SyntaxKind.ModuleDeclaration:
            yield member


def _declaration(node: Any) -> ModuleDeclaration:
    header = node.header
    params = list(_parameters(header.parameters))
    ports: list[DeclaredPort] = []
    if header.ports is not None and header.ports.kind == SyntaxKind.AnsiPortList:
        ports = list(_ansi_ports(header.ports))
    elif header.ports is not None and header.ports.kind == SyntaxKind.NonAnsiPortList:
        ports = list(_non_ansi_ports(header.ports, node.members))
    truncated = len(ports) > MAX_PORTS or len(params) > MAX_PARAMETERS
    return ModuleDeclaration(
        module=str(header.name.rawText),
        parameters=tuple(params[:MAX_PARAMETERS]),
        ports=tuple(ports[:MAX_PORTS]),
        truncated=truncated,
    )


def declarations_in_text(text: str, *, name: str = "source") -> tuple[ModuleDeclaration, ...]:
    """Every module declaration in an RTL source text (headers only)."""
    tree = SyntaxTree.fromText(text, name)
    return tuple(_declaration(node) for node in _modules(tree.root))


def declaration_in_text(
    text: str, module: str, *, name: str = "source"
) -> ModuleDeclaration | None:
    """The declaration of `module` in an RTL source text, or None if it is not there."""
    for decl in declarations_in_text(text, name=name):
        if decl.module == module:
            return decl
    return None


def extract_declaration(files: Iterable[Path], module: str | None) -> ModuleDeclaration | None:
    """The declaration of `module` from the first of `files` that declares it.

    With `module` None, the only module the files declare (None when they declare none
    or several). Unreadable, missing or oversized files are skipped.
    """
    found: list[ModuleDeclaration] = []
    for path in files:
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for decl in declarations_in_text(text, name=path.name):
            if module is not None and decl.module == module:
                return decl
            found.append(decl)
    if module is None and len({d.module for d in found}) == 1:
        return found[0]
    return None


__all__ = [
    "MAX_PARAMETERS",
    "MAX_PORTS",
    "MAX_TEXT",
    "DeclaredParameter",
    "DeclaredPort",
    "ModuleDeclaration",
    "declaration_in_text",
    "declarations_in_text",
    "extract_declaration",
]

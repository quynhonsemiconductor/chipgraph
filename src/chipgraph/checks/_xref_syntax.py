"""Syntax-tree helpers shared by the `connect` and `hardcode` cross checks.

Both checks look at RTL *structure*, not the elaborated model: `connect` needs the
hierarchy instances of the top module and their named port connections; `hardcode` needs
every integer literal a file types, with its decoded value and line, and the comment on
that line (so a `// contract:` tag can exempt it). Neither elaborates: a single file with
missing packages still parses (matching `naming`'s single-file, syntax-only parse).

Everything here is pure and deterministic and never raises on malformed input: a parse
error is returned as a `ParseFailure` (via `chipgraph.checks._naming_pyslang.parse_errors`),
never thrown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyslang.syntax import SyntaxKind, SyntaxNode, SyntaxTree

from chipgraph.checks._naming_pyslang import ParseFailure, parse_errors, parse_file


@dataclass(frozen=True, slots=True)
class PortConn:
    """One named port connection `.port(expr)` of an instance."""

    port: str
    expr: str
    line: int


@dataclass(frozen=True, slots=True)
class Instance:
    """One hierarchy instance in a module: `module_name inst_name (.p(e), ...)`."""

    module: str
    name: str
    line: int
    connections: tuple[PortConn, ...] = ()

    def connection(self, port: str) -> PortConn | None:
        """The named connection to `port`, or None if it is not connected by name."""
        for conn in self.connections:
            if conn.port == port:
                return conn
        return None


@dataclass(frozen=True, slots=True)
class IntLiteral:
    """One integer literal token in RTL: its decoded value, line and raw text."""

    value: int
    line: int
    raw: str


@dataclass(slots=True)
class ParsedRtl:
    """The syntax facts one RTL file yields, plus any parse errors."""

    instances: list[Instance] = field(default_factory=list)
    literals: list[IntLiteral] = field(default_factory=list)
    parse_errors: tuple[ParseFailure, ...] = ()
    line_comments: dict[int, str] = field(default_factory=dict)


def parse_rtl(path: Path) -> ParsedRtl:
    """Parse the RTL at `path`, returning its instances, literals and parse errors."""
    tree = parse_file(path)
    text = path.read_text(errors="ignore")
    return ParsedRtl(
        instances=_instances(tree),
        literals=_literals(tree),
        parse_errors=tuple(parse_errors(tree)),
        line_comments=_line_comments(text),
    )


def parse_rtl_text(text: str, name: str = "source") -> ParsedRtl:
    """Parse RTL `text` (for tests), returning its instances, literals and parse errors."""
    tree = SyntaxTree.fromText(text, name)
    return ParsedRtl(
        instances=_instances(tree),
        literals=_literals(tree),
        parse_errors=tuple(parse_errors(tree)),
        line_comments=_line_comments(text),
    )


# --------------------------------------------------------------------------------------
# instances and their named port connections
# --------------------------------------------------------------------------------------


def _instances(tree: Any) -> list[Instance]:
    sm = tree.sourceManager

    def line_of(token: Any) -> int:
        return int(sm.getLineNumber(token.location))

    out: list[Instance] = []
    for hi in _find(tree.root, SyntaxKind.HierarchyInstantiation):
        module = _text(getattr(hi, "type", None))
        if not module:
            continue
        for inst in _find(hi, SyntaxKind.HierarchicalInstance):
            decl = getattr(inst, "decl", None)
            name = _text(getattr(decl, "name", None)) if decl is not None else ""
            conns: list[PortConn] = []
            for npc in _find(inst, SyntaxKind.NamedPortConnection):
                port = _text(getattr(npc, "name", None))
                if not port:
                    continue
                expr = _text(getattr(npc, "expr", None))
                conns.append(PortConn(port=port, expr=expr, line=line_of(npc.getFirstToken())))
            out.append(
                Instance(
                    module=module,
                    name=name,
                    line=line_of(inst.getFirstToken()),
                    connections=tuple(conns),
                )
            )
    return out


# --------------------------------------------------------------------------------------
# integer literals (tokens, so comments and strings are never scanned)
# --------------------------------------------------------------------------------------

_RADIX = {"b": 2, "o": 8, "d": 10, "h": 16}


def _literals(tree: Any) -> list[IntLiteral]:
    """Every integer literal in `tree`, decoded, in source order.

    Works on the token stream, so numbers inside comments or strings are never seen. A
    sized literal (`32'h8000_4000`) is three tokens -- the size `IntegerLiteral`, the
    `IntegerBase` (`'h`) and the digits `IntegerLiteral` -- so the size token is skipped
    and the digits are decoded with the base's radix. A bare `IntegerLiteral` with no
    base before or after it is a plain decimal.
    """
    sm = tree.sourceManager
    tokens = _tokens(tree.root)
    out: list[IntLiteral] = []
    for i, tok in enumerate(tokens):
        kind = str(getattr(tok, "kind", ""))
        if not kind.endswith("IntegerLiteral"):
            continue
        prev = str(getattr(tokens[i - 1], "kind", "")) if i > 0 else ""
        nxt = str(getattr(tokens[i + 1], "kind", "")) if i + 1 < len(tokens) else ""
        raw = str(getattr(tok, "rawText", "") or "")
        line = int(sm.getLineNumber(tok.location))
        if prev.endswith("IntegerBase"):
            base = str(getattr(tokens[i - 1], "rawText", "") or "").lstrip("'").lower()
            value = _decode(raw, _RADIX.get(base[:1], 10))
        elif nxt.endswith("IntegerBase"):
            # A size prefix (`32` in `32'h...`), not a value of its own.
            continue
        else:
            value = _decode(raw, 10)
        if value is not None:
            out.append(IntLiteral(value=value, line=line, raw=raw))
    return out


def _decode(digits: str, radix: int) -> int | None:
    """Decode `digits` (with `_` separators) in `radix`; None if it is not a plain number.

    A literal with `x`/`z`/`?` bits (`8'bxxxx_xxxx`) has no single value, so it is
    dropped rather than guessed at.
    """
    cleaned = digits.replace("_", "")
    if not cleaned:
        return None
    try:
        return int(cleaned, radix)
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# shared token/node walking
# --------------------------------------------------------------------------------------


def _find(node: Any, kind: SyntaxKind) -> list[Any]:
    out: list[Any] = []

    def walk(n: Any) -> None:
        if n is None:
            return
        if getattr(n, "kind", None) == kind:
            out.append(n)
        if isinstance(n, SyntaxNode):
            for i in range(len(n)):
                try:
                    child = n[i]
                except Exception:  # pragma: no cover - defensive against odd child slots
                    child = None
                walk(child)

    walk(node)
    return out


def _tokens(node: Any) -> list[Any]:
    out: list[Any] = []

    def walk(n: Any) -> None:
        if n is None:
            return
        if isinstance(n, SyntaxNode):
            for i in range(len(n)):
                try:
                    child = n[i]
                except Exception:  # pragma: no cover - defensive against odd child slots
                    child = None
                walk(child)
        else:
            out.append(n)

    walk(node)
    return out


def _text(node: Any) -> str:
    """The trimmed source text of `node` (or a token), or '' when it is None/missing."""
    if node is None:
        return ""
    if getattr(node, "isMissing", False):
        return ""
    return str(node).strip()


def _line_comments(text: str) -> dict[int, str]:
    """Map each 1-based line number to its `//` comment text (without the `//`), if any.

    A best-effort text scan used only to read a `// contract:`-style exemption marker;
    the literals themselves come from the token stream, never from this.
    """
    out: dict[int, str] = {}
    for i, line in enumerate(text.splitlines(), start=1):
        idx = line.find("//")
        if idx >= 0:
            out[i] = line[idx + 2 :]
    return out


__all__ = [
    "Instance",
    "IntLiteral",
    "ParsedRtl",
    "PortConn",
    "parse_rtl",
    "parse_rtl_text",
]

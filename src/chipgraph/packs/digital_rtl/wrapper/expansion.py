"""The expanded wrapper read back, and checked against the IP's port declaration.

verilog-mode writes an instance's connections one per line after `/*AUTOINST*/`, each
`.<ip port> (<signal>),` and, when an `AUTO_TEMPLATE` line made it, `// Templated`. A
port no template line matches is connected to a signal of its own name, and the
`AUTOINPUT`/`AUTOOUTPUT` groups then declare that signal as a wrapper port: the IP port
is left unmapped. `instances` reads the instances of a module from the text (comments and
strings masked, so a template's text in a comment is never taken for code); `validate`
checks each one against the IP module's declaration (`ModuleDeclaration`, header only):

- `wrap.no_instance`: the wrapper does not instantiate the IP module;
- `wrap.autoinst_empty`: an `/*AUTOINST*/` that expanded to no connection;
- `wrap.unknown_port`: a connection to a port the IP does not declare;
- `wrap.port_missing`: an IP port the instance does not connect;
- `wrap.untemplated`: an IP port no template line maps (see above);
- `wrap.input_open`: an IP input left open, `()`; an open output is a note
  (`wrap.output_open`): Verilator's lint warns about it (`PINCONNECTEMPTY`).

Every issue carries the expanded connection's text: what the author, who cannot run
emacs, needs to fix the template.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from chipgraph.core.contracts import Issue
from chipgraph.core.contracts.types import Severity
from chipgraph.packs.dv.declaration import ModuleDeclaration

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_MODULE_RE = re.compile(r"\bmodule\s+(?:automatic\s+|static\s+)?([A-Za-z_][A-Za-z0-9_$]*)")
_CONNECTION_RE = re.compile(r"\.\s*([A-Za-z_][A-Za-z0-9_$]*)")
_AUTOINST_RE = re.compile(r"/\*\s*AUTOINST\b")
_TEMPLATED = "Templated"


def mask(text: str) -> str:
    """`text` with comments and string literals blanked (newlines kept, same length)."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        two = text[i : i + 2]
        if two == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
        elif two == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif text[i] == '"':
            j = i + 1
            while j < n and text[j] not in '"\n':
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
        else:
            i += 1
            continue
        for k in range(i, j):
            if out[k] != "\n":
                out[k] = " "
        i = j
    return "".join(out)


def _close(code: str, open_at: int) -> int:
    """The index of the `)` that closes the `(` at `open_at` in masked `code`, or -1."""
    depth = 0
    for i in range(open_at, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _skip_ws(code: str, i: int) -> int:
    while i < len(code) and code[i].isspace():
        i += 1
    return i


def line_of(text: str, offset: int) -> int:
    """The 1-based line of `offset` in `text`."""
    return text.count("\n", 0, offset) + 1


@dataclass(frozen=True)
class Connection:
    """One named port connection of an instance."""

    port: str
    expr: str
    """The connected expression, as written (comments removed); '' when open."""
    line: int
    text: str
    """The connection's line, stripped: what the author is shown."""
    templated: bool
    manual: bool
    """Written before `/*AUTOINST*/` in the source, i.e. by hand."""


@dataclass(frozen=True)
class Instance:
    """One instance of a module in a wrapper."""

    module: str
    name: str
    line: int
    autoinst: bool
    connections: tuple[Connection, ...]


def module_name(text: str) -> str | None:
    """The name of the first module the text declares."""
    match = _MODULE_RE.search(mask(text))
    return match.group(1) if match else None


def instances(text: str, module: str) -> list[Instance]:
    """Every instance of `module` in `text` (comments and strings never count)."""
    code = mask(text)
    found: list[Instance] = []
    for match in re.finditer(rf"(?<![\w$`.]){re.escape(module)}(?![\w$])", code):
        i = _skip_ws(code, match.end())
        if i < len(code) and code[i] == "#":
            i = _skip_ws(code, i + 1)
            if i >= len(code) or code[i] != "(":
                continue
            end = _close(code, i)
            if end < 0:
                continue
            i = _skip_ws(code, end + 1)
        ident = _IDENT.match(code, i)
        if ident is None or ident.group(0) in ("module", "endmodule"):
            continue
        name = ident.group(0)
        i = _skip_ws(code, ident.end())
        while i < len(code) and code[i] == "[":
            close = code.find("]", i)
            if close < 0:
                break
            i = _skip_ws(code, close + 1)
        if i >= len(code) or code[i] != "(":
            continue
        end = _close(code, i)
        if end < 0:
            continue
        region = text[i + 1 : end]
        auto = _AUTOINST_RE.search(region)
        auto_at = i + 1 + auto.start() if auto else -1
        found.append(
            Instance(
                module=module,
                name=name,
                line=line_of(text, match.start()),
                autoinst=auto is not None,
                connections=tuple(_connections(text, code, i + 1, end, auto_at)),
            )
        )
    return found


def _connections(text: str, code: str, start: int, end: int, auto_at: int) -> list[Connection]:
    out: list[Connection] = []
    i = start
    while True:
        match = _CONNECTION_RE.search(code, i, end)
        if match is None:
            return out
        port = match.group(1)
        j = _skip_ws(code, match.end())
        if j < end and code[j] == "(":
            close = _close(code, j)
            close = end if close < 0 or close > end else close
            expr = " ".join(code[j + 1 : close].split())
            after = close + 1
        else:  # `.name` (implicit) connects the signal of that name
            expr, after = port, match.end()
        line_end = text.find("\n", after)
        line_end = len(text) if line_end < 0 else line_end
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_text = text[line_start:line_end]
        out.append(
            Connection(
                port=port,
                expr=expr,
                line=line_of(text, match.start()),
                text=" ".join(line_text.split()),
                templated=_TEMPLATED in text[after:line_end],
                manual=auto_at >= 0 and match.start() < auto_at,
            )
        )
        i = after


def validate(
    text: str,
    declaration: ModuleDeclaration,
    *,
    file: str,
    line_for: Callable[[Instance], int | None] | None = None,
    wrapper: str | None = None,
) -> list[Issue]:
    """The problems of the expanded `text`'s instances of the IP (see the module).

    Issues point at `file`, at the line `line_for(instance)` gives (default: the
    instance's line in `text`).
    """
    issues: list[Issue] = []
    vendor = declaration.module
    ports = {p.name: p for p in declaration.ports}
    found = instances(text, vendor)

    def issue(rule: str, msg: str, inst: Instance | None, severity: Severity = "error") -> None:
        line = (line_for(inst) if line_for is not None else inst.line) if inst else None
        issues.append(Issue(file=file, line=line, rule=rule, severity=severity, msg=msg))

    declared = module_name(text)
    if wrapper is not None and declared != wrapper:
        issue(
            "wrap.module_name",
            f"the source declares module `{declared}`, not the wrapper `{wrapper}`",
            None,
        )
    if not found:
        issue(
            "wrap.no_instance",
            f"the wrapper does not instantiate the IP module `{vendor}`: write "
            f"`{vendor} u_<name> (/*AUTOINST*/);`",
            None,
        )
    for inst in found:
        where = f"instance `{inst.name}` of `{vendor}`"
        if inst.autoinst and not inst.connections:
            issue(
                "wrap.autoinst_empty",
                f"/*AUTOINST*/ of {where} expanded to no port: emacs found no port of "
                f"`{vendor}` (is the module name right, and its file in vendor_files?)",
                inst,
            )
            continue
        connected: set[str] = set()
        for conn in inst.connections:
            connected.add(conn.port)
            port = ports.get(conn.port)
            if port is None:
                issue(
                    "wrap.unknown_port",
                    f"{where} connects `{conn.port}`, which `{vendor}` does not declare: "
                    f"`{conn.text}`",
                    inst,
                )
                continue
            direction = port.direction or "port"
            if not conn.expr:
                if direction == "input":
                    issue(
                        "wrap.input_open",
                        f"{where} leaves the input `{conn.port}` unconnected: `{conn.text}`; "
                        "tie it (1'b0, '0) or map it in the AUTO_TEMPLATE",
                        inst,
                    )
                else:
                    issue(
                        "wrap.output_open",
                        f"{where} leaves the {direction} `{conn.port}` open: `{conn.text}`; "
                        "lint warns about an empty connection, sink it in an `unused_*` "
                        "signal instead",
                        inst,
                        "info",
                    )
                continue
            if inst.autoinst and not conn.manual and not conn.templated:
                issue(
                    "wrap.untemplated",
                    f"{where}: the {direction} `{conn.port}` is not mapped by the AUTO_TEMPLATE, "
                    f"so it is left unconnected to any project signal: the expansion wires it "
                    f"to a new wrapper port of the IP's own name: `{conn.text}`. Add a template "
                    f"line for `.{conn.port}` (or fix the regexp that should match it)",
                    inst,
                )
        for name in ports:
            if name not in connected:
                issue(
                    "wrap.port_missing",
                    f"{where} does not connect the {ports[name].direction or 'port'} `{name}`",
                    inst,
                )
    return issues


__all__ = [
    "Connection",
    "Instance",
    "instances",
    "line_of",
    "mask",
    "module_name",
    "validate",
]

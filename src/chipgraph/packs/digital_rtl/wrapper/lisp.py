"""The Emacs Lisp a wrapper source may carry, and nothing more.

verilog-mode evaluates Lisp written in the source: `/*AUTO_LISP(...)*/`, a template's
`@"(...)"` function, `/*AUTOINSERTLISP(...)*/`. The source is written by an agent that
has no shell, so emacs must not become one. Before the generator runs emacs it reads
every such form and refuses the source unless the form uses only:

- in `AUTO_LISP`: `(setq <var> <value> ...)` where `<var>` is one of verilog-mode's
  ignore regexps (`verilog-auto-input-ignore-regexp`, ...: `SETQ_VARIABLE`);
- everywhere: strings, numbers, characters, quoted data, `nil`/`t`, the template's
  `vl-*` variables and backreferences (`\\1`), and calls of the side-effect-free string
  and arithmetic functions in `FUNCTIONS` (none takes a function argument).

`AUTOINSERTLISP` and `AUTOINSERTLAST` are refused, and so is any reader syntax beyond
quote (backquote, `#'`, `#` forms). File-local variables are the other way to run
Lisp: the generator makes them inert in the copy it hands to emacs (`emacs.neutralize`),
so a `Local Variables:` block (and its `eval:`) or a `-*-` mode line is ignored, and
`scan` reports it as a note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FUNCTIONS = frozenset(
    {
        "concat",
        "downcase",
        "upcase",
        "capitalize",
        "substring",
        "string-to-number",
        "number-to-string",
        "format",
        "if",
        "when",
        "unless",
        "and",
        "or",
        "not",
        "null",
        "equal",
        "eq",
        "string=",
        "string-equal",
        "string-match",
        "string-match-p",
        "match-string",
        "string-prefix-p",
        "string-suffix-p",
        "regexp-quote",
        "symbol-name",
        "length",
        "progn",
        "+",
        "-",
        "*",
        "/",
        "%",
        "1+",
        "1-",
        "max",
        "min",
        "abs",
    }
)
"""Functions a form may call: pure string and arithmetic functions, and `if`-like forms."""

VARIABLES = frozenset(
    {
        "nil",
        "t",
        "vl-name",
        "vl-width",
        "vl-bits",
        "vl-mbits",
        "vl-dir",
        "vl-cell-type",
        "vl-cell-name",
        "vl-memory",
        "vl-modport",
    }
)
"""Variables a form may read: verilog-mode's template variables."""

SETQ_VARIABLE = re.compile(
    r"^(?:verilog-auto-[a-z]+-ignore-regexp|verilog-typedef-regexp|verilog-active-low-regexp)$"
)
"""Variables an `AUTO_LISP` form may set: verilog-mode's name filters."""

_REFUSED_AUTOS = re.compile(r"/\*\s*(AUTOINSERTLISP|AUTOINSERTLAST)\b")
_AUTO_LISP = re.compile(r"/\*\s*AUTO_LISP\s*(?=\()")
_TEMPLATE_LISP = re.compile(r'@"')
_LOCAL_VARIABLES = re.compile(r"Local\s+Variables\s*:|-\*-", re.IGNORECASE)
_BACKREF = re.compile(r"^\\[0-9]$")
_ATOM_END = set(" \t\r\n()'\";")


@dataclass(frozen=True)
class LispProblem:
    """A form the generator refuses to hand to emacs, or a note about the source."""

    line: int
    message: str
    note: bool = False
    """True for a note (the source is not refused)."""


class _ParseError(Exception):
    pass


@dataclass(frozen=True)
class _Sym:
    name: str


@dataclass(frozen=True)
class _Quoted:
    datum: object


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _read_string(text: str, start: int) -> tuple[str, int]:
    """The string literal opening at `text[start] == '"'`; (its raw body, end index)."""
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return text[start + 1 : i], i + 1
        i += 1
    raise _ParseError("a string is not closed")


def _read(text: str, i: int) -> tuple[object, int]:
    """One datum of `text` from `i`: a list, `_Sym`, `_Quoted`, str or number."""
    n = len(text)
    while i < n:
        if text[i] in " \t\r\n":
            i += 1
        elif text[i] == ";":
            while i < n and text[i] != "\n":
                i += 1
        else:
            break
    if i >= n:
        raise _ParseError("the form ends early")
    char = text[i]
    if char == "(":
        items: list[object] = []
        i += 1
        while True:
            while i < n and text[i] in " \t\r\n":
                i += 1
            if i < n and text[i] == ";":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if i >= n:
                raise _ParseError("a list is not closed")
            if text[i] == ")":
                return items, i + 1
            item, i = _read(text, i)
            items.append(item)
    if char == ")":
        raise _ParseError("an unmatched ')'")
    if char == "'":
        datum, i = _read(text, i + 1)
        return _Quoted(datum), i
    if char in "`,#":
        raise _ParseError(f"the reader syntax {char!r} (only quote is allowed)")
    if char == '"':
        body, end = _read_string(text, i)
        return body, end
    if char == "?":
        j = i + 1
        if j < n and text[j] == "\\":
            j += 1
        return 0, min(j + 1, n)
    j = i
    while j < n and text[j] not in _ATOM_END:
        j += 2 if text[j] == "\\" else 1
    atom = text[i:j]
    try:
        return float(atom), j
    except ValueError:
        return _Sym(atom), j


def _check(datum: object, *, setq: bool) -> str | None:
    """Why `datum` may not be evaluated, or None when it may."""
    if isinstance(datum, (str, int, float, _Quoted)):
        return None
    if isinstance(datum, _Sym):
        name = datum.name
        if name in VARIABLES or name.startswith(":") or _BACKREF.match(name):
            return None
        return f"reads the variable `{name}`, which is not allowed"
    if isinstance(datum, list):
        if not datum:
            return None
        head = datum[0]
        if not isinstance(head, _Sym):
            return "calls something that is not a function name"
        if head.name == "quote":
            return None
        if head.name == "setq":
            if not setq:
                return "sets a variable (`setq` is allowed in AUTO_LISP only)"
            pairs = datum[1:]
            if not pairs or len(pairs) % 2:
                return "has a `setq` without a value"
            for var, value in zip(pairs[::2], pairs[1::2], strict=True):
                if not isinstance(var, _Sym) or not SETQ_VARIABLE.match(var.name):
                    shown = var.name if isinstance(var, _Sym) else "a non-symbol"
                    return (
                        f"sets `{shown}`; AUTO_LISP may set only verilog-mode's name "
                        "filters (verilog-auto-<kind>-ignore-regexp, verilog-typedef-regexp)"
                    )
                why = _check(value, setq=False)
                if why is not None:
                    return why
            return None
        if head.name not in FUNCTIONS:
            return f"calls `{head.name}`, which is not allowed"
        for arg in datum[1:]:
            why = _check(arg, setq=False)
            if why is not None:
                return why
        return None
    return "holds something that is not a plain value"


def _unescape(body: str) -> str:
    """The Lisp text a template's `@"..."` string holds (its `\\"` and `\\\\` undone)."""
    return re.sub(r'\\(["\\])', r"\1", body)


def _forms(code: str) -> list[object]:
    out: list[object] = []
    i = 0
    while True:
        while i < len(code) and code[i] in " \t\r\n":
            i += 1
        if i >= len(code):
            return out
        datum, i = _read(code, i)
        out.append(datum)


def scan(text: str) -> list[LispProblem]:
    """Every Lisp form of a wrapper source that may not run, and the notes (see above)."""
    problems: list[LispProblem] = []
    for match in _REFUSED_AUTOS.finditer(text):
        problems.append(
            LispProblem(
                _line(text, match.start()),
                f"{match.group(1)} inserts the result of Lisp code: not allowed in a wrapper "
                "source; use AUTO_TEMPLATE lines instead",
            )
        )
    for match in _AUTO_LISP.finditer(text):
        line = _line(text, match.start())
        try:
            datum, _ = _read(text, match.end())
        except _ParseError as exc:
            problems.append(LispProblem(line, f"AUTO_LISP cannot be read: {exc}"))
            continue
        why = _check(datum, setq=True)
        if why is not None:
            problems.append(LispProblem(line, f"AUTO_LISP {why}"))
    for match in _TEMPLATE_LISP.finditer(text):
        line = _line(text, match.start())
        try:
            body, _ = _read_string(text, match.start() + 1)
            forms = _forms(_unescape(body))
        except _ParseError as exc:
            problems.append(LispProblem(line, f'a template function @"..." cannot be read: {exc}'))
            continue
        for datum in forms:
            why = _check(datum, setq=False)
            if why is not None:
                problems.append(LispProblem(line, f'a template function @"..." {why}'))
    for match in _LOCAL_VARIABLES.finditer(text):
        problems.append(
            LispProblem(
                _line(text, match.start()),
                "file-local variables (`Local Variables:`, `-*-`) are ignored: the generator "
                "pins verilog-mode's settings and reads the IP from the profile's vendor_files",
                note=True,
            )
        )
    return sorted(problems, key=lambda p: (p.line, p.message))


__all__ = ["FUNCTIONS", "SETQ_VARIABLE", "VARIABLES", "LispProblem", "scan"]

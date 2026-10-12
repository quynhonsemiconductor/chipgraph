"""The Emacs Lisp a wrapper source may carry, and nothing more.

verilog-mode evaluates Lisp written in the source: `AUTO_LISP(...)`, a template's
`@"(...)"` function, `AUTOINSERTLISP(...)`. The source is written by an agent, so this
module treats it as hostile: emacs must not become a shell. Before the generator runs
emacs, `scan` reads the **raw text** (no comment or string is skipped: verilog-mode does
not skip them either) and refuses the source unless every piece of Lisp in it is proved
harmless. Anything the scanner cannot prove harmless is refused; an error in the scanner
is a refusal too.

Where emacs finds Lisp, and what the scanner accepts:

- `AUTO_LISP(`: verilog-mode finds it **anywhere** in the buffer (a `//` comment, a string,
  the middle of a block comment), in any letter case, and hands the region it spans to
  `eval-region`, which evaluates every form in it. The scanner looks for
  `auto_lisp\\s*\\(` in any case, on a skeleton of the text where every non-ASCII letter
  that case-folds to an ASCII one (long s U+017F, Kelvin sign U+212A, dotless i U+0131,
  fullwidth forms) is that ASCII letter, and accepts only the exact form `/*AUTO_LISP(`
  ... `)*/`, whose text from `(` to the first `*/` is exactly one form,
  `(setq <var> <value> ...)`, where `<var>` is one of verilog-mode's name filters (`SETQ_VARIABLE`).
- `AUTOINSERTLISP` and `AUTOINSERTLAST`, in any case, anywhere: refused.
- A template function `@"..."`: the scanner finds it with verilog-mode's own regexp and
  applies its transform (`\\"` becomes `"`) before reading. The Lisp must be one form, may
  not contain `@` (verilog-mode replaces it with the template number) nor a backslash other
  than a quoted back-reference (`'\\1`, which pastes a name the template's regexp matched),
  and `@@"` is refused: verilog-mode splices a function's result back into the template and
  searches it again from the start, so a result that holds `@"` would run as Lisp in the
  next round. No allowed function can build `@` or `"` from literal data: `format` is not
  allowed, nor is quoted data other than a symbol.

The forms themselves may use only strings, integers, quoted symbols, `nil`/`t`, the
template's `vl-*` variables and calls of the side-effect-free string and arithmetic
functions in `FUNCTIONS` (none takes a function argument; `string-match` only with a
literal regexp without nested repetition). The text of a form is restricted so that the
Elisp reader and verilog-mode's Verilog syntax table see the same form: outside strings only
letters, digits, whitespace, `(`, `)`, `'`, `"` and the symbol characters `-_+*/%<>=:!`
(no `;`, `?`, `\\`, `[`, `]`, `{`, `}`, `#`, `` ` ``, `,`, `.`, `@`, `|`, nor `//`, `/*`, `*/`);
inside strings only printable ASCII with the escape `\\\\` (in `AUTO_LISP`; template strings
have none). Integers are plain decimal; a list nests at most `MAX_DEPTH` deep.

File-local variables are the other way to run Lisp: the generator makes them inert in the
copies it hands to emacs (`emacs.neutralize`), so a `Local Variables:` block (and its
`eval:`) or a `-*-` mode line is ignored, and `scan` reports it as a note.

What the scanner cannot see: a template function that reads a `vl-*` variable returns text
verilog-mode took from the IP's or the source's declarations (port names, widths with their
parameter values). The generator therefore also runs emacs with a guard that refuses, inside
emacs, any form outside the same allow-list (`emacs.GUARD`).
"""

from __future__ import annotations

import functools
import re
import unicodedata
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
"""Functions a form may call: pure string and arithmetic functions, and `if`-like forms.

`format` is not one of them: with `%c` it builds any character from numbers, so a template
function's result could hold a new `@"..."` that verilog-mode would evaluate next."""

REGEXP_FUNCTIONS = frozenset({"string-match", "string-match-p"})
"""Functions whose first argument is a regexp: it must be a literal without nested repetition."""

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

MAX_DEPTH = 32
"""How deep a form's lists may nest."""

ACCEPTED_AUTO_LISP = "/*AUTO_LISP("
"""The one way a source may write `AUTO_LISP`: this, then the form's rest, then `*/`."""

# On the folded skeleton (see `skeleton`): every spelling emacs may take as these, and more.
_KEYWORDS = re.compile(r"auto_lisp\s*\(|autoinsertlisp|autoinsertlast")
# verilog-mode's template-function regexp `@"\(\([^\\"]\|\\.\)*\)"` (`.` stops at a newline).
_TEMPLATE_LISP = re.compile(r'@"((?:[^\\"]|\\.)*)"')
_TEMPLATE_START = re.compile(r'@"')
# On the skeleton; a superset of what emacs's case-folded search matches (see emacs.py).
_LOCAL_VARIABLES = re.compile(r"local\s*variables\s*:|-\*-")
_INTEGER = re.compile(r"^[-+]?[0-9]+$")
_BACKREF = re.compile(r"\\[0-9]")
_SYMBOL = re.compile(r"^[A-Za-z0-9_+*/%<>=:!-]+$")
_DIGIT_SYMBOLS = frozenset({"1+", "1-"})
_SPACE = frozenset(" \t\r\n")
_SYMBOL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+*/%<>=:!-"
)
_STRING_CHARS = frozenset(chr(c) for c in range(0x20, 0x7F))
_FOLD_TARGETS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_()*/:-")
_NOT_ASCII = "\x7f"


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


@dataclass(frozen=True)
class _Backref:
    """`'\\N` in a template function: the name the template's regexp group N matched."""

    group: int


@functools.cache
def _fold(char: str) -> str:
    """The ASCII character `char` may stand for under any case folding or compatibility
    form, lowercased; `_NOT_ASCII` when there is none."""
    for form in (
        char.lower(),
        char.upper(),
        char.casefold(),
        unicodedata.normalize("NFKC", char),
        unicodedata.normalize("NFKD", char),
        unicodedata.normalize("NFKC", char).casefold(),
    ):
        head = form[:1].lower()
        if head in _FOLD_TARGETS:
            return head
    return _NOT_ASCII


def skeleton(text: str) -> str:
    """`text` folded to lowercase ASCII, one character for one (see the module)."""
    return "".join(c.lower() if c.isascii() else _fold(c) for c in text)


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _read_string(text: str, i: int, *, escapes: frozenset[str]) -> tuple[str, int]:
    """The string literal opening at `text[i] == '"'`; (its value, end index)."""
    out: list[str] = []
    i += 1
    while i < len(text):
        char = text[i]
        if char == '"':
            return "".join(out), i + 1
        if char == "\\":
            nxt = text[i + 1 : i + 2]
            if nxt not in escapes:
                raise _ParseError("a string holds a backslash escape that is not allowed")
            out.append(nxt)
            i += 2
            continue
        if char not in _STRING_CHARS:
            raise _ParseError(f"a string holds the character {_shown(char)}")
        out.append(char)
        i += 1
    raise _ParseError("a string is not closed")


def _shown(char: str) -> str:
    return repr(char) if char.isprintable() and char.isascii() else f"U+{ord(char):04X}"


def _read(text: str, *, template: bool) -> object:
    """The one form `text` holds (only whitespace may follow it); iterative, at most
    `MAX_DEPTH` deep. `_ParseError` for anything outside the restricted syntax."""
    escapes = frozenset() if template else frozenset("\\")
    n = len(text)
    i = 0
    # Each frame: (items, quote marks pending before the list).
    stack: list[tuple[list[object], int]] = []
    quotes = 0
    result: object = None
    done = False
    while True:
        while i < n and text[i] in _SPACE:
            i += 1
        if done:
            if i < n:
                raise _ParseError("something follows the form (only one form is allowed)")
            return result
        if i >= n:
            raise _ParseError("the form ends early" if not stack else "a list is not closed")
        char = text[i]
        datum: object
        if char == "(":
            if len(stack) >= MAX_DEPTH:
                raise _ParseError(f"lists nest deeper than {MAX_DEPTH}")
            stack.append(([], quotes))
            quotes = 0
            i += 1
            continue
        if char == ")":
            if not stack or quotes:
                raise _ParseError("an unmatched ')'")
            items, quotes = stack.pop()
            datum = items
            i += 1
        elif char == "'":
            quotes += 1
            i += 1
            continue
        elif char == '"':
            datum, i = _read_string(text, i, escapes=escapes)
        elif template and char == "\\" and quotes and _BACKREF.match(text, i):
            datum = _Backref(int(text[i + 1]))
            i += 2
            if i < n and text[i] in _SYMBOL_CHARS:
                raise _ParseError("a back-reference is followed by more of a name")
            quotes -= 1
            datum = _Quoted(datum)
        else:
            if char in "/*" and text[i : i + 2] in ("//", "/*", "*/"):
                raise _ParseError("a comment mark (`//`, `/*`, `*/`)")
            if char in "`,#":
                raise _ParseError(f"the reader syntax {char!r} (only quote is allowed)")
            if char not in _SYMBOL_CHARS:
                raise _ParseError(f"the character {_shown(char)} (outside a string)")
            j = i
            while j < n and text[j] in _SYMBOL_CHARS:
                if text[j] in "/*" and text[j : j + 2] in ("//", "/*", "*/"):
                    raise _ParseError("a comment mark (`//`, `/*`, `*/`)")
                j += 1
            if j < n and text[j] not in _SPACE and text[j] not in "()":
                raise _ParseError(f"the character {_shown(text[j])} (outside a string)")
            atom = text[i:j]
            i = j
            if _INTEGER.match(atom):
                datum = int(atom)
            elif atom[0].isdigit() and atom not in _DIGIT_SYMBOLS:
                raise _ParseError(f"`{atom}` is neither an integer nor a name")
            elif not _SYMBOL.match(atom):
                raise _ParseError(f"`{atom}` is not a name")
            else:
                datum = _Sym(atom)
        while quotes:
            datum = _Quoted(datum)
            quotes -= 1
        if stack:
            stack[-1][0].append(datum)
        else:
            result, done = datum, True


def _nested_repetition(regexp: str) -> bool:
    """Whether an Emacs regexp repeats a group that itself repeats (`\\(a+\\)*`)."""
    groups: list[bool] = []
    i = 0
    while i < len(regexp):
        char = regexp[i]
        if char == "\\" and regexp[i + 1 : i + 2] == "(":
            groups.append(False)
            i += 2
            continue
        if char == "\\" and regexp[i + 1 : i + 2] == ")":
            inner = groups.pop() if groups else False
            i += 2
            after = regexp[i : i + 1]
            if inner and (after in ("*", "+", "?") or regexp[i : i + 2] == "\\{"):
                return True
            if groups:
                groups[-1] = groups[-1] or inner
            continue
        if (char in "*+?" or regexp[i : i + 2] == "\\{") and groups:
            groups[-1] = True
        i += 2 if char == "\\" else 1
    return False


def _check(datum: object, *, setq: bool) -> str | None:
    """Why `datum` may not be evaluated, or None when it may (iterative)."""
    todo: list[tuple[object, bool]] = [(datum, setq)]
    while todo:
        item, may_setq = todo.pop()
        why = _check_one(item, may_setq, todo)
        if why is not None:
            return why
    return None


def _check_one(datum: object, setq: bool, todo: list[tuple[object, bool]]) -> str | None:
    if isinstance(datum, (str, int)):
        return None
    if isinstance(datum, _Quoted):
        if isinstance(datum.datum, (_Sym, _Backref)):
            return None
        return "quotes data other than a name (a quoted list or string can build any text)"
    if isinstance(datum, _Backref):
        return "uses a back-reference that is not quoted (write '\\N)"
    if isinstance(datum, _Sym):
        name = datum.name
        if name in VARIABLES or (name.startswith(":") and len(name) > 1):
            return None
        return f"reads the variable `{name}`, which is not allowed"
    if not isinstance(datum, list):
        return "holds something that is not a plain value"
    if not datum:
        return None
    head = datum[0]
    if not isinstance(head, _Sym):
        return "calls something that is not a function name"
    if head.name == "quote":
        if len(datum) == 2 and isinstance(datum[1], _Sym):
            return None
        return "quotes data other than a name (a quoted list or string can build any text)"
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
            todo.append((value, False))
        return None
    if head.name not in FUNCTIONS:
        return f"calls `{head.name}`, which is not allowed"
    if head.name in REGEXP_FUNCTIONS:
        regexp = datum[1] if len(datum) > 1 else None
        if not isinstance(regexp, str):
            return f"calls `{head.name}` with a regexp that is not a literal string"
        if _nested_repetition(regexp):
            return f"calls `{head.name}` with a regexp that repeats a repeated group"
    todo.extend((arg, False) for arg in datum[1:])
    return None


def _auto_lisp(text: str, start: int) -> str | None:
    """Why the `AUTO_LISP` at `start` (the `a` of the keyword) may not run, or None."""
    begin = start - 2
    if begin < 0 or not text.startswith(ACCEPTED_AUTO_LISP, begin):
        return (
            "AUTO_LISP is allowed only as `/*AUTO_LISP(setq ...)*/` (exact case, no space, "
            "in a block comment of its own): emacs evaluates it wherever it appears"
        )
    open_paren = begin + len(ACCEPTED_AUTO_LISP) - 1
    end = text.find("*/", open_paren)
    if end < 0:
        return "AUTO_LISP cannot be read: its comment is not closed"
    try:
        datum = _read(text[open_paren:end], template=False)
    except _ParseError as exc:
        return f"AUTO_LISP cannot be read: {exc}"
    why = _check(datum, setq=True)
    return None if why is None else f"AUTO_LISP {why}"


def _template(text: str, start: int) -> str | None:
    """Why the template function `@"` at `start` may not run, or None."""
    if start > 0 and text[start - 1] == "@":
        return 'a template function @"..." follows an `@`: its result could form a new one'
    match = _TEMPLATE_LISP.match(text, start)
    if match is None:
        return 'a template function @"..." cannot be read: its string is not closed'
    code = match.group(1).replace('\\"', '"')
    if "@" in code:
        return 'a template function @"..." holds `@` (emacs replaces it with the template number)'
    if "\\" in _BACKREF.sub("", code):
        return 'a template function @"..." holds a backslash other than `\\"` or a back-reference'
    try:
        datum = _read(code, template=True)
    except _ParseError as exc:
        return f'a template function @"..." cannot be read: {exc}'
    why = _check(datum, setq=False)
    return None if why is None else f'a template function @"..." {why}'


def _scan(text: str) -> list[LispProblem]:
    problems: list[LispProblem] = []
    folded = skeleton(text)
    for match in _KEYWORDS.finditer(folded):
        line = _line(text, match.start())
        word = match.group(0)
        if word.startswith("autoinsert"):
            problems.append(
                LispProblem(
                    line,
                    f"{word.upper()} inserts the result of Lisp code: not allowed in a wrapper "
                    "source, in any letter case; use AUTO_TEMPLATE lines instead",
                )
            )
            continue
        why = _auto_lisp(text, match.start())
        if why is not None:
            problems.append(LispProblem(line, why))
    for match in _TEMPLATE_START.finditer(text):
        why = _template(text, match.start())
        if why is not None:
            problems.append(LispProblem(_line(text, match.start()), why))
    for match in _LOCAL_VARIABLES.finditer(folded):
        problems.append(
            LispProblem(
                _line(text, match.start()),
                "file-local variables (`Local Variables:`, `-*-`) are ignored: the generator "
                "pins verilog-mode's settings and reads the IP from the profile's vendor_files",
                note=True,
            )
        )
    return problems


def scan(text: str) -> list[LispProblem]:
    """Every Lisp form of a wrapper source that may not run, and the notes (see above).

    Fails closed: if the scan itself fails, the result is one refusal.
    """
    try:
        problems = _scan(text)
    except Exception as exc:  # any scanner failure is a refusal
        problems = [LispProblem(1, f"the Lisp scanner could not read the source ({exc!r})")]
    return sorted(problems, key=lambda p: (p.line, p.message))


__all__ = [
    "ACCEPTED_AUTO_LISP",
    "FUNCTIONS",
    "MAX_DEPTH",
    "REGEXP_FUNCTIONS",
    "SETQ_VARIABLE",
    "VARIABLES",
    "LispProblem",
    "scan",
    "skeleton",
]

"""Mutants of the oracle: small, deterministic textual edits of its source.

`generate(problem, text)` applies each operator below at each of its sites in the
oracle's code (comments and strings are masked first, so they are never edited), and
keeps at most `mutants.cap` of them, round-robin over the operators in this order (the
first site of each operator, then the second, ...), after dropping the ids in
`mutants.exclude`. Same text in, same mutants out. A mutant's id is `<operator>-<n>`,
`n` counting that operator's sites from 1 in source order.

Operators (each a classic mutation of an RTL bug class):

- `invert_condition`: `if (c)` -> `if (!(c))`, except the reset test (an inverted enable);
- `reset_value`: a sized literal assigned in the reset branch gets its lowest bit flipped
  (a wrong reset value);
- `swap_concat`: `{a, b}` -> `{b, a}` (swapped bits);
- `index_shift`: a constant bit select `x[k]` -> `x[k-1]` (`x[1]` for `x[0]`) (off by one);
- `and_or`: `&&` -> `||`, a binary `&` -> `|`;
- `drop_not`: `~x` / `!x` -> `x`, except the reset test (an inverted polarity);
- `const_flip`: a sized literal on the right of an `assign` gets its lowest bit flipped;
- `enum_next`: a use of an enum member (not its declaration, not a case label) -> the
  next member (a wrong state).

Each mutant must compile and must differ from the oracle on the differential stimulus;
the tests check both (with Verilator) for every problem.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .problems import Problem

OPERATORS = (
    "invert_condition",
    "reset_value",
    "swap_concat",
    "index_shift",
    "and_or",
    "drop_not",
    "const_flip",
    "enum_next",
)

_IDENT = r"[A-Za-z_][A-Za-z0-9_$]*"
_LITERAL_RE = re.compile(r"(\d+)'([bdh])([0-9a-fA-F_]+)")


@dataclass(frozen=True)
class Mutant:
    id: str
    operator: str
    line: int
    before: str
    after: str
    text: str

    def describe(self) -> str:
        return f"{self.id} (line {self.line}): {self.before!r} -> {self.after!r}"


@dataclass(frozen=True)
class _Site:
    start: int
    end: int
    replacement: str


def mask(text: str) -> str:
    """`text` with comments and string literals blanked (same length, newlines kept)."""

    def blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"', blank, text, flags=re.DOTALL)


def _matching(code: str, start: int, open_: str = "(", close: str = ")") -> int | None:
    """Index of the bracket closing the one at `start`."""
    depth = 0
    for index in range(start, len(code)):
        if code[index] == open_:
            depth += 1
        elif code[index] == close:
            depth -= 1
            if depth == 0:
                return index
    return None


def _body_start(code: str) -> int:
    """Where the module body starts: after the port list's `);`."""
    match = re.search(r"\bmodule\b", code)
    if match is None:
        return 0
    paren = code.find("(", match.end())
    hash_ = code.find("#", match.end())
    if hash_ != -1 and hash_ < paren:  # a parameter list first
        end = _matching(code, code.find("(", hash_))
        paren = code.find("(", (end or hash_) + 1)
    end = _matching(code, paren) if paren != -1 else None
    return (end + 1) if end is not None else 0


def _reset_name(problem: Problem) -> str:
    return re.escape(problem.reset.name)


def _is_reset_test(problem: Problem, cond: str) -> bool:
    name = _reset_name(problem)
    return re.fullmatch(rf"\s*[!~]?\s*{name}\s*(==\s*1'b[01]\s*)?", cond) is not None


def _flip(literal: re.Match[str]) -> str:
    width, base, digits = int(literal[1]), literal[2], literal[3].replace("_", "")
    value = int(digits, {"b": 2, "d": 10, "h": 16}[base]) ^ 1
    value &= (1 << width) - 1
    if base == "b":
        return f"{width}'b{value:0{len(digits)}b}"
    if base == "h":
        return f"{width}'h{value:0{len(digits)}x}"
    return f"{width}'d{value}"


def _reset_branches(problem: Problem, code: str) -> Iterator[tuple[int, int]]:
    """(start, end) of each statement run when the reset test of an `if` is true."""
    for match in re.finditer(r"\bif\s*\(", code):
        open_ = match.end() - 1
        close = _matching(code, open_)
        if close is None or not _is_reset_test(problem, code[open_ + 1 : close]):
            continue
        rest = code[close + 1 :]
        begin = re.match(r"\s*begin\b", rest)
        if begin:
            depth, index = 0, close + 1
            for token in re.finditer(r"\bbegin\b|\bend\b", code[close + 1 :]):
                depth += 1 if token[0] == "begin" else -1
                if depth == 0:
                    index = close + 1 + token.end()
                    break
            yield close + 1, index
        else:
            semi = code.find(";", close + 1)
            yield close + 1, semi + 1


def _invert_condition(problem: Problem, code: str, body: int) -> Iterator[_Site]:
    for match in re.finditer(r"\bif\s*\(", code[body:]):
        open_ = body + match.end() - 1
        close = _matching(code, open_)
        if close is None:
            continue
        cond = code[open_ + 1 : close]
        if _is_reset_test(problem, cond):
            continue
        yield _Site(open_ + 1, close, f"!({cond.strip()})")


def _reset_value(problem: Problem, code: str, body: int) -> Iterator[_Site]:
    for start, end in _reset_branches(problem, code):
        for match in re.finditer(rf"<=\s*({_LITERAL_RE.pattern})", code[start:end]):
            lit = _LITERAL_RE.fullmatch(match[1])
            assert lit is not None
            pos = start + match.start(1)
            yield _Site(pos, pos + len(match[1]), _flip(lit))


def _swap_concat(problem: Problem, code: str, body: int) -> Iterator[_Site]:
    for match in re.finditer(r"\{", code[body:]):
        open_ = body + match.start()
        close = _matching(code, open_, "{", "}")
        if close is None:
            continue
        inner = code[open_ + 1 : close]
        if "{" in inner or inner.count(",") != 1:
            continue
        left, right = (part.strip() for part in inner.split(","))
        if left and right:
            yield _Site(open_ + 1, close, f"{right}, {left}")


def _index_shift(problem: Problem, code: str, body: int) -> Iterator[_Site]:
    for match in re.finditer(rf"\b{_IDENT}\[(\d+)\]", code[body:]):
        k = int(match[1])
        pos = body + match.start(1)
        yield _Site(pos, pos + len(match[1]), str(k - 1 if k > 0 else 1))


def _and_or(problem: Problem, code: str, body: int) -> Iterator[_Site]:
    for match in re.finditer(r"&&|&=|&", code[body:]):
        token = match[0]
        if token == "&=":
            continue
        pos = body + match.start()
        if token == "&":
            before = code[:pos].rstrip()
            if not before or not (before[-1].isalnum() or before[-1] in "_])"):
                continue  # a unary reduction `&x`, not a binary and
        yield _Site(pos, pos + len(token), "||" if token == "&&" else "|")


def _drop_not(problem: Problem, code: str, body: int) -> Iterator[_Site]:
    name = problem.reset.name
    for match in re.finditer(rf"(~|!)(?!=)\s*({_IDENT})", code[body:]):
        if match[2] == name:
            continue
        pos = body + match.start()
        yield _Site(pos, body + match.end(), match[2])


def _const_flip(problem: Problem, code: str, body: int) -> Iterator[_Site]:
    for match in re.finditer(r"\bassign\b[^;]*?=([^;]*);", code[body:]):
        rhs_start = body + match.start(1)
        for lit in _LITERAL_RE.finditer(match[1]):
            pos = rhs_start + lit.start()
            yield _Site(pos, pos + len(lit[0]), _flip(lit))


def _enum_members(code: str) -> list[list[tuple[str, int, int]]]:
    """Each enum's members as (name, start, end) of the declaration."""
    out = []
    for match in re.finditer(r"\benum\b[^{;]*\{", code):
        open_ = match.end() - 1
        close = _matching(code, open_, "{", "}")
        if close is None:
            continue
        members = []
        for member in re.finditer(rf"({_IDENT})\s*(?:=[^,}}]*)?(?:,|$)", code[open_ + 1 : close]):
            members.append((member[1], open_ + 1 + member.start(1), open_ + 1 + member.end(1)))
        if len(members) >= 2:
            out.append(members)
    return out


def _enum_next(problem: Problem, code: str, body: int) -> Iterator[_Site]:
    enums = _enum_members(code)
    declared = {(s, e) for members in enums for _, s, e in members}
    following = {}
    for members in enums:
        names = [m[0] for m in members]
        for index, name in enumerate(names):
            following[name] = names[(index + 1) % len(names)]
    if not following:
        return
    pattern = r"\b(" + "|".join(re.escape(n) for n in following) + r")\b"
    for match in re.finditer(pattern, code[body:]):
        start, end = body + match.start(), body + match.end()
        if (start, end) in declared or re.match(r"\s*:(?!:)", code[end:]):
            continue
        yield _Site(start, end, following[match[1]])


_FINDERS: dict[str, Callable[[Problem, str, int], Iterator[_Site]]] = {
    "invert_condition": _invert_condition,
    "reset_value": _reset_value,
    "swap_concat": _swap_concat,
    "index_shift": _index_shift,
    "and_or": _and_or,
    "drop_not": _drop_not,
    "const_flip": _const_flip,
    "enum_next": _enum_next,
}


def candidates(problem: Problem, text: str) -> dict[str, list[Mutant]]:
    """Every mutant of every operator, by operator, in source order (no cap, no exclude)."""
    code = mask(text)
    body = _body_start(code)
    out: dict[str, list[Mutant]] = {}
    for operator in OPERATORS:
        found: list[Mutant] = []
        for site in _FINDERS[operator](problem, code, body):
            before = text[site.start : site.end]
            mutated = text[: site.start] + site.replacement + text[site.end :]
            if mutated == text:
                continue
            found.append(
                Mutant(
                    id=f"{operator}-{len(found) + 1}",
                    operator=operator,
                    line=text.count("\n", 0, site.start) + 1,
                    before=before,
                    after=site.replacement,
                    text=mutated,
                )
            )
        out[operator] = found
    return out


def generate(problem: Problem, text: str) -> list[Mutant]:
    """The problem's mutants: round-robin over the operators, excludes dropped, capped."""
    pools = {
        op: [m for m in found if m.id not in problem.mutants.exclude]
        for op, found in candidates(problem, text).items()
    }
    out: list[Mutant] = []
    depth = 0
    while len(out) < problem.mutants.cap and any(len(p) > depth for p in pools.values()):
        for operator in OPERATORS:
            pool = pools[operator]
            if depth < len(pool) and len(out) < problem.mutants.cap:
                out.append(pool[depth])
        depth += 1
    return out


def oracle_mutants(problem: Problem) -> list[Mutant]:
    """The mutants of the problem's oracle fixture."""
    from .fixtures import strip_header

    return generate(problem, strip_header(problem.fixture(problem.files.oracle)))


__all__ = ["OPERATORS", "Mutant", "candidates", "generate", "mask", "oracle_mutants"]

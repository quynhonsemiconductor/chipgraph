"""`StyleCheck`: a file's code style measured against the project's style exemplars.

DESIGN.md 8.6 V3, task M2-04. A deterministic check (no model): `style_profile` derives a
`StyleProfile` from the files in the profile's `style.exemplars`, and `style_issues`
reports every place a file departs from it, as `Issue`s `style.<key>` on `file:line`
whose message names the value the exemplars use. `style_distance` sums the departures
into one number in [0, 1] (0: the file follows every measured habit).

The features (the keys, `chipgraph.core.config.models.STYLE_KEYS`):

| key           | what is measured                                                       |
|---------------|------------------------------------------------------------------------|
| `indent`      | indentation character and width (the most common indent step)          |
| `begin`       | `begin` at the end of the line, or on a line of its own                |
| `ports`       | port declarations: ANSI or not, one per line, direction keywords padded |
|               | so the types line up                                                   |
| `reset`       | reset style: asynchronous or synchronous, active low or high, its name |
| `reset_kind`  | asynchronous or synchronous reset, only that: always an **error**,       |
|               | whatever `style.enforce` says (a wrong kind is a functional change)    |
| `sequential`  | the sequential block: `always_ff` or `always`, the clock edge          |
| `suffix`      | signal suffixes: the registered (`_q`, `_r`, `_reg`, `_ff`) and         |
|               | next-state (`_d`, `_next`, `_nxt`) ones, `_i`/`_o` on ports            |
| `header`      | a comment header before the module (a `timescale` may come first)     |
| `blank_lines` | the most blank lines in a row                                          |
| `line_length` | the longest line                                                       |
| `case`        | the naming case of local signals: snake, camel or upper                |

A feature the exemplars do not show (no sequential block, no next-state signal) is not
measured. The text is read with comments and strings blanked out, line numbers kept; it
is a style measure over SystemVerilog text, not a parser (lint and naming are the checks
that parse).

Deviations are `warning`s, except for the keys listed in the profile's `style.enforce`
(or arg `enforce`), which are `error`s and fail the check. An `info` issue
`style.distance` gives the distance.

Which files: `chipgraph.checks.format.target_files` (arg `files`, else the module's HDL
files in the block's approved plan); none (e.g. `chipgraph check` over a block) is
`skipped`. Which exemplars: arg `exemplars`, else the profile's `style.exemplars`; none is
`skipped`; a missing one is an `error`. A checked file that is also an exemplar is left
out of the exemplars it is measured against.

Profile example (`.chipgraph.yml`)::

    style:
      exemplars: [rtl/tiny_timer.sv, rtl/tiny_gpio.sv]
      enforce: [indent, reset]
    adapters:
      style: { use: style }
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.checks._common import compute_idempotency_key, sha256_file
from chipgraph.checks.format import TargetError, target_files
from chipgraph.core.config.errors import ConfigError
from chipgraph.core.config.loader import load as load_profile
from chipgraph.core.config.models import STYLE_KEYS, StyleKey
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus, Severity
from chipgraph.core.plugin_api.types import ToolContext

REG_SUFFIXES = ("_q", "_r", "_reg", "_ff")
"""Suffixes that mark a registered signal."""
NEXT_SUFFIXES = ("_d", "_next", "_nxt")
"""Suffixes that mark a register's next value."""
REPORTED_SUFFIXES = ("_q", "_d", "_r", "_next", "_reg", "_n", "_i", "_o")
"""The suffixes whose frequency the profile reports."""
MAX_ISSUES_PER_KEY = 5
"""Issues per feature and file; the rest are counted in the last one."""

Case = Literal["snake", "camel", "upper"]

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"')
_MODULE = re.compile(r"\bmodule\s+([A-Za-z_]\w*)")
_DIRECTION = re.compile(r"\b(input|output|inout)\b")
_PORT_DECL = re.compile(
    r"\b(input|output|inout)\b\s*(?:(?:logic|wire|reg|bit|var)\b\s*)?(?:(?:signed|unsigned)\b\s*)?"
    r"(?:\[[^\]]*\]\s*)*([A-Za-z_]\w*)"
)
_LOCAL_DECL = re.compile(
    r"^\s*(?:logic|reg|wire|bit)\b\s*(?:(?:signed|unsigned)\b\s*)?(?:\[[^\]]*\]\s*)*"
    r"([A-Za-z_]\w*(?:\s*(?:\[[^\]]*\]\s*)*,\s*[A-Za-z_]\w*)*)"
)
_SEQ = re.compile(
    r"\b(always_ff|always)\s*@\s*\(\s*(posedge|negedge)\s+([A-Za-z_]\w*)"
    r"(?:\s*(?:or|,)\s*(posedge|negedge)\s+([A-Za-z_]\w*))?\s*\)"
)
_SYNC_RESET = re.compile(
    r"\bif\s*\(\s*([!~])?\s*([A-Za-z_]\w*)\s*(?:==\s*1'b([01])\s*)?\)", re.IGNORECASE
)
_NONBLOCKING = re.compile(
    r"(?:^|[;:]|\bbegin\b|\belse\b|\))\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*<=", re.MULTILINE
)
_COMB = re.compile(r"\balways_comb\b")
_BLOCKING = re.compile(
    r"(?:^|[;:]|\bbegin\b|\belse\b|\))\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*=(?![=])", re.MULTILINE
)
_RESETLIKE = re.compile(r"rst|reset", re.IGNORECASE)


# --- reading a file ----------------------------------------------------------------------


def _blank(match: re.Match[str]) -> str:
    """The match with every character but a newline replaced by a space."""
    return re.sub(r"[^\n]", " ", match.group(0))


def code_text(text: str) -> str:
    """`text` with comments and strings blanked out; lines and columns kept."""
    text = _STRING.sub(_blank, text)
    text = _BLOCK_COMMENT.sub(_blank, text)
    return _LINE_COMMENT.sub(_blank, text)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _case(name: str) -> Case | None:
    if re.fullmatch(r"[a-z][a-z0-9_]*", name):
        return "snake"
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        return "upper"
    if re.fullmatch(r"[a-z][A-Za-z0-9]*", name) and re.search(r"[A-Z]", name):
        return "camel"
    return None


def _suffix(name: str, suffixes: Sequence[str]) -> str | None:
    return next((s for s in sorted(suffixes, key=len, reverse=True) if name.endswith(s)), None)


@dataclass
class SeqBlock:
    """One sequential block: its keyword, clock, and reset as written."""

    line: int
    keyword: str
    clock_edge: str
    clock: str
    reset_kind: Literal["async", "sync"] | None
    reset_active: Literal["low", "high"] | None
    reset: str | None


@dataclass
class Observed:
    """What one file shows of each feature (the raw facts `style_profile` votes over)."""

    path: str
    lines: list[str]
    indents: list[tuple[int, str]] = field(default_factory=list)
    """(line, leading whitespace) of every indented line of code."""
    steps: list[tuple[int, int]] = field(default_factory=list)
    """(line, step) for every increase of the indent between two lines of code."""
    tab_lines: int = 0
    begins: list[tuple[int, str]] = field(default_factory=list)
    """(line, 'same_line' | 'next_line') for every `begin`."""
    ansi: bool | None = None
    one_per_line: bool | None = None
    aligned: bool | None = None
    port_line: int = 1
    ports: list[tuple[int, str, str]] = field(default_factory=list)
    """(line, direction, name) of every header port."""
    seq: list[SeqBlock] = field(default_factory=list)
    registered: list[tuple[int, str]] = field(default_factory=list)
    """(line, name) of every signal assigned with `<=` (first assignment)."""
    combinational: list[tuple[int, str]] = field(default_factory=list)
    """(line, name) of every local signal assigned with `=` in an `always_comb`."""
    locals: list[tuple[int, str]] = field(default_factory=list)
    """(line, name) of every local signal declaration."""
    header: bool = False
    blank_runs: list[tuple[int, int]] = field(default_factory=list)
    """(first line, length) of every run of blank lines."""
    long_lines: list[tuple[int, int]] = field(default_factory=list)
    """(line, length) of every line, longest first."""


def _module_header(code: str) -> tuple[int, int] | None:
    """The span of the first module header's port list `( ... );`, or None."""
    match = _MODULE.search(code)
    if match is None:
        return None
    pos = match.end()
    params = re.compile(r"\s*#\s*\(").match(code, pos)
    if params is not None:  # skip the parameter list
        depth, i = 1, params.end()
        while i < len(code) and depth:
            depth += {"(": 1, ")": -1}.get(code[i], 0)
            i += 1
        pos = i
    open_ = code.find("(", pos)
    semi = code.find(";", pos)
    if open_ < 0 or (0 <= semi < open_):
        return None
    depth, i = 1, open_ + 1
    while i < len(code) and depth:
        depth += {"(": 1, ")": -1}.get(code[i], 0)
        i += 1
    return open_ + 1, i - 1


def observe(text: str, path: str = "<text>") -> Observed:
    """Every fact `style_profile` and `style_issues` read from one file's text."""
    code = code_text(text)
    raw_lines = text.splitlines()
    code_lines = code.splitlines()
    obs = Observed(path=path, lines=raw_lines)

    # indentation, `begin` placement
    previous: int | None = None
    for number, line in enumerate(code_lines, start=1):
        if not line.strip():
            continue
        lead = line[: len(line) - len(line.lstrip())]
        width = len(lead.expandtabs(8))
        if lead:
            obs.indents.append((number, lead))
            if "\t" in lead:
                obs.tab_lines += 1
        if previous is not None and width > previous:
            obs.steps.append((number, width - previous))
        previous = width
        for match in re.finditer(r"\bbegin\b", line):
            alone = not line[: match.start()].strip()
            obs.begins.append((number, "next_line" if alone else "same_line"))

    # the header comment, blank lines, line lengths
    # A comment before the module (after a `timescale` or an include, say) is a header.
    head: list[str] = []
    for line in raw_lines:
        if re.match(r"\s*(?:module|interface|package)\b", line):
            break
        head.append(line.strip())
    obs.header = any(line.startswith(("//", "/*")) for line in head)
    run_start, run = 0, 0
    for number, line in enumerate([*raw_lines, "x"], start=1):
        if not line.strip():
            run_start, run = (number if run == 0 else run_start), run + 1
            continue
        if run:
            obs.blank_runs.append((run_start, run))
        run = 0
    obs.long_lines = sorted(
        ((n, len(line.rstrip())) for n, line in enumerate(raw_lines, start=1)),
        key=lambda item: (-item[1], item[0]),
    )

    # the port list
    header = _module_header(code)
    if header is not None:
        start, end = header
        body = code[start:end]
        base_line = _line_of(code, start)
        obs.port_line = base_line
        obs.ansi = bool(_DIRECTION.search(body))
        if obs.ansi:
            columns: list[int] = []
            lengths: set[int] = set()
            many = False
            for offset, line in enumerate(body.split("\n")):
                found = list(_DIRECTION.finditer(line))
                if len(found) > 1:
                    many = True
                for match in found:
                    rest = line[match.end() :]
                    columns.append(match.end() + len(rest) - len(rest.lstrip()))
                    lengths.add(len(match.group(1)))
                for port in _PORT_DECL.finditer(line):
                    obs.ports.append((base_line + offset, port.group(1), port.group(2)))
            obs.one_per_line = not many
            # Padded or not shows only when directions of different lengths are declared.
            if len(lengths) > 1:
                obs.aligned = len(set(columns)) == 1

    port_names = {name for _, _, name in obs.ports}

    # sequential blocks and their resets
    for seq_m in _SEQ.finditer(code):
        at = _line_of(code, seq_m.start())
        keyword, edge, clock = seq_m.group(1), seq_m.group(2), seq_m.group(3)
        kind: Literal["async", "sync"] | None = None
        active: Literal["low", "high"] | None = None
        reset: str | None = None
        if seq_m.group(4) is not None:
            second_edge, second = seq_m.group(4), seq_m.group(5)
            if _RESETLIKE.search(clock) and not _RESETLIKE.search(second):
                clock, second, edge, second_edge = second, clock, second_edge, edge
            kind, reset = "async", second
            active = "low" if second_edge == "negedge" else "high"
        else:
            window = code[seq_m.end() : seq_m.end() + 400]
            cond = _SYNC_RESET.search(window)
            if cond is not None and _RESETLIKE.search(cond.group(2)):
                kind, reset = "sync", cond.group(2)
                negated = bool(cond.group(1)) != (cond.group(3) == "0")
                active = "low" if negated else "high"
        obs.seq.append(SeqBlock(at, keyword, edge, clock, kind, active, reset))

    # registered and combinational signals, local declarations
    seen: set[str] = set()
    for nb in _NONBLOCKING.finditer(code):
        name = nb.group(1)
        if name in seen or name in ("if", "else", "begin", "end", "case"):
            continue
        seen.add(name)
        obs.registered.append((_line_of(code, nb.start(1)), name))
    for number, line in enumerate(code_lines, start=1):
        decl = _LOCAL_DECL.match(line)
        if decl is None:
            continue
        for name in re.findall(r"[A-Za-z_]\w*", re.sub(r"\[[^\]]*\]", " ", decl.group(1))):
            if name not in port_names and name not in ("signed", "unsigned"):
                obs.locals.append((number, name))
    local_names = {name for _, name in obs.locals}
    comb_seen: set[str] = set()
    for comb in _COMB.finditer(code):
        stop = min((m.start() for m in re.finditer(r"\balways", code[comb.end() :])), default=None)
        chunk_end = comb.end() + stop if stop is not None else len(code)
        chunk = code[comb.end() : chunk_end]
        for match in _BLOCKING.finditer(chunk):
            name = match.group(1)
            if name in local_names and name not in comb_seen and name not in seen:
                comb_seen.add(name)
                obs.combinational.append((_line_of(code, comb.end() + match.start(1)), name))
    return obs


# --- the profile -------------------------------------------------------------------------


class PortStyle(BaseModel):
    """How the exemplars declare ports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ansi: bool = Field(description="Ports declared in the module header (ANSI style).")
    one_per_line: bool = Field(description="At most one port declaration per line.")
    aligned: bool = Field(description="Direction keywords padded so the types line up.")


class ResetStyle(BaseModel):
    """How the exemplars reset their registers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["async", "sync"] = Field(description="Asynchronous or synchronous reset.")
    active: Literal["low", "high"] = Field(description="The active level.")
    name: str = Field(description="The reset signal's name.")


class SequentialStyle(BaseModel):
    """How the exemplars write a sequential block."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    keyword: str = Field(description="'always_ff' or 'always'.")
    clock_edge: Literal["posedge", "negedge"] = Field(description="The clock edge.")
    clock: str = Field(description="The clock signal's name.")
    template: str = Field(description="The block's head as written, e.g. 'always_ff @(...)'.")


class StyleProfile(BaseModel):
    """The style habits a set of exemplar files share (`style_profile`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    exemplars: tuple[str, ...] = Field(description="The files it was derived from.")
    indent_char: Literal["space", "tab"] | None = Field(default=None, description="Indent char.")
    indent_width: int | None = Field(default=None, description="The indent step, in columns.")
    begin: Literal["same_line", "next_line"] | None = Field(
        default=None, description="Where `begin` goes."
    )
    ports: PortStyle | None = Field(default=None, description="Port declarations.")
    reset: ResetStyle | None = Field(default=None, description="The reset style.")
    sequential: SequentialStyle | None = Field(default=None, description="Sequential blocks.")
    reg_suffix: str | None = Field(default=None, description="Suffix of registered signals.")
    next_suffix: str | None = Field(default=None, description="Suffix of next-state signals.")
    port_suffixes: bool | None = Field(
        default=None, description="Inputs end in _i and outputs in _o."
    )
    suffix_freq: dict[str, float] = Field(
        default_factory=dict, description="Share of declared names with each suffix."
    )
    header: bool | None = Field(default=None, description="A comment header comes first.")
    max_blank_lines: int | None = Field(default=None, description="Most blank lines in a row.")
    max_line_length: int | None = Field(default=None, description="The longest line.")
    local_case: Case | None = Field(default=None, description="Naming case of local signals.")

    def describe(self) -> dict[str, str]:
        """Each measured feature as text, by key (what the RTL Author's context shows)."""
        out: dict[str, str] = {}
        if self.indent_width is not None:
            out["indent"] = f"{self.indent_width} {self.indent_char or 'space'}s per level"
        if self.begin is not None:
            out["begin"] = (
                "`begin` at the end of the line" if self.begin == "same_line" else "`begin` alone"
            )
        if self.ports is not None:
            p = self.ports
            out["ports"] = ", ".join(
                [
                    "ANSI ports in the header" if p.ansi else "non-ANSI ports",
                    "one per line" if p.one_per_line else "several per line",
                    "direction keywords padded so the types line up" if p.aligned else "unaligned",
                ]
            )
        if self.reset is not None:
            r = self.reset
            out["reset"] = f"{r.kind}hronous, active {r.active}, named {r.name}"
        if self.sequential is not None:
            out["sequential"] = self.sequential.template
        parts = []
        if self.reg_suffix is not None:
            parts.append(f"registers end in {self.reg_suffix}")
        if self.next_suffix is not None:
            parts.append(f"next values end in {self.next_suffix}")
        if self.port_suffixes:
            parts.append("inputs end in _i, outputs in _o")
        if parts:
            out["suffix"] = ", ".join(parts)
        if self.header is not None:
            out["header"] = "a comment header first" if self.header else "no comment header"
        if self.max_blank_lines is not None:
            out["blank_lines"] = f"at most {self.max_blank_lines} blank line(s) in a row"
        if self.max_line_length is not None:
            out["line_length"] = f"lines of at most {self.max_line_length} characters"
        if self.local_case is not None:
            out["case"] = f"local signals in {self.local_case} case"
        return out


def _majority[T](values: Iterable[T], share: float = 0.5) -> T | None:
    counts = Counter(values)
    if not counts:
        return None
    value, count = counts.most_common(1)[0]
    return value if count / sum(counts.values()) > share else None


def _width(obs: Iterable[Observed]) -> int | None:
    return _majority(step for o in obs for _, step in o.steps)


def style_profile(exemplars: Mapping[str, str] | Sequence[Observed]) -> StyleProfile:
    """The `StyleProfile` of exemplar files: path to text, or `observe`d files.

    Pure and deterministic: every feature is the habit most of the exemplars' instances
    show (more than half; else it is not measured).
    """
    files = (
        [observe(text, path) for path, text in sorted(exemplars.items())]
        if isinstance(exemplars, Mapping)
        else list(exemplars)
    )
    indented = sum(len(o.indents) for o in files)
    tabs = sum(o.tab_lines for o in files)
    ports = [
        (o.ansi, o.one_per_line, o.aligned)
        for o in files
        if o.ansi is not None and o.one_per_line is not None and o.aligned is not None
    ]
    port_style = _majority(ports)
    blocks = [b for o in files for b in o.seq]
    resets = [(b.reset_kind, b.reset_active, b.reset) for b in blocks if b.reset_kind is not None]
    reset = _majority(resets)
    seq = _majority((b.keyword, b.clock_edge, b.clock) for b in blocks)
    registered = [name for o in files for _, name in o.registered]
    reg = _majority(_suffix(n, REG_SUFFIXES) for n in registered)
    combinational = [name for o in files for _, name in o.combinational]
    nxt = _majority(_suffix(n, NEXT_SUFFIXES) for n in combinational)
    port_names = [(d, n) for o in files for _, d, n in o.ports]
    io = [
        n.endswith("_i") if d == "input" else n.endswith("_o")
        for d, n in port_names
        if d in ("input", "output") and not _RESETLIKE.search(n) and n not in _clocks(blocks)
    ]
    declared = [*(n for _, n in port_names), *(n for o in files for _, n in o.locals)]
    freq = (
        {
            s: round(sum(1 for n in declared if n.endswith(s)) / len(declared), 3)
            for s in REPORTED_SUFFIXES
        }
        if declared
        else {}
    )
    locals_ = [name for o in files for _, name in o.locals]
    return StyleProfile(
        exemplars=tuple(o.path for o in files),
        indent_char=("tab" if tabs * 2 > indented else "space") if indented else None,
        indent_width=_width(files),
        begin=_begin(files),
        ports=PortStyle(ansi=port_style[0], one_per_line=port_style[1], aligned=port_style[2])
        if port_style is not None
        else None,
        reset=ResetStyle(kind=reset[0], active=reset[1], name=reset[2])
        if reset is not None and reset[0] and reset[1] and reset[2]
        else None,
        sequential=SequentialStyle(
            keyword=seq[0],
            clock_edge=seq[1],  # type: ignore[arg-type]
            clock=seq[2],
            template=_template(seq, reset),
        )
        if seq is not None
        else None,
        reg_suffix=reg,
        next_suffix=nxt,
        port_suffixes=(sum(io) / len(io) >= 0.8) if io else None,
        suffix_freq=freq,
        header=_majority(o.header for o in files),
        max_blank_lines=max((n for o in files for _, n in o.blank_runs), default=0)
        if files
        else None,
        max_line_length=max((o.long_lines[0][1] for o in files if o.long_lines), default=None),
        local_case=_majority(c for c in (_case(n) for n in locals_) if c is not None),
    )


def _begin(files: Iterable[Observed]) -> Literal["same_line", "next_line"] | None:
    found = _majority(kind for o in files for _, kind in o.begins)
    return "same_line" if found == "same_line" else "next_line" if found == "next_line" else None


def _clocks(blocks: Iterable[SeqBlock]) -> set[str]:
    return {b.clock for b in blocks}


def _template(seq: tuple[str, str, str], reset: tuple[Any, Any, Any] | None) -> str:
    keyword, edge, clock = seq
    if reset is not None and reset[0] == "async":
        reset_edge = "negedge" if reset[1] == "low" else "posedge"
        return f"{keyword} @({edge} {clock} or {reset_edge} {reset[2]})"
    return f"{keyword} @({edge} {clock})"


# --- deviations --------------------------------------------------------------------------


@dataclass(frozen=True)
class Deviation:
    """One place a file departs from the profile."""

    key: StyleKey
    line: int
    msg: str


def deviations(profile: StyleProfile, obs: Observed) -> tuple[list[Deviation], dict[str, float]]:
    """Every deviation of `obs` from `profile`, and each measured feature's share of them.

    The share is in [0, 1]: 0 when the file follows the habit everywhere it shows it.
    A feature the profile or the file does not show is not in the shares.
    """
    found: list[Deviation] = []
    share: dict[str, float] = {}

    def add(key: StyleKey, line: int, msg: str) -> None:
        found.append(Deviation(key, line, msg))

    # indent
    if profile.indent_width is not None and obs.indents:
        bad = 0.0
        char = "tab" if obs.tab_lines * 2 > len(obs.indents) else "space"
        if profile.indent_char is not None and char != profile.indent_char:
            line = next(n for n, lead in obs.indents if (("\t" in lead) == (char == "tab")))
            add("indent", line, f"indents with {char}s (exemplars: {profile.indent_char}s)")
            bad = 1.0
        width = _width([obs])
        if width is not None and width != profile.indent_width:
            line = next(n for n, step in obs.steps if step == width)
            add(
                "indent",
                line,
                f"indents {width} columns per level (exemplars: {profile.indent_width})",
            )
            bad = 1.0
        share["indent"] = bad
    # begin
    if profile.begin is not None and obs.begins:
        off = [(n, k) for n, k in obs.begins if k != profile.begin]
        where = "at the end of the line" if profile.begin == "same_line" else "on its own line"
        for n, _ in off:
            add("begin", n, f"`begin` placement differs (exemplars: `begin` {where})")
        share["begin"] = len(off) / len(obs.begins)
    # ports
    if profile.ports is not None and obs.ansi is not None:
        want = profile.ports
        have = (obs.ansi, obs.one_per_line, obs.aligned)
        names = ("ANSI ports in the header", "one port per line", "padded direction keywords")
        misses = 0
        wants = (want.ansi, want.one_per_line, want.aligned)
        for name, w, h in zip(names, wants, have, strict=True):
            if h is not None and h != w:
                misses += 1
                add("ports", obs.port_line, f"port style: {name} is {h} (exemplars: {w})")
        share["ports"] = misses / 3
    # reset and sequential blocks
    if obs.seq:
        if profile.reset is not None:
            want_r = profile.reset
            resets = [b for b in obs.seq if b.reset_kind is not None]
            off_r = 0
            for b in resets:
                parts = []
                kind_off = b.reset_kind != want_r.kind
                if kind_off:
                    add(
                        "reset_kind",
                        b.line,
                        f"reset is {b.reset_kind}hronous, the exemplars' is {want_r.kind}hronous: "
                        "a different kind of reset changes the behaviour, not only the style",
                    )
                if b.reset_active != want_r.active:
                    parts.append(f"active {b.reset_active} (exemplars: active {want_r.active})")
                if b.reset != want_r.name:
                    parts.append(f"named {b.reset} (exemplars: {want_r.name})")
                if parts:
                    add("reset", b.line, "reset is " + ", ".join(parts))
                if parts or kind_off:
                    off_r += 1
            missing = [b for b in obs.seq if b.reset_kind is None]
            for b in missing[:1]:
                if not resets:
                    add("reset", b.line, f"no reset found (exemplars: {want_r.kind}hronous)")
            share["reset"] = (off_r + (len(missing) if not resets else 0)) / len(obs.seq)
        if profile.sequential is not None:
            want_s = profile.sequential
            off_s = 0
            for b in obs.seq:
                parts = []
                if b.keyword != want_s.keyword:
                    parts.append(f"`{b.keyword}` (exemplars: `{want_s.keyword}`)")
                if b.clock_edge != want_s.clock_edge or b.clock != want_s.clock:
                    parts.append(
                        f"clocked on {b.clock_edge} {b.clock} "
                        f"(exemplars: {want_s.clock_edge} {want_s.clock})"
                    )
                if parts:
                    off_s += 1
                    add("sequential", b.line, "sequential block " + ", ".join(parts))
            share["sequential"] = off_s / len(obs.seq)
    # suffixes
    checked = 0
    off_x = 0
    if profile.reg_suffix is not None and obs.registered:
        for n, name in obs.registered:
            checked += 1
            if not name.endswith(profile.reg_suffix):
                off_x += 1
                add(
                    "suffix",
                    n,
                    f"register {name!r} does not end in {profile.reg_suffix} "
                    f"(exemplars: registers end in {profile.reg_suffix})",
                )
    if profile.next_suffix is not None:
        for n, name in obs.combinational:
            other = _suffix(name, NEXT_SUFFIXES) or _suffix(name, REG_SUFFIXES)
            if other is None:
                continue
            checked += 1
            if other != profile.next_suffix:
                off_x += 1
                add(
                    "suffix",
                    n,
                    f"{name!r} ends in {other} "
                    f"(exemplars: next values end in {profile.next_suffix})",
                )
    if profile.port_suffixes is not None and obs.ports:
        clocks = _clocks(obs.seq)
        for n, d, name in obs.ports:
            if d not in ("input", "output") or _RESETLIKE.search(name) or name in clocks:
                continue
            checked += 1
            has = name.endswith("_i") if d == "input" else name.endswith("_o")
            if has != profile.port_suffixes:
                off_x += 1
                want_txt = "end in _i/_o" if profile.port_suffixes else "carry no _i/_o suffix"
                add("suffix", n, f"port {name!r}: exemplar ports {want_txt}")
    if checked:
        share["suffix"] = off_x / checked
    # header
    if profile.header is not None:
        ok = obs.header == profile.header
        if not ok:
            add(
                "header",
                1,
                "a comment header "
                + ("is missing" if profile.header else "comes first; the exemplars have none"),
            )
        share["header"] = 0.0 if ok else 1.0
    # blank lines
    if profile.max_blank_lines is not None:
        runs = [(n, k) for n, k in obs.blank_runs if k > profile.max_blank_lines]
        for n, k in runs:
            add(
                "blank_lines",
                n,
                f"{k} blank lines in a row (exemplars: at most {profile.max_blank_lines})",
            )
        share["blank_lines"] = 1.0 if runs else 0.0
    # line length
    if profile.max_line_length is not None and obs.long_lines:
        over = [(n, k) for n, k in obs.long_lines if k > profile.max_line_length]
        for n, k in sorted(over):
            add(
                "line_length",
                n,
                f"line of {k} characters (exemplars: at most {profile.max_line_length})",
            )
        share["line_length"] = 1.0 if over else 0.0
    # case of locals
    if profile.local_case is not None and obs.locals:
        off_c = [(n, name) for n, name in obs.locals if _case(name) != profile.local_case]
        for n, name in off_c:
            add(
                "case",
                n,
                f"local signal {name!r} is not {profile.local_case} case "
                f"(exemplars: {profile.local_case} case)",
            )
        share["case"] = len(off_c) / len(obs.locals)
    return found, share


def style_distance(profile: StyleProfile, file: str | Observed) -> float:
    """How far a file (its text, or `observe`d) is from `profile`: the mean, over the
    features both show, of the share of the file that departs from the habit; 0 to 1."""
    obs = observe(file) if isinstance(file, str) else file
    _, share = deviations(profile, obs)
    return round(sum(share.values()) / len(share), 4) if share else 0.0


ALWAYS_ERRORS: frozenset[str] = frozenset({"reset_kind"})
"""Keys reported as errors whatever `style.enforce` lists: a deviation that changes behaviour."""


def style_issues(
    profile: StyleProfile,
    obs: Observed,
    *,
    enforce: Iterable[str] = (),
) -> list[Issue]:
    """The deviations of one file as `Issue`s (`error` for the keys in `enforce`)."""
    strict = set(enforce)
    found, _ = deviations(profile, obs)
    by_key: dict[str, list[Deviation]] = {}
    for dev in found:
        by_key.setdefault(dev.key, []).append(dev)
    issues: list[Issue] = []
    for key in STYLE_KEYS:
        devs = by_key.get(key, [])
        severity: Severity = "error" if key in strict or key in ALWAYS_ERRORS else "warning"
        for dev in devs[:MAX_ISSUES_PER_KEY]:
            issues.append(
                Issue(
                    file=obs.path,
                    line=dev.line,
                    rule=f"style.{key}",
                    severity=severity,
                    msg=dev.msg,
                )
            )
        if len(devs) > MAX_ISSUES_PER_KEY:
            issues[-1] = issues[-1].model_copy(
                update={"msg": f"{issues[-1].msg} (and {len(devs) - MAX_ISSUES_PER_KEY} more)"}
            )
    return issues


# --- the check -----------------------------------------------------------------------------


def _settings(args: Mapping[str, Any], root: Path) -> tuple[list[str], list[str]]:
    """The exemplars and the enforced keys: args first, else the profile's `style`."""
    exemplars = args.get("exemplars")
    enforce = args.get("enforce")
    if exemplars is None or enforce is None:
        resolved = load_profile(root)
        style = resolved.profile.style if resolved is not None else None
        if exemplars is None:
            exemplars = list(style.exemplars) if style is not None else []
        if enforce is None:
            enforce = list(style.enforce) if style is not None else []
    for name, value in (("exemplars", exemplars), ("enforce", enforce)):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise TargetError(f"args[{name!r}] must be a list of strings")
    unknown = sorted(set(enforce) - set(STYLE_KEYS))
    if unknown:
        raise TargetError(f"unknown style keys in enforce: {', '.join(unknown)}")
    return list(exemplars), list(enforce)


def _result(
    spec: CheckSpec,
    status: CheckStatus,
    issues: list[Issue],
    start: float,
    hashes: list[tuple[str, str]],
) -> CheckResult:
    return CheckResult(
        check_id=spec.id,
        status=status,
        issues=tuple(issues),
        duration_s=time.monotonic() - start,
        idempotency_key=compute_idempotency_key(spec.id, spec.args, hashes),
    )


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8", errors="replace")


class StyleCheck:
    """Measures a task's output files against the project's style exemplars."""

    id = "style"
    name = "Style"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        root = ctx.repo_root
        try:
            files = target_files(spec.args, ctx.params, root)
            exemplars, enforce = _settings(spec.args, root)
        except (TargetError, ConfigError) as exc:
            return _result(spec, "error", [Issue(rule="style.args", msg=str(exc))], start, [])
        if files is None:
            note = "nothing to measure: the style check runs on an agent task's outputs"
            return _result(
                spec,
                "skipped",
                [Issue(severity="info", rule="style.no_files", msg=note)],
                start,
                [],
            )
        if not exemplars:
            note = "no style.exemplars in the profile: nothing to measure the files against"
            return _result(
                spec,
                "skipped",
                [Issue(severity="info", rule="style.no_exemplars", msg=note)],
                start,
                [],
            )
        missing = [e for e in exemplars if not (root / e).is_file()]
        if missing:
            msg = f"style exemplars not found: {', '.join(missing)}"
            return _result(spec, "error", [Issue(rule="style.exemplars", msg=msg)], start, [])

        issues: list[Issue] = []
        hashes = [(e, sha256_file(root / e)) for e in exemplars]
        cache: dict[tuple[str, ...], StyleProfile] = {}
        for rel in files:
            if not (root / rel).is_file():
                continue  # the engine's own output check reports a missing output
            own = tuple(e for e in exemplars if e != rel) or tuple(exemplars)
            if own not in cache:
                cache[own] = style_profile({e: _read(root, e) for e in own})
            profile = cache[own]
            obs = observe(_read(root, rel), rel)
            issues.extend(style_issues(profile, obs, enforce=enforce))
            issues.append(
                Issue(
                    file=rel,
                    severity="info",
                    rule="style.distance",
                    msg=f"style distance to the exemplars: {style_distance(profile, obs):.4f}",
                )
            )
            hashes.append((rel, sha256_file(root / rel)))
        status: CheckStatus = "fail" if any(i.severity == "error" for i in issues) else "pass"
        return _result(spec, status, issues, start, hashes)


__all__ = [
    "NEXT_SUFFIXES",
    "REG_SUFFIXES",
    "Deviation",
    "Observed",
    "PortStyle",
    "ResetStyle",
    "SequentialStyle",
    "StyleCheck",
    "StyleProfile",
    "code_text",
    "deviations",
    "observe",
    "style_distance",
    "style_issues",
    "style_profile",
]

"""The `decide()` question for a failing log: choices `infra|rtl|tb|spec`, and its context.

The context is what a model needs and no more (DESIGN 5.4, shortened logs): the failing
check, the log excerpt, the parsed issues and, for a simulation failure, the spec lines the
Design Model and the indexed documents hold about what failed (`/ask`'s retrieval), plus
the chip-level map entries of the registers and blocks the failure names: a simulation
mismatch is only decidable against the spec (is the testbench's expectation or the
design's answer the one the spec gives?), and a test of a top-level design drives its
blocks through the chip's address map.

A simulation failure is a self-checking testbench's `FAIL ...` / `expected X got Y` line
or the message of a failed assertion (`$error`, `assert`): `failure_lines`.

Spec lines never come from RTL or testbench files (those are the two sides being judged)
and never from an 'nda' file. The question id is `triage.<hash of the log and check>`, so
the same log asks the same question and a recorded answer is picked up again.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.core.contracts.types import DataLabel
from chipgraph.core.engine.decide import Question
from chipgraph.core.model.entities import EntityBase
from chipgraph.packs.assist.ask._project import AskProject
from chipgraph.packs.assist.ask.contract import AskSource
from chipgraph.packs.assist.ask.retrieve import retrieve
from chipgraph.packs.assist.triage.contract import LABELS, SpecLine
from chipgraph.packs.assist.triage.parse import ParsedLog
from chipgraph.packs.assist.triage.rules import TriageFacts, is_rtl_path, is_tb_path, where

QUESTION_PREFIX = "triage."
MAX_SPEC_LINES = 10
MAX_MAP_LINES = 6
MAX_SPEC_TEXT = 240
MAX_ISSUES = 12
MAX_FAILURES = 6

PROMPT = """\
A chip design run failed. Which side has to change to fix it?
- infra: the environment or the flow, not the design: a missing tool or file, a wrong \
path, a missing make target, a time limit, a crash or a licence problem.
- rtl: the RTL design is wrong: lint or compile errors in RTL sources, or a simulation \
result that disagrees with the spec.
- tb: the testbench or test is wrong: it does not compile, drives the design otherwise \
than the spec describes (a wrong address, port or reset), or expects a value the spec \
does not give.
- spec: the specification (MAS, chip contract) is wrong or incomplete, or contradicts the \
RTL and its users.
For a simulation mismatch, compare the testbench's stimulus and expected value with the \
spec lines: if the spec agrees with the expected value, the RTL is wrong; if the spec \
agrees with what the design returned, or the testbench drove the design in a way the spec \
does not describe, the testbench is wrong."""
"""The question every triage asks a model (choices: `LABELS`)."""

_IDENT = re.compile(r"\w+(?:[-.]\w+)*")

_FAIL_WORD = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(?:FAIL(?:ED|URE)?|MISMATCH)\b[:\s]*")


def question_id(parsed: ParsedLog, check_id: str | None) -> str:
    """`triage.<16 hex>`: a hash of the check id and the log text."""
    digest = hashlib.sha256(f"{check_id or ''}\n{parsed.text}".encode()).hexdigest()
    return QUESTION_PREFIX + digest[:16]


def _not_spec(path: str, facts: TriageFacts) -> bool:
    """RTL, testbench and filelist files are not spec sources."""
    return (
        is_rtl_path(path, facts.tb_patterns)
        or is_tb_path(path, facts.tb_patterns)
        or path.endswith(".f")
    )


def _file_of(citation: str | None) -> str | None:
    if not citation or citation.startswith("model:"):
        return None
    return citation.rsplit(":", 1)[0] if re.search(r":\d+(?:-\d+)?$", citation) else citation


def failure_lines(parsed: ParsedLog) -> tuple[str, ...]:
    """What the simulation says failed: the self-check failure lines, then the messages of
    its failed assertions (a testbench that reports with `$error` or `assert`)."""
    lines = list(parsed.sim_failures)
    for message in parsed.sim_assertions:
        if not any(message in line for line in lines):
            lines.append(message)
    return tuple(lines)


def spec_lines(
    ctx: AppContext, parsed: ParsedLog, facts: TriageFacts
) -> tuple[tuple[SpecLine, ...], tuple[DataLabel, ...]]:
    """The spec sources about a simulation failure, and their data labels.

    The `/ask` retrieval for the failure lines (`failure_lines`), then the chip-level map
    entries of what they name (`chip_map_sources`): the registers, and the address map
    of the blocks and of the buses they sit on. A test of a top-level design drives the
    blocks through that map, so it is only decidable against it.

    Empty when the log shows no simulation failure, or the project has no Design Model
    yet (`chipgraph ingest`): the question is then asked without them.
    """
    failures = failure_lines(parsed)
    if not failures:
        return (), ()
    try:
        project = AskProject.load(ctx)
    except AppError:
        return (), ()
    texts = [_FAIL_WORD.sub("", line) for line in failures[:3]]
    query = " ".join(t for t in texts if t)
    sources = retrieve(project, query, limit=MAX_SPEC_LINES * 2).sources if query.strip() else ()
    lines: list[SpecLine] = []
    labels: set[DataLabel] = set()
    _collect(project, sources, facts, MAX_SPEC_LINES, lines, labels, set())
    named = "\n".join((*failures, *parsed.runtime_lines))
    _collect(
        project,
        chip_map_sources(project, named),
        facts,
        len(lines) + MAX_MAP_LINES,
        lines,
        labels,
        {line.citation for line in lines},
        by_citation=True,
    )
    return tuple(lines), tuple(sorted(labels))


def spec_sources(
    ctx: AppContext, query: str, facts: TriageFacts, limit: int = MAX_SPEC_LINES
) -> tuple[tuple[SpecLine, ...], tuple[DataLabel, ...]]:
    """Spec-side sources for `query` (`/ask` retrieval, no RTL or testbench file)."""
    if not query.strip():
        return (), ()
    try:
        project = AskProject.load(ctx)
    except AppError:
        return (), ()
    context = retrieve(project, query, limit=limit * 2)
    lines: list[SpecLine] = []
    labels: set[DataLabel] = set()
    _collect(project, context.sources, facts, limit, lines, labels, set())
    return tuple(lines), tuple(sorted(labels))


def _collect(
    project: AskProject,
    sources: Iterable[AskSource],
    facts: TriageFacts,
    limit: int,
    lines: list[SpecLine],
    labels: set[DataLabel],
    seen: set[str],
    *,
    by_citation: bool = False,
) -> None:
    """Add the spec-side `sources` to `lines` (and their labels), up to `limit` lines.

    A source whose location is in `seen` already is skipped, or with `by_citation` one
    whose citation is (two entities read from one table row are both kept).
    """
    for source in sources:
        if len(lines) >= limit:
            break
        files = [f for f in (_file_of(source.citation), _file_of(source.defined_at)) if f]
        if any(_not_spec(f, facts) for f in files):
            continue
        # A document line the model entity was read from says the same thing again.
        location = source.defined_at or source.citation
        key = source.citation if by_citation else location
        if key in seen:
            continue
        seen.update((key, location))
        text = " ".join(source.text.split())
        if len(text) > MAX_SPEC_TEXT:
            text = text[: MAX_SPEC_TEXT - 1] + "…"
        defined_at = source.defined_at if source.defined_at != source.citation else None
        lines.append(SpecLine(citation=source.citation, text=text, defined_at=defined_at))
        labels.update(project.labels.label_for(f) for f in files)


# --- the chip-level map of what a failure names -------------------------------------------


def _words(text: str) -> list[str]:
    """The identifiers of `text`, in order, without repeats (`CTRL.EN` is one)."""
    words: list[str] = []
    for match in _IDENT.finditer(text):
        word = match.group(0).strip("-.")
        if word and word not in words:
            words.append(word)
    return words


def _named_blocks(project: AskProject, words: list[str]) -> list[str]:
    """The keys of the blocks the words name, in order: by the name of the block, of one
    of its modules, or of an instance of one.

    A word that names nothing is split, at `.` and `-` then at `_`, and its parts are
    tried: a top-level port or instance is usually named after its block (`<block>_irq`,
    `u_<block>`, `dut.u_<block>.q`).
    """
    owners: dict[str, list[str]] = {}

    def own(name: str, block: str | None) -> None:
        if name and block and project.get(block) is not None:
            owners.setdefault(name.lower(), []).append(block)

    for entity in sorted(project.model.by_kind("block"), key=lambda e: e.key):
        if project.visible(entity):
            own(entity.name, entity.key)
    for module in sorted(project.model.by_kind("module"), key=lambda e: e.key):
        if project.visible(module):
            own(module.name, project.block_of(module))
    for relation in project.model.relations:
        child = project.get(relation.dst) if relation.kind == "instantiates" else None
        if child is not None:
            own(str(relation.attrs.get("instance_name", "")), project.block_of(child))

    blocks: list[str] = []

    def named(word: str) -> bool:
        found = owners.get(word.lower(), [])
        blocks.extend(b for b in found if b not in blocks)
        return bool(found)

    for word in words:
        if named(word):
            continue
        for segment in re.split(r"[.\-]+", word):
            if segment and not named(segment):
                for part in segment.split("_"):
                    if part:
                        named(part)
    return blocks


def _related_blocks(project: AskProject, blocks: list[str]) -> list[str]:
    """`blocks`, then their IP blocks or instances (`instance_of`), then the other blocks
    on the buses they connect to: the address map a test of the chip addresses them by."""
    related = list(blocks)

    def add(key: str) -> None:
        if key not in related and project.get(key) is not None:
            related.append(key)

    for relation in project.model.relations:
        if relation.kind == "instance_of":
            if relation.src in blocks:
                add(relation.dst)
            elif relation.dst in blocks:
                add(relation.src)
    buses = [r.src for r in project.model.relations if r.kind == "connects" and r.dst in related]
    for relation in project.model.relations:
        if relation.kind == "connects" and relation.src in buses:
            add(relation.dst)
    return related


def _named_registers(project: AskProject, words: list[str], blocks: list[str]) -> list[EntityBase]:
    """The registers the words name exactly (`CTRL`, or `CTRL.EN` for a field of it): of
    the named blocks (or their IP blocks) when there are any, else of any block."""
    names = set(words) | {w.split(".")[0] for w in words if "." in w}
    found = [
        r
        for r in sorted(project.model.by_kind("register"), key=lambda e: e.key)
        if r.name in names and project.visible(r)
    ]
    if blocks:
        return [r for r in found if project.block_of(r) in blocks]
    return found


def chip_map_sources(project: AskProject, text: str) -> list[AskSource]:
    """The chip-level map entries of what `text` names, most specific first.

    The registers it names (of the blocks it names, when it names any); the memory
    regions of the blocks it names (`_named_blocks`) or whose registers it names, of
    their IP blocks or instances (`instance_of`), and of the other blocks on the same
    buses.
    """
    words = _words(text)
    blocks = _named_blocks(project, words)
    registers = _named_registers(project, words, _related_blocks(project, blocks))
    for register in registers:
        block = project.block_of(register)
        if block is not None and block not in blocks:
            blocks.append(block)
    related = _related_blocks(project, blocks)
    order = {key: index for index, key in enumerate(related)}
    regions = sorted(
        (
            r
            for r in project.model.by_kind("memory_region")
            if project.visible(r) and project.block_of(r) in order
        ),
        key=lambda r: (order[project.block_of(r) or ""], r.key),
    )
    return [
        AskSource(
            citation=f"model:{entity.key}",
            kind="model",
            text=project.summary(entity),
            origin="lookup",
            defined_at=project.defined_at(entity),
        )
        for entity in (*registers, *regions)
    ]


def build_context(parsed: ParsedLog, facts: TriageFacts, specs: tuple[SpecLine, ...] = ()) -> str:
    """The text a model reads with the question."""
    parts: list[str] = []
    checks = parsed.failing_checks
    if checks:
        named = ", ".join(
            f"{c.check_id} ({facts.kind_of(c.check_id)}, {c.status})"
            + (f" for block {c.block}" if c.block else "")
            for c in checks
        )
        parts.append(f"Failing check: {named}")
    elif facts.check_id:
        parts.append(f"Failing check: {facts.check_id} ({facts.kind_of(facts.check_id)})")
    parts.append(f"Log excerpt:\n{parsed.excerpt or '(empty log)'}")
    issues = parsed.issues[:MAX_ISSUES]
    if issues:
        rows = [
            f"- {where(i) or '(no file)'} [{i.rule or '-'}] {i.severity}: {i.msg}" for i in issues
        ]
        parts.append("Parsed issues:\n" + "\n".join(rows))
    failures = failure_lines(parsed)
    if failures:
        rows = [f"- {line}" for line in failures[:MAX_FAILURES]]
        parts.append("Simulation self-check failures:\n" + "\n".join(rows))
    if specs:
        rows = [
            f"- {s.citation}" + (f" ({s.defined_at})" if s.defined_at else "") + f": {s.text}"
            for s in specs
        ]
        parts.append(
            "Spec lines (from the project's Design Model and specification documents):\n"
            + "\n".join(rows)
        )
    elif failures:
        parts.append("Spec lines: none found (no Design Model, or nothing matched).")
    return "\n\n".join(parts)


def build_question(
    parsed: ParsedLog,
    facts: TriageFacts,
    *,
    specs: tuple[SpecLine, ...] = (),
    labels: tuple[DataLabel, ...] = (),
) -> Question:
    """The `decide()` question for `parsed`."""
    return Question(
        id=question_id(parsed, facts.check_id),
        prompt=PROMPT,
        choices=LABELS,
        context=build_context(parsed, facts, specs),
        labels=tuple(sorted(set(labels))),
    )


__all__ = [
    "PROMPT",
    "QUESTION_PREFIX",
    "build_context",
    "build_question",
    "chip_map_sources",
    "failure_lines",
    "question_id",
    "spec_lines",
    "spec_sources",
]

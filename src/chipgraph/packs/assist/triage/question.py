"""The `decide()` question for a failing log: choices `infra|rtl|tb|spec`, and its context.

The context is what a model needs and no more (DESIGN 5.4, shortened logs): the failing
check, the log excerpt, the parsed issues and, for a simulation failure, the spec lines the
Design Model and the indexed documents hold about what failed (`/ask`'s retrieval): a
simulation mismatch is only decidable against the spec (is the testbench's expectation
or the design's answer the one the spec gives?).

Spec lines never come from RTL or testbench files (those are the two sides being judged)
and never from an 'nda' file. The question id is `triage.<hash of the log and check>`, so
the same log asks the same question and a recorded answer is picked up again.
"""

from __future__ import annotations

import hashlib
import re

from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.core.contracts.types import DataLabel
from chipgraph.core.engine.decide import Question
from chipgraph.packs.assist.ask._project import AskProject
from chipgraph.packs.assist.ask.retrieve import retrieve
from chipgraph.packs.assist.triage.contract import LABELS, SpecLine
from chipgraph.packs.assist.triage.parse import ParsedLog
from chipgraph.packs.assist.triage.rules import TriageFacts, is_rtl_path, is_tb_path, where

QUESTION_PREFIX = "triage."
MAX_SPEC_LINES = 10
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


def spec_lines(
    ctx: AppContext, parsed: ParsedLog, facts: TriageFacts
) -> tuple[tuple[SpecLine, ...], tuple[DataLabel, ...]]:
    """The spec sources about a simulation failure, and their data labels.

    Empty when the log shows no simulation failure, or the project has no Design Model
    yet (`chipgraph ingest`): the question is then asked without them.
    """
    if not parsed.sim_failures:
        return (), ()
    failures = [_FAIL_WORD.sub("", line) for line in parsed.sim_failures[:3]]
    return spec_sources(ctx, " ".join(f for f in failures if f), facts)


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
    seen: set[str] = set()
    for source in context.sources:
        files = [f for f in (_file_of(source.citation), _file_of(source.defined_at)) if f]
        if any(_not_spec(f, facts) for f in files):
            continue
        # A document line the model entity was read from says the same thing again.
        location = source.defined_at or source.citation
        if location in seen:
            continue
        seen.add(location)
        text = " ".join(source.text.split())
        if len(text) > MAX_SPEC_TEXT:
            text = text[: MAX_SPEC_TEXT - 1] + "…"
        defined_at = source.defined_at if source.defined_at != source.citation else None
        lines.append(SpecLine(citation=source.citation, text=text, defined_at=defined_at))
        labels.update(project.labels.label_for(f) for f in files)
        if len(lines) >= limit:
            break
    return tuple(lines), tuple(sorted(labels))


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
    if parsed.sim_failures:
        rows = [f"- {line}" for line in parsed.sim_failures[:MAX_FAILURES]]
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
    elif parsed.sim_failures:
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
    "question_id",
    "spec_lines",
    "spec_sources",
]

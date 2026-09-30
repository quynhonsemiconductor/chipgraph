"""`ask_check`: verify an `/ask` answer's citations before anyone sees the answer.

Rules (DESIGN 4.8: no evidence, no claim):

- A citation is either `path:line` / `path:start-end` (the path must be an indexed
  document and every line must exist; a range spans at most `MAX_RANGE` lines) or
  `model:<key>` (the key must exist in the Design Model). A bare model key is accepted and
  written back as `model:<key>`. Nothing from an `nda` file is citable.
- Every citation must be valid: one invented citation rejects the answer.
- An answer with `unknown: false` needs at least one valid citation and some text.
- An answer with `unknown: true` needs no citation (and any it gives must be valid).

The result is the verified answer (citations in canonical form, duplicates dropped) or the
reasons it was rejected, with a verdict per citation so the answerer can fix it.
"""

from __future__ import annotations

import difflib
import re

from chipgraph.app.context import AppContext
from chipgraph.packs.assist.ask._project import AskProject
from chipgraph.packs.assist.ask.contract import (
    UNKNOWN_ANSWER,
    AskAnswer,
    AskCheck,
    CitationCheck,
)

MAX_RANGE = 30
"""A `path:start-end` citation may span at most this many lines."""

MAX_CITATIONS = 20
"""An answer may carry at most this many citations."""

_FILE_LINE = re.compile(r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")
_MAX_TEXT = 400


def ask_check(ctx: AppContext, answer: AskAnswer) -> AskCheck:
    """Verify `answer` against the project of `ctx` (see the module docstring)."""
    return check_answer(AskProject.load(ctx), answer)


def check_answer(project: AskProject, answer: AskAnswer) -> AskCheck:
    """`ask_check` on an already loaded `AskProject`."""
    reasons: list[str] = []
    checks = [check_citation(project, c) for c in answer.citations[:MAX_CITATIONS]]
    if len(answer.citations) > MAX_CITATIONS:
        reasons.append(
            f"too many citations ({len(answer.citations)}); cite at most {MAX_CITATIONS}"
        )
    for check in checks:
        if not check.valid:
            reasons.append(f"invalid citation {check.citation!r}: {check.reason}")
    valid = [c for c in checks if c.valid]
    text = answer.answer.strip()
    if not answer.unknown:
        if not text:
            reasons.append("the answer text is empty")
        if not valid:
            reasons.append(
                "an answer needs at least one valid citation ('path:line' or 'model:<key>' "
                "from ask_context); if the sources do not answer the question, set unknown: true"
            )
    if reasons:
        return AskCheck(ok=False, citations=tuple(checks), reasons=tuple(reasons))

    canonical: list[str] = []
    for check in valid:
        if check.normalized is not None and check.normalized not in canonical:
            canonical.append(check.normalized)
    verified = AskAnswer(
        answer=text or UNKNOWN_ANSWER,
        citations=tuple(canonical),
        unknown=answer.unknown,
    )
    return AskCheck(ok=True, answer=verified, citations=tuple(checks))


def check_citation(project: AskProject, citation: str) -> CitationCheck:
    """The verdict on one citation string."""
    raw = citation
    text = citation.strip()
    if not text:
        return CitationCheck(citation=raw, valid=False, reason="empty citation")
    if text.startswith("model:"):
        return _check_key(project, raw, text.removeprefix("model:").strip())
    match = _FILE_LINE.match(text)
    if match is not None:
        path = _normalise_path(match.group("path"))
        if project.docs.is_indexed(path) or project.model.get(text) is None:
            return _check_lines(project, raw, path, match.group("start"), match.group("end"))
    if project.model.get(text) is not None:
        return _check_key(project, raw, text)
    return CitationCheck(
        citation=raw,
        valid=False,
        reason=(
            "not a 'path:line' or 'model:<key>' citation"
            + _did_you_mean(text, list(project.model.entities))
        ),
    )


def _check_key(project: AskProject, raw: str, key: str) -> CitationCheck:
    entity = project.model.get(key)
    if entity is None:
        return CitationCheck(
            citation=raw,
            valid=False,
            kind="model",
            reason=f"no model key {key!r}" + _did_you_mean(key, list(project.model.entities)),
        )
    if not project.visible(entity):
        return CitationCheck(
            citation=raw,
            valid=False,
            kind="model",
            reason=f"{key!r} comes from an 'nda' file, which /ask may not cite",
        )
    return CitationCheck(
        citation=raw,
        valid=True,
        kind="model",
        normalized=f"model:{key}",
        text=_clip(project.summary(entity)),
    )


def _check_lines(
    project: AskProject, raw: str, path: str, start_text: str, end_text: str | None
) -> CitationCheck:
    counts = project.docs.line_counts()
    if path not in counts:
        label = project.labels.label_for(path)
        why = (
            f"{path} is labelled 'nda' and is never indexed for /ask"
            if label == "nda"
            else f"{path} is not an indexed document" + _did_you_mean(path, list(counts))
        )
        return CitationCheck(citation=raw, valid=False, kind="document", reason=why)
    start = int(start_text)
    end = int(end_text) if end_text is not None else start
    total = counts[path]
    if start < 1 or end < start:
        reason = f"bad line range {start}-{end}"
    elif end > total:
        reason = f"{path} has {total} lines; line {end} does not exist"
    elif end - start + 1 > MAX_RANGE:
        reason = f"a range may span at most {MAX_RANGE} lines; cite the exact lines"
    else:
        rows = project.docs.lines(path, start, end)
        normalized = f"{path}:{start}" if start == end else f"{path}:{start}-{end}"
        return CitationCheck(
            citation=raw,
            valid=True,
            kind="document",
            normalized=normalized,
            text=_clip("\n".join(t for _, t in rows)),
        )
    return CitationCheck(citation=raw, valid=False, kind="document", reason=reason)


def _normalise_path(path: str) -> str:
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _did_you_mean(name: str, choices: list[str]) -> str:
    matches = difflib.get_close_matches(name, choices, n=3, cutoff=0.6)
    return f"; did you mean: {', '.join(matches)}" if matches else ""


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 3] + "..."


__all__ = ["MAX_CITATIONS", "MAX_RANGE", "ask_check", "check_answer", "check_citation"]

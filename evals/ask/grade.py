"""Deterministic grader for the `/ask` evals (task M1-13; reused by M1-17).

    python evals/ask/grade.py ANSWERS.jsonl [--questions evals/ask/tinysoc.yml] [--json]

ANSWERS.jsonl has one JSON object per line, one per question:

    {"id": "q01", "answer": "...", "citations": ["path:line" | "model:<key>", ...],
     "unknown": false, "checked": true}

`checked` (optional, default true) is whether the answer passed `ask_check`; an
unchecked answer never passes. A question with `expected` citations passes when the answer
is not `unknown`, at least one citation matches an expected one (the same model key, or
the same file with overlapping line ranges), and the text states every expected fact (one
of its alternatives; see `fact_pattern`). A question with `unknown: true` passes only when
the answer is `unknown`; any other answer to it is counted as *invented*.

A fact alternative is either text, found case-insensitively and on word boundaries (`0x2`
does not match `0x20`, `8 bits` does not match `128 bits`), or `re:<regex>`, searched
case-insensitively, for facts that need context (`reset ... 0`, not any `0`).

The run passes when the correct-citation rate on answerable questions is at least 90 %
and no answer was invented. Exit code: 0 pass, 1 fail, 2 bad input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_QUESTIONS = Path(__file__).resolve().parent / "tinysoc.yml"
MIN_CORRECT_RATE = 0.9
MAX_INVENTED = 0

_FILE_LINE = re.compile(r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")
_WORD_CHAR = re.compile(r"\w")
REGEX_PREFIX = "re:"


class GradeError(ValueError):
    """Bad questions or answers input."""


@dataclass(frozen=True)
class Citation:
    """A parsed citation: a model key, or a file with an inclusive line range."""

    key: str | None = None
    path: str | None = None
    start: int = 0
    end: int = 0

    def matches(self, other: Citation) -> bool:
        if self.key is not None or other.key is not None:
            return self.key == other.key
        return self.path == other.path and self.start <= other.end and other.start <= self.end


def parse_citation(text: str) -> Citation:
    """`model:<key>` (or a bare key) or `path:line` / `path:start-end`."""
    raw = text.strip()
    if raw.startswith("model:"):
        return Citation(key=raw.removeprefix("model:").strip())
    match = _FILE_LINE.match(raw)
    if match is None:
        return Citation(key=raw)
    path = match.group("path").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    return Citation(path=path, start=start, end=end)


def fact_pattern(alternative: str) -> re.Pattern[str]:
    """The pattern one fact alternative is searched with (case-insensitive).

    `re:<regex>` is the regex itself. Any other text is matched literally, with a word
    boundary on each side that starts or ends with a word character: `0x2` matches
    "offset 0x2," but not "0x20", and `[7:0]` matches "wdata[7:0]".
    """
    if alternative.startswith(REGEX_PREFIX):
        return re.compile(alternative.removeprefix(REGEX_PREFIX), re.IGNORECASE)
    left = r"(?<!\w)" if _WORD_CHAR.match(alternative[0]) else ""
    right = r"(?!\w)" if _WORD_CHAR.match(alternative[-1]) else ""
    return re.compile(left + re.escape(alternative) + right, re.IGNORECASE)


def states_fact(text: str, alternatives: tuple[str, ...]) -> bool:
    """Whether `text` states the fact: one of its alternatives is found in it."""
    return any(fact_pattern(a).search(text) for a in alternatives)


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    expected: tuple[str, ...] = ()
    facts: tuple[tuple[str, ...], ...] = ()
    unknown: bool = False
    reference: str = ""


@dataclass
class QuestionGrade:
    id: str
    unknown_expected: bool
    passed: bool
    invented: bool = False
    matched: list[str] = field(default_factory=list)
    missing_facts: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class GradeReport:
    grades: list[QuestionGrade]
    answerable: int
    correct: int
    unanswerable: int
    said_unknown: int
    invented: int

    @property
    def correct_rate(self) -> float:
        return self.correct / self.answerable if self.answerable else 1.0

    @property
    def passed(self) -> bool:
        return self.correct_rate >= MIN_CORRECT_RATE and self.invented <= MAX_INVENTED

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "correct_citation_rate": round(self.correct_rate, 4),
            "answerable": self.answerable,
            "correct": self.correct,
            "unanswerable": self.unanswerable,
            "said_unknown": self.said_unknown,
            "invented": self.invented,
            "thresholds": {"min_correct_rate": MIN_CORRECT_RATE, "max_invented": MAX_INVENTED},
            "questions": [asdict(g) for g in self.grades],
        }


def load_questions(path: Path) -> list[Question]:
    """The questions of an eval YAML file (see evals/ask/tinysoc.yml)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise GradeError(f"{path}: expected a mapping with a 'questions' list")
    questions: list[Question] = []
    seen: set[str] = set()
    for item in data["questions"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise GradeError(f"{path}: every question needs an 'id'")
        qid = item["id"]
        if qid in seen:
            raise GradeError(f"{path}: duplicate question id {qid!r}")
        seen.add(qid)
        unknown = bool(item.get("unknown", False))
        expected = tuple(str(e["cite"]) for e in item.get("expected") or ())
        if unknown == bool(expected):
            raise GradeError(f"{path}: {qid} needs either 'expected' or 'unknown: true'")
        facts = tuple(
            tuple(str(a) for a in (f if isinstance(f, list) else [f]))
            for f in item.get("facts") or ()
        )
        for alternatives in facts:
            for alternative in alternatives:
                if not alternative.removeprefix(REGEX_PREFIX):
                    raise GradeError(f"{path}: {qid} has an empty fact")
                try:
                    fact_pattern(alternative)
                except re.error as exc:
                    raise GradeError(
                        f"{path}: {qid}: bad fact regex {alternative!r}: {exc}"
                    ) from exc
        questions.append(
            Question(
                id=qid,
                question=str(item.get("question", "")),
                expected=expected,
                facts=facts,
                unknown=unknown,
                reference=str(item.get("reference", "")),
            )
        )
    return questions


def load_answers(path: Path) -> dict[str, dict[str, Any]]:
    """The answers of an ANSWERS.jsonl file, by question id (a later line wins)."""
    answers: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GradeError(f"{path}:{number}: not JSON: {exc.msg}") from exc
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise GradeError(f"{path}:{number}: an answer needs an 'id'")
        answers[item["id"]] = item
    return answers


def grade_one(question: Question, answer: dict[str, Any] | None) -> QuestionGrade:
    if answer is None:
        return QuestionGrade(
            question.id, question.unknown, passed=False, invented=False, note="no answer"
        )
    unknown = answer.get("unknown") is True
    checked = answer.get("checked", True) is True
    text = str(answer.get("answer", ""))
    raw_citations = answer.get("citations") or []
    citations = [parse_citation(str(c)) for c in raw_citations]

    if question.unknown:
        invented = not unknown
        note = "answered a question tinysoc has no source for" if invented else ""
        if not checked:
            note = ", ".join(n for n in (note, "not checked by ask_check") if n)
        return QuestionGrade(
            question.id,
            True,
            passed=unknown and checked,
            invented=invented,
            note=note,
        )

    expected = [parse_citation(e) for e in question.expected]
    matched = [
        str(raw)
        for raw, cited in zip(raw_citations, citations, strict=True)
        if any(cited.matches(e) for e in expected)
    ]
    missing = [" | ".join(alts) for alts in question.facts if not states_fact(text, alts)]
    notes = []
    if unknown:
        notes.append("said unknown")
    if not checked:
        notes.append("not checked by ask_check")
    if not matched:
        notes.append("no expected citation")
    if missing:
        notes.append("missing facts")
    return QuestionGrade(
        question.id,
        False,
        passed=not notes,
        matched=matched,
        missing_facts=missing,
        note=", ".join(notes),
    )


def grade(questions: list[Question], answers: dict[str, dict[str, Any]]) -> GradeReport:
    grades = [grade_one(q, answers.get(q.id)) for q in questions]
    answerable = [g for g in grades if not g.unknown_expected]
    unanswerable = [g for g in grades if g.unknown_expected]
    return GradeReport(
        grades=grades,
        answerable=len(answerable),
        correct=sum(g.passed for g in answerable),
        unanswerable=len(unanswerable),
        said_unknown=sum(g.passed for g in unanswerable),
        invented=sum(g.invented for g in unanswerable),
    )


def format_report(report: GradeReport) -> str:
    lines = []
    for g in report.grades:
        verdict = "PASS" if g.passed else "FAIL"
        what = ", ".join(g.matched) if g.matched else ("unknown" if g.unknown_expected else "-")
        note = f"  ({g.note})" if g.note else ""
        lines.append(f"{g.id:5} {verdict}  {what}{note}")
    lines.append(
        f"answerable:   {report.correct}/{report.answerable} with a correct citation "
        f"({report.correct_rate:.0%}; needs >= {MIN_CORRECT_RATE:.0%})"
    )
    lines.append(
        f"unanswerable: {report.said_unknown}/{report.unanswerable} said unknown; "
        f"invented answers: {report.invented} (needs <= {MAX_INVENTED})"
    )
    lines.append(f"verdict: {'PASS' if report.passed else 'FAIL'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade /ask answers against the eval questions.")
    parser.add_argument("answers", type=Path, help="ANSWERS.jsonl, one answer per line")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    try:
        report = grade(load_questions(args.questions), load_answers(args.answers))
    except (GradeError, OSError, yaml.YAMLError) as exc:
        print(f"grade.py: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_json(), indent=2) if args.json else format_report(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())

"""Deterministic grader for the `/triage` evals (task M1-14; reused by M1-17).

    python evals/triage/grade.py ANSWERS.jsonl [--faults evals/triage/faults.yml] [--json]

ANSWERS.jsonl has one JSON object per line, one per sample log:

    {"id": "log-01", "label": "rtl" | null, "backend": "rule" | "small" | "large" | null,
     "status": "decided", "confidence": 0.9, "low_confidence": false}

The true label of each sample is the one in `faults.yml`: it was fixed by the fault that
was injected to make the log, never by a model. A sample passes when the answer's label
is that label; a missing answer, or one with no label, fails. The report gives a verdict
per sample, the accuracy, the confusion matrix (true label x answered label) and the
accuracy by `decide()` backend (rule, small, large; `none` for no label).

The run passes when the accuracy is at least 80 %. Exit code: 0 pass, 1 fail, 2 bad input.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_FAULTS = Path(__file__).resolve().parent / "faults.yml"
LABELS = ("infra", "rtl", "tb", "spec")
BACKENDS = ("rule", "small", "large", "none")
MIN_ACCURACY = 0.8
NONE = "none"


class GradeError(ValueError):
    """Bad faults or answers input."""


@dataclass(frozen=True)
class Sample:
    id: str
    label: str
    description: str = ""


@dataclass
class SampleGrade:
    id: str
    expected: str
    answered: str
    backend: str
    passed: bool
    low_confidence: bool = False
    note: str = ""


@dataclass
class GradeReport:
    grades: list[SampleGrade]

    @property
    def total(self) -> int:
        return len(self.grades)

    @property
    def correct(self) -> int:
        return sum(g.passed for g in self.grades)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def passed(self) -> bool:
        return self.total > 0 and self.accuracy >= MIN_ACCURACY

    def confusion(self) -> dict[str, dict[str, int]]:
        """`matrix[true][answered]`, answered including `none`."""
        matrix = {t: dict.fromkeys((*LABELS, NONE), 0) for t in LABELS}
        for g in self.grades:
            matrix[g.expected][g.answered] += 1
        return matrix

    def by_backend(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for backend in BACKENDS:
            mine = [g for g in self.grades if g.backend == backend]
            correct = sum(g.passed for g in mine)
            out[backend] = {
                "answered": len(mine),
                "correct": correct,
                "accuracy": round(correct / len(mine), 4) if mine else None,
            }
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "accuracy": round(self.accuracy, 4),
            "correct": self.correct,
            "total": self.total,
            "threshold": MIN_ACCURACY,
            "confusion": self.confusion(),
            "by_backend": self.by_backend(),
            "low_confidence": sum(g.low_confidence for g in self.grades),
            "samples": [asdict(g) for g in self.grades],
        }


def load_samples(path: Path = DEFAULT_FAULTS) -> list[Sample]:
    """The samples' ids and true labels from faults.yml."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
        raise GradeError(f"{path}: expected a mapping with a 'samples' list")
    samples: list[Sample] = []
    seen: set[str] = set()
    for item in data["samples"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise GradeError(f"{path}: every sample needs an 'id'")
        sid = item["id"]
        if sid in seen:
            raise GradeError(f"{path}: duplicate sample id {sid!r}")
        seen.add(sid)
        if item.get("label") not in LABELS:
            raise GradeError(f"{path}: {sid} has label {item.get('label')!r}, not one of {LABELS}")
        samples.append(Sample(sid, item["label"], str(item.get("description", ""))))
    return samples


def load_answers(path: Path) -> dict[str, dict[str, Any]]:
    """The answers of an ANSWERS.jsonl file, by sample id (a later line wins)."""
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


def grade_one(sample: Sample, answer: dict[str, Any] | None) -> SampleGrade:
    if answer is None:
        return SampleGrade(sample.id, sample.label, NONE, NONE, passed=False, note="no answer")
    label = answer.get("label")
    answered = label if isinstance(label, str) and label in LABELS else NONE
    backend = answer.get("backend")
    backend = backend if isinstance(backend, str) and backend in BACKENDS else NONE
    if answered == NONE:
        backend = NONE
    notes = []
    if answered == NONE:
        notes.append(f"no label (status {answer.get('status', '?')})")
    elif answered != sample.label:
        notes.append(f"expected {sample.label}")
    return SampleGrade(
        sample.id,
        sample.label,
        answered,
        backend,
        passed=answered == sample.label,
        low_confidence=answer.get("low_confidence") is True,
        note=", ".join(notes),
    )


def grade(samples: list[Sample], answers: dict[str, dict[str, Any]]) -> GradeReport:
    return GradeReport([grade_one(s, answers.get(s.id)) for s in samples])


def format_report(report: GradeReport) -> str:
    lines = []
    for g in report.grades:
        verdict = "PASS" if g.passed else "FAIL"
        low = " low-confidence" if g.low_confidence else ""
        note = f"  ({g.note})" if g.note else ""
        lines.append(f"{g.id:7} {verdict}  {g.answered:5} by {g.backend}{low}{note}")
    lines.append("confusion (rows: true label, columns: answered):")
    header = "         " + " ".join(f"{c:>6}" for c in (*LABELS, NONE))
    lines.append(header)
    for true, row in report.confusion().items():
        lines.append(f"  {true:6} " + " ".join(f"{row[c]:>6}" for c in (*LABELS, NONE)))
    lines.append("by backend:")
    for backend, stats in report.by_backend().items():
        if stats["answered"]:
            lines.append(
                f"  {backend:6} {stats['correct']}/{stats['answered']} correct "
                f"({stats['accuracy']:.0%})"
            )
    lines.append(
        f"accuracy: {report.correct}/{report.total} ({report.accuracy:.0%}; "
        f"needs >= {MIN_ACCURACY:.0%})"
    )
    lines.append(f"verdict: {'PASS' if report.passed else 'FAIL'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade /triage answers against faults.yml.")
    parser.add_argument("answers", type=Path, help="ANSWERS.jsonl, one answer per line")
    parser.add_argument("--faults", type=Path, default=DEFAULT_FAULTS)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    try:
        report = grade(load_samples(args.faults), load_answers(args.answers))
    except (GradeError, OSError, yaml.YAMLError) as exc:
        print(f"grade.py: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_json(), indent=2) if args.json else format_report(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())

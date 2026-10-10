"""Deterministic grader for the Critic review evals (task M2-09).

    python evals/review/grade.py ANSWERS.jsonl [--defects FILE] [--json]

ANSWERS.jsonl has one JSON object per line, one per sample of `defects.yml`:

    {"id": "d-01", "review": {<ReviewReport>} | null, "accepted": true}

`review` is the review report the engine wrote (`reports/review/<block>.json`): only a
review `submit` accepted is ever written, so `null` means no valid review.

- A **defect** sample is caught when one comment is on the planted `at.file`, within
  `LINE_SLACK` lines of `at.line`, and its `category` is the defect's `class` (or one of
  its `also`) or its `req_id` is the defect's `req`. No review: missed.
- A **clean** sample is a false alarm when its review has a `blocker` or `major` comment.
  No review: not a false alarm, but the sample does not pass either.

The report gives each sample's verdict, the recall (defects caught / defects), recall by
class, the false alarms on clean samples, and a precision proxy: of all blocker/major
comments, the share that point at the planted defect (a proxy: a comment that does not
may still be a true, unplanted finding). The run passes when the recall is at least 70 %
and at most one clean sample is a false alarm. Exit code: 0 pass, 1 fail, 2 bad input.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DEFECTS = Path(__file__).resolve().parent / "defects.yml"
LINE_SLACK = 3
MIN_RECALL = 0.7
MAX_FALSE_ALARMS = 1
BLOCKING = ("blocker", "major")
RULE = "digital-rtl/review"


class GradeError(ValueError):
    """Bad defects or answers input."""


@dataclass(frozen=True)
class Edit:
    file: str
    find: str
    replace: str


@dataclass(frozen=True)
class Sample:
    id: str
    block: str
    edits: tuple[Edit, ...]
    description: str = ""
    clean: bool = False
    cls: str = ""
    also: tuple[str, ...] = ()
    req: str | None = None
    spec: str = ""
    spec_has: str = ""
    at_file: str = ""
    at_line: int = 0
    at_has: str = ""

    @property
    def categories(self) -> tuple[str, ...]:
        return (self.cls, *self.also)

    @property
    def target(self) -> str:
        """The build target whose review sees this sample."""
        return f"{RULE}[block={self.block}]"


@dataclass
class SampleGrade:
    id: str
    kind: str
    cls: str
    passed: bool
    reviewed: bool
    comments: int = 0
    blocking: int = 0
    matched_blocking: int = 0
    caught: bool | None = None
    false_alarm: bool | None = None
    note: str = ""


@dataclass
class GradeReport:
    grades: list[SampleGrade] = field(default_factory=list)

    @property
    def defects(self) -> list[SampleGrade]:
        return [g for g in self.grades if g.kind == "defect"]

    @property
    def cleans(self) -> list[SampleGrade]:
        return [g for g in self.grades if g.kind == "clean"]

    @property
    def caught(self) -> int:
        return sum(bool(g.caught) for g in self.defects)

    @property
    def recall(self) -> float:
        return self.caught / len(self.defects) if self.defects else 0.0

    @property
    def false_alarms(self) -> int:
        return sum(bool(g.false_alarm) for g in self.cleans)

    @property
    def false_alarm_rate(self) -> float:
        return self.false_alarms / len(self.cleans) if self.cleans else 0.0

    @property
    def precision_proxy(self) -> float | None:
        total = sum(g.blocking for g in self.grades)
        return sum(g.matched_blocking for g in self.grades) / total if total else None

    @property
    def by_class(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for g in self.defects:
            row = out.setdefault(g.cls, {"total": 0, "caught": 0})
            row["total"] += 1
            row["caught"] += int(bool(g.caught))
        return dict(sorted(out.items()))

    @property
    def passed(self) -> bool:
        return (
            bool(self.defects)
            and self.recall >= MIN_RECALL
            and self.false_alarms <= MAX_FALSE_ALARMS
        )

    def to_json(self) -> dict[str, Any]:
        proxy = self.precision_proxy
        return {
            "passed": self.passed,
            "defects": len(self.defects),
            "caught": self.caught,
            "recall": round(self.recall, 4),
            "clean": len(self.cleans),
            "false_alarms": self.false_alarms,
            "false_alarm_rate": round(self.false_alarm_rate, 4),
            "precision_proxy": None if proxy is None else round(proxy, 4),
            "by_class": self.by_class,
            "reviewed": sum(g.reviewed for g in self.grades),
            "thresholds": {
                "min_recall": MIN_RECALL,
                "max_false_alarms": MAX_FALSE_ALARMS,
                "line_slack": LINE_SLACK,
            },
            "samples": [asdict(g) for g in self.grades],
        }


# --- loading and applying ---------------------------------------------------------------


def _edit(raw: Any, where: str) -> Edit:
    if not isinstance(raw, dict) or set(raw) != {"file", "find", "replace"}:
        raise GradeError(f"{where}: an edit is {{file, find, replace}}")
    return Edit(str(raw["file"]), str(raw["find"]), str(raw["replace"]))


def load_samples(path: Path = DEFAULT_DEFECTS) -> list[Sample]:
    """The samples of a defects file, in file order."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GradeError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
        raise GradeError(f"{path}: expected a mapping with a `samples` list")
    samples: list[Sample] = []
    seen: set[str] = set()
    for raw in data["samples"]:
        if not isinstance(raw, dict) or "id" not in raw:
            raise GradeError(f"{path}: every sample needs an id")
        sid = str(raw["id"])
        if sid in seen:
            raise GradeError(f"{path}: duplicate sample id {sid!r}")
        seen.add(sid)
        edits = tuple(_edit(e, f"{path}:{sid}") for e in raw.get("edits") or [])
        if not edits:
            raise GradeError(f"{path}:{sid}: a sample needs at least one edit")
        common = {
            "id": sid,
            "block": str(raw["block"]),
            "edits": edits,
            "description": str(raw.get("description", "")),
        }
        if raw.get("clean"):
            samples.append(Sample(**common, clean=True))
            continue
        at = raw.get("at") or {}
        try:
            samples.append(
                Sample(
                    **common,
                    cls=str(raw["class"]),
                    also=tuple(str(c) for c in raw.get("also") or ()),
                    req=raw.get("req"),
                    spec=str(raw["spec"]),
                    spec_has=str(raw.get("spec_has", "")),
                    at_file=str(at["file"]),
                    at_line=int(at["line"]),
                    at_has=str(at.get("has", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GradeError(f"{path}:{sid}: a defect needs class, spec, at.file, at.line") from exc
    return samples


def apply_edits(sample: Sample, root: Path) -> None:
    """Make the sample's change in the project at `root` (each `find` must occur once)."""
    for edit in sample.edits:
        path = root / edit.file
        text = path.read_text(encoding="utf-8")
        count = text.count(edit.find)
        if count != 1:
            raise GradeError(f"{sample.id}: {edit.find!r} occurs {count} times in {edit.file}")
        path.write_text(text.replace(edit.find, edit.replace), encoding="utf-8")


def load_answers(path: Path) -> dict[str, dict[str, Any]]:
    answers: dict[str, dict[str, Any]] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            answer = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GradeError(f"{path}:{n}: not JSON: {exc}") from exc
        if not isinstance(answer, dict) or "id" not in answer:
            raise GradeError(f"{path}:{n}: an answer is an object with an id")
        answers[str(answer["id"])] = answer
    return answers


# --- grading ----------------------------------------------------------------------------


def _comments(answer: Mapping[str, Any] | None) -> list[dict[str, Any]] | None:
    review = (answer or {}).get("review")
    if not isinstance(review, dict):
        return None
    return [c for c in review.get("comments") or [] if isinstance(c, dict)]


def _near(sample: Sample, comment: Mapping[str, Any]) -> bool:
    try:
        line = int(comment.get("line", -10_000))
    except (TypeError, ValueError):
        return False
    return comment.get("file") == sample.at_file and abs(line - sample.at_line) <= LINE_SLACK


def matches(sample: Sample, comment: Mapping[str, Any]) -> bool:
    """Whether `comment` points at the sample's planted defect."""
    if sample.clean or not _near(sample, comment):
        return False
    return comment.get("category") in sample.categories or (
        sample.req is not None and comment.get("req_id") == sample.req
    )


def grade_one(sample: Sample, answer: Mapping[str, Any] | None) -> SampleGrade:
    comments = _comments(answer)
    kind = "clean" if sample.clean else "defect"
    if comments is None:
        return SampleGrade(
            sample.id,
            kind,
            sample.cls,
            passed=False,
            reviewed=False,
            caught=None if sample.clean else False,
            false_alarm=None if not sample.clean else False,
            note="no review",
        )
    blocking = [c for c in comments if c.get("severity") in BLOCKING]
    matched = [c for c in blocking if matches(sample, c)]
    if sample.clean:
        alarm = bool(blocking)
        note = f"false alarm: {', '.join(str(c.get('id')) for c in blocking)}" if alarm else ""
        return SampleGrade(
            sample.id,
            kind,
            "",
            passed=not alarm,
            reviewed=True,
            comments=len(comments),
            blocking=len(blocking),
            false_alarm=alarm,
            note=note,
        )
    caught = any(matches(sample, c) for c in comments)
    note = "" if caught else f"missed {sample.cls} at {sample.at_file}:{sample.at_line}"
    return SampleGrade(
        sample.id,
        kind,
        sample.cls,
        passed=caught,
        reviewed=True,
        comments=len(comments),
        blocking=len(blocking),
        matched_blocking=len(matched),
        caught=caught,
        note=note,
    )


def grade(samples: Iterable[Sample], answers: Mapping[str, Mapping[str, Any]]) -> GradeReport:
    return GradeReport([grade_one(s, answers.get(s.id)) for s in samples])


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("answers", type=Path)
    parser.add_argument("--defects", type=Path, default=DEFAULT_DEFECTS)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    try:
        report = grade(load_samples(args.defects), load_answers(args.answers))
    except GradeError as exc:
        print(f"grade.py: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.to_json(), indent=2))
    else:
        for g in report.grades:
            print(f"{g.id:6} {'PASS' if g.passed else 'FAIL'}  {g.kind:6} {g.cls:13} {g.note}")
        print(
            f"recall {report.caught}/{len(report.defects)} ({_pct(report.recall)}; needs >= "
            f"{_pct(MIN_RECALL)}); false alarms {report.false_alarms}/{len(report.cleans)} "
            f"(needs <= {MAX_FALSE_ALARMS}); precision proxy {_pct(report.precision_proxy)}"
        )
        print("PASS" if report.passed else "FAIL")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())

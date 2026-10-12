"""The deterministic grader of the `rtl` and `tb` suites: the author's work against the
oracle, the RTL people wrote.

`load_samples(path, phase)` gives one `Sample` per problem of `problems.yml` for a phase
(`rtl`: the RTL Author writes the module; `tb`: the testbench Author writes the test).
An answer (one per sample, written by either runtime) is::

    {"id", "phase", "status", "accepted", "text", "attempts": [{accepted, label,
     failed_checks}, ...], "checks": [...], "tries": k, "cost_usd", "usage", "wall_s"}

`grade_one(sample, answer)`:

- `rtl`: the module (accepted or not, the last one written) against the oracle by the
  differential simulation (`diffsim`): matching = identical outputs on every cycle of
  every seed, with the fraction of cycles and seeds that match; plus `static.rtl_static`;
- `tb`: the test against the oracle and its mutants (`tbsim`): matching = at least one
  test and every test passes on the oracle (soundness 1); plus the mutation score;
- both: the metrics of `metrics.ProblemMetrics` (pass@1, pass@k, repair turns, first
  rejection, cost, tokens, wall time). A sample passes at pass@k.

`grade(samples, answers)` is the suite's report and verdict, against `THRESHOLDS`
(proposed in evals/README.md before any real run, not tuned on one):

- `rtl`: pass@k >= 50 %;
- `tb`: pass@k >= 50 % and a mean mutation score >= 50 % (a test that is not sound
  scores 0).

Simulations are cached by (phase, problem, text) for the life of the process: the
scorer and the final report grade the same answers.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import diffsim, metrics, static, tbsim
from .problems import PROBLEMS, Problem, ProblemSet, load_problems

Phase = Literal["rtl", "tb"]

THRESHOLDS: dict[str, dict[str, float]] = {
    "rtl": {"min_pass_at_k": 0.5},
    "tb": {"min_pass_at_k": 0.5, "min_mutation_score": 0.5},
}

_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}


@dataclass(frozen=True)
class Sample:
    id: str
    phase: Phase
    problem: Problem
    problems: ProblemSet = field(repr=False, compare=False)

    @property
    def target(self) -> str:
        return self.problem.rtl_target if self.phase == "rtl" else self.problem.tb_target

    @property
    def output(self) -> str:
        return self.problem.rtl_output if self.phase == "rtl" else self.problem.tb_output


@dataclass
class Grade:
    id: str
    phase: str
    passed: bool
    pass_at_1: bool = False
    pass_at_k: bool = False
    accepted: bool = False
    matches_oracle: bool = False
    repair_turns: int = 0
    k: int = 0
    first_rejection: str | None = None
    status: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    diff: dict[str, Any] | None = None
    tb: dict[str, Any] | None = None
    static: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    tokens: int = 0
    usage: dict[str, dict[str, float]] = field(default_factory=dict)
    wall_s: float = 0.0
    estimate: dict[str, Any] | None = None
    note: str = ""


@dataclass
class Report:
    phase: Phase
    grades: list[Grade]

    def _metrics(self) -> dict[str, Any]:
        return metrics.suite_metrics([_problem_metrics(g) for g in self.grades])

    @property
    def mutation_score_mean(self) -> float | None:
        if self.phase != "tb" or not self.grades:
            return None
        scores = [
            float((g.tb or {}).get("mutation_score", 0.0)) if g.matches_oracle else 0.0
            for g in self.grades
        ]
        return round(sum(scores) / len(scores), 4)

    @property
    def passed(self) -> bool:
        th = THRESHOLDS[self.phase]
        m = self._metrics()
        if (m["pass_at_k"] or 0.0) < th["min_pass_at_k"]:
            return False
        if self.phase == "tb":
            return (self.mutation_score_mean or 0.0) >= th["min_mutation_score"]
        return True

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "phase": self.phase,
            **self._metrics(),
            "thresholds": THRESHOLDS[self.phase],
            "passed": self.passed,
            "samples": [_grade_json(g) for g in self.grades],
        }
        estimates = [g.estimate for g in self.grades if g.estimate]
        if estimates:
            out["estimate"] = metrics.estimate_usd(estimates)
        if self.phase == "rtl":
            diffs = [g.diff for g in self.grades if g.diff and g.diff.get("seeds")]
            out["match_fraction_mean"] = (
                round(sum(float(d["match_fraction"]) for d in diffs) / len(diffs), 4)
                if diffs
                else None
            )
        else:
            out["mutation_score_mean"] = self.mutation_score_mean
            sound = [float((g.tb or {}).get("soundness", 0.0)) for g in self.grades]
            out["soundness_mean"] = round(sum(sound) / len(sound), 4) if sound else None
        return out


def _grade_json(grade: Grade) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(grade)


def load_samples(path: Path = PROBLEMS, phase: Phase = "rtl") -> list[Sample]:
    problems = load_problems(path)
    return [Sample(p.id, phase, p, problems) for p in problems.problems]


def _problem_metrics(grade: Grade) -> metrics.ProblemMetrics:
    attempts = tuple(metrics.Attempt.from_json(a) for a in grade.attempts)
    return metrics.ProblemMetrics(
        id=grade.id,
        tries_allowed=grade.k,
        attempts=attempts,
        matches_oracle=grade.matches_oracle,
        cost_usd=grade.cost_usd,
        usage=grade.usage,
        wall_s=grade.wall_s,
    )


def _key(sample: Sample, text: str) -> tuple[str, str, str]:
    return (sample.phase, sample.id, hashlib.sha256(text.encode("utf-8")).hexdigest())


def _diff(sample: Sample, text: str) -> dict[str, Any]:
    key = _key(sample, text)
    if key not in _CACHE:
        tools = diffsim.Toolchain()
        if not tools.available():
            return {"status": "error", "passed": False, "note": "verilator or make not on PATH"}
        with tempfile.TemporaryDirectory(prefix=f"cg-eval-diff-{sample.id}-") as tmp:
            result = diffsim.differential(sample.problem, {"module": text}, Path(tmp), tools=tools)
        _CACHE[key] = result["module"].to_json()
    return _CACHE[key]


def _tb(sample: Sample, text: str) -> dict[str, Any]:
    key = _key(sample, text)
    if key not in _CACHE:
        with tempfile.TemporaryDirectory(prefix=f"cg-eval-tb-{sample.id}-") as tmp:
            _CACHE[key] = tbsim.run_tb(sample.problem, text, Path(tmp) / "work").to_json()
    return _CACHE[key]


def grade_one(sample: Sample, answer: Mapping[str, Any] | None) -> Grade:
    """The grade of one sample's answer (None: the sample was not run)."""
    if answer is None:
        return Grade(sample.id, sample.phase, False, note="no answer (not run)")
    attempts = [dict(a) for a in answer.get("attempts") or []]
    text = answer.get("text")
    grade = Grade(
        sample.id,
        sample.phase,
        False,
        accepted=bool(answer.get("accepted")),
        k=int(answer.get("tries") or 0),
        status=answer.get("status"),
        attempts=attempts,
        checks=static.check_summary(answer.get("checks")),
        cost_usd=float(answer.get("cost_usd") or 0.0),
        usage={k: dict(v) for k, v in (answer.get("usage") or {}).items()},
        wall_s=float(answer.get("wall_s") or 0.0),
        estimate=dict(answer["estimate"]) if answer.get("estimate") else None,
        note=str(answer.get("note") or ""),
    )
    if isinstance(text, str) and text.strip():
        if sample.phase == "rtl":
            grade.diff = _diff(sample, text)
            grade.matches_oracle = bool(grade.diff.get("passed"))
            grade.static = static.rtl_static(sample.problem, text, sample.problems)
        else:
            grade.tb = _tb(sample, text)
            grade.matches_oracle = grade.tb["tests"] > 0 and grade.tb["soundness"] == 1.0
    elif not grade.note:
        grade.note = "nothing was written"
    pm = _problem_metrics(grade)
    grade.pass_at_1, grade.pass_at_k = pm.pass_at_1, pm.pass_at_k
    grade.repair_turns, grade.first_rejection = pm.repair_turns, pm.first_rejection
    grade.tokens = metrics.tokens_total(grade.usage)
    grade.passed = grade.pass_at_k
    if not grade.note:
        grade.note = _note(grade)
    return grade


def _note(grade: Grade) -> str:
    if grade.passed:
        return "" if grade.pass_at_1 else f"accepted after {grade.repair_turns} tries"
    if not grade.accepted:
        return f"not accepted ({grade.status}, first rejection {grade.first_rejection})"
    if grade.phase == "rtl" and grade.diff:
        first = grade.diff.get("first_mismatch") or {}
        return (
            f"accepted, but differs from the oracle ({grade.diff['status']}; "
            f"{grade.diff.get('match_fraction', 0):.0%} of cycles match"
            + (
                f"; first at seed {first['seed']} cycle {first['cycle']} {first['port']}"
                if first
                else ""
            )
            + (f"; {grade.diff['note']}" if grade.diff.get("note") else "")
            + ")"
        )
    if grade.tb:
        failing = ", ".join(grade.tb["failing_on_oracle"]) or grade.tb["note"]
        return f"accepted, but fails on the oracle: {failing}"
    return "accepted, but not graded"


def grade(samples: Sequence[Sample], answers: Mapping[str, Mapping[str, Any]]) -> Report:
    phase: Phase = samples[0].phase if samples else "rtl"
    return Report(phase, [grade_one(s, answers.get(s.id)) for s in samples])


__all__ = ["THRESHOLDS", "Grade", "Report", "Sample", "grade", "grade_one", "load_samples"]

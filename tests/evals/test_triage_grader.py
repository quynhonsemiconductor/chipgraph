"""M1-14: the `/triage` grader grades deterministically (no model)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
GRADE = REPO / "evals" / "triage" / "grade.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("triage_grade", GRADE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["triage_grade"] = module
    spec.loader.exec_module(module)
    return module


grade = _load()
SAMPLES = [
    grade.Sample("s1", "infra"),
    grade.Sample("s2", "rtl"),
    grade.Sample("s3", "tb"),
    grade.Sample("s4", "spec"),
    grade.Sample("s5", "rtl"),
]


def _answers(*rows: tuple[str, str | None, str | None]) -> dict[str, dict[str, object]]:
    return {sid: {"id": sid, "label": label, "backend": backend} for sid, label, backend in rows}


def test_all_correct_passes() -> None:
    answers = _answers(
        ("s1", "infra", "rule"),
        ("s2", "rtl", "rule"),
        ("s3", "tb", "small"),
        ("s4", "spec", "rule"),
        ("s5", "rtl", "large"),
    )
    report = grade.grade(SAMPLES, answers)
    assert report.passed and report.accuracy == 1.0
    stats = report.by_backend()
    assert (stats["rule"]["answered"], stats["small"]["answered"]) == (3, 1)
    assert stats["large"]["accuracy"] == 1.0 and stats["none"]["accuracy"] is None


def test_eighty_percent_passes_and_less_fails() -> None:
    four = _answers(
        ("s1", "infra", "rule"),
        ("s2", "rtl", "rule"),
        ("s3", "rtl", "small"),
        ("s4", "spec", "rule"),
        ("s5", "rtl", "large"),
    )
    assert grade.grade(SAMPLES, four).passed
    three = {**four, "s5": {"id": "s5", "label": "tb", "backend": "large"}}
    report = grade.grade(SAMPLES, three)
    assert not report.passed and report.accuracy == 0.6


def test_missing_and_unlabelled_answers_fail() -> None:
    answers = _answers(("s1", None, None), ("s2", "nonsense", "small"))
    report = grade.grade(SAMPLES, answers)
    assert report.correct == 0 and not report.passed
    notes = {g.id: (g.answered, g.backend, g.note) for g in report.grades}
    assert notes["s1"][:2] == ("none", "none") and "no label" in notes["s1"][2]
    assert notes["s2"][:2] == ("none", "none")
    assert notes["s3"] == ("none", "none", "no answer")


def test_the_confusion_matrix() -> None:
    answers = _answers(
        ("s1", "infra", "rule"),
        ("s2", "tb", "small"),
        ("s3", "tb", "small"),
        ("s4", "rtl", "large"),
    )
    matrix = grade.grade(SAMPLES, answers).confusion()
    assert matrix["infra"]["infra"] == 1
    assert matrix["rtl"]["tb"] == 1 and matrix["rtl"]["none"] == 1
    assert matrix["tb"]["tb"] == 1 and matrix["spec"]["rtl"] == 1
    assert sum(sum(row.values()) for row in matrix.values()) == len(SAMPLES)


def test_the_real_faults_file_loads() -> None:
    samples = grade.load_samples()
    assert len(samples) >= 15 and all(s.label in grade.LABELS for s in samples)


def test_main_exit_codes_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    faults = tmp_path / "faults.yml"
    faults.write_text(
        "samples:\n  - {id: a, label: rtl}\n  - {id: b, label: infra}\n"
        "  - {id: c, label: tb}\n  - {id: d, label: spec}\n  - {id: e, label: rtl}\n"
    )
    answers = tmp_path / "answers.jsonl"
    rows = [("a", "rtl"), ("b", "infra"), ("c", "tb"), ("d", "spec"), ("e", "tb")]
    answers.write_text(
        "".join(
            json.dumps({"id": i, "label": label, "backend": "rule"}) + "\n" for i, label in rows
        )
    )
    assert grade.main([str(answers), "--faults", str(faults)]) == 0
    out = capsys.readouterr().out
    assert "accuracy: 4/5 (80%" in out and "verdict: PASS" in out and "confusion" in out

    assert grade.main([str(answers), "--faults", str(faults), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["correct"] == 4 and payload["confusion"]["rtl"]["tb"] == 1

    answers.write_text(json.dumps({"id": "a", "label": "tb"}) + "\n")
    assert grade.main([str(answers), "--faults", str(faults)]) == 1

    answers.write_text("not json\n")
    assert grade.main([str(answers), "--faults", str(faults)]) == 2
    faults.write_text("samples:\n  - {id: a, label: hardware}\n")
    answers.write_text("")
    assert grade.main([str(answers), "--faults", str(faults)]) == 2

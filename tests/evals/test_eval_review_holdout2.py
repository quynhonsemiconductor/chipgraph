"""The second Critic review set (`review-holdout2`) is true to tinysoc and new against the first.

`evals/review/defects2.yml` was written by an author who never read the Critic's prompt,
role, skill, context builder or review tests, to measure the Critic honestly; it is graded
only and frozen (its sha256 is pinned here). These tests check its shape (ids, classes,
counts), that every change still applies to `examples/tinysoc` and lands on its line, that
every spec line it cites says what it claims, that no defect repeats one of `defects.yml`,
that `grade.py` grades it with the first set's thresholds, and that the fake runtime runs
the suite end to end.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
REVIEW = REPO / "evals" / "review"
DEFECTS = REVIEW / "defects.yml"
DEFECTS2 = REVIEW / "defects2.yml"
HARNESS = REPO / "evals" / "harness"
TINYSOC = REPO / "examples" / "tinysoc"
SHA256 = "012d18797b46e1fe24337aa36d40b58683241c1cad55c6df58c575298e939fda"
"""The frozen set: change `defects2.yml` and it is a new set (new ids, new hash)."""


def _load_harness() -> ModuleType:
    if "chipgraph_evals" in sys.modules:
        return sys.modules["chipgraph_evals"]
    spec = importlib.util.spec_from_file_location(
        "chipgraph_evals", HARNESS / "__init__.py", submodule_search_locations=[str(HARNESS)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["chipgraph_evals"] = module
    spec.loader.exec_module(module)
    return module


evals = _load_harness()
SUITE = evals.SUITES["review-holdout2"]
grade: Any = SUITE.grader
SAMPLES = grade.load_samples(DEFECTS2)
FIRST = grade.load_samples(DEFECTS)
DEFECT_SAMPLES = [s for s in SAMPLES if not s.clean]
CLEAN_SAMPLES = [s for s in SAMPLES if s.clean]


def _project(tmp_path: Path, sample: Any) -> Path:
    root = tmp_path / sample.id
    shutil.copytree(TINYSOC, root, ignore=shutil.ignore_patterns("state", "build", "obj_dir"))
    grade.apply_edits(sample, root)
    return root


def _changed_lines(before: str, after: str) -> set[int]:
    """1-based lines of `after` that a diff touches; a pure deletion marks the line after it."""
    out: set[int] = set()
    matcher = difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines(), autojunk=False)
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        out.update(range(j1 + 1, j2 + 1) if j2 > j1 else {j1 + 1})
    return out


def _files(sample: Any) -> list[str]:
    return sorted({e.file for e in sample.edits})


# --- shape --------------------------------------------------------------------------------


def test_the_set_is_frozen() -> None:
    assert hashlib.sha256(DEFECTS2.read_bytes()).hexdigest() == SHA256


def test_ids_are_r2_nn_for_defects_and_r2_cn_for_clean_diffs() -> None:
    assert [s.id for s in DEFECT_SAMPLES] == [f"r2-{n:02d}" for n in range(1, 18)]
    assert [s.id for s in CLEAN_SAMPLES] == [f"r2-c{n}" for n in range(1, 9)]
    assert not {s.id for s in SAMPLES} & {s.id for s in FIRST}


def test_at_least_14_defects_in_8_classes_and_6_clean_diffs() -> None:
    classes = Counter(s.cls for s in DEFECT_SAMPLES)
    assert len(DEFECT_SAMPLES) == 17 >= 14
    assert len(classes) == 9 >= 8
    assert len(CLEAN_SAMPLES) == 8 >= 6
    # Only categories the first set uses: a class the Critic cannot name is never caught
    # by category.
    first_classes = {c for s in FIRST if not s.clean for c in s.categories}
    assert {c for s in DEFECT_SAMPLES for c in s.categories} <= first_classes
    assert {s.block for s in SAMPLES} == {"timer", "gpio", "top"}


def test_the_header_and_the_suite_state_the_thresholds() -> None:
    header = DEFECTS2.read_text().split("schema_version:")[0]
    flat = " ".join(header.replace("#", " ").split())
    assert "recall >= 70 % (at least 12 of the 17 defects)" in flat
    assert "at most 1 false alarm on the 8 clean samples" in flat
    assert (grade.MIN_RECALL, grade.MAX_FALSE_ALARMS, grade.LINE_SLACK) == (0.7, 1, 3)
    assert 12 / 17 >= grade.MIN_RECALL > 11 / 17
    assert "recall >= 70 % (>= 12 of 17)" in SUITE.description
    assert "<= 1 false alarm on 8 clean diffs" in SUITE.description
    source = " ".join((HARNESS / "suites.py").read_text().split())
    assert "recall >= 70 % (at least 12 of its 17 defects)" in source
    assert "at most 1 false alarm on its 8 clean diffs" in source


# --- true to tinysoc ----------------------------------------------------------------------


@pytest.mark.parametrize("sample", DEFECT_SAMPLES, ids=lambda s: s.id)
def test_every_defect_applies_and_lands_on_its_line(tmp_path: Path, sample: Any) -> None:
    root = _project(tmp_path, sample)
    assert sample.at_file in _files(sample)
    before = (TINYSOC / sample.at_file).read_text()
    after = (root / sample.at_file).read_text()
    assert before != after
    assert sample.at_has in after.splitlines()[sample.at_line - 1]
    assert sample.at_line in _changed_lines(before, after)


@pytest.mark.parametrize("sample", DEFECT_SAMPLES, ids=lambda s: s.id)
def test_every_spec_line_exists_and_says_what_it_claims(sample: Any) -> None:
    path, _, line = sample.spec.rpartition(":")
    lines = (TINYSOC / path).read_text().splitlines()
    assert 1 <= int(line) <= len(lines)
    assert sample.spec_has and sample.spec_has in lines[int(line) - 1]
    if sample.req is not None:
        spec_text = "\n".join(p.read_text() for p in (TINYSOC / "doc" / "specs").glob("*.md"))
        assert f"`{sample.req}`" in spec_text


@pytest.mark.parametrize("sample", CLEAN_SAMPLES, ids=lambda s: s.id)
def test_every_clean_diff_applies(tmp_path: Path, sample: Any) -> None:
    root = _project(tmp_path, sample)
    assert any((root / f).read_text() != (TINYSOC / f).read_text() for f in _files(sample))


def _single(sample_id: str, tmp_path: Path) -> tuple[str, str]:
    sample = next(s for s in CLEAN_SAMPLES if s.id == sample_id)
    [file] = _files(sample)
    root = _project(tmp_path, sample)
    return (TINYSOC / file).read_text(), (root / file).read_text()


def test_the_comment_and_whitespace_diffs_change_nothing_else(tmp_path: Path) -> None:
    before, after = _single("r2-c1", tmp_path)
    added = [ln for ln in after.splitlines() if ln not in before.splitlines()]
    assert added and all(ln.strip().startswith("//") for ln in added)
    assert [ln for ln in after.splitlines() if ln not in added] == before.splitlines()
    before, after = _single("r2-c2", tmp_path)
    assert before != after and before.split() == after.split()


def test_the_rename_is_consistent(tmp_path: Path) -> None:
    before, after = _single("r2-c3", tmp_path)
    assert not re.search(r"\bout_q\b", after)
    assert after.count("data_out_q") == before.count("out_q") == 5
    assert re.sub(r"\bdata_out_q\b", "out_q", after) == before


# --- new against the first set ------------------------------------------------------------


def test_no_defect_repeats_one_of_the_first_set() -> None:
    first = {(s.at_file, s.at_line, s.cls) for s in FIRST if not s.clean}
    second = [(s.at_file, s.at_line, s.cls) for s in DEFECT_SAMPLES]
    assert not first & set(second)
    assert len(set(second)) == len(second)  # nor one of its own
    # Every planted fault contradicts a spec line the first set does not use ...
    assert not {s.spec for s in FIRST if not s.clean} & {s.spec for s in DEFECT_SAMPLES}
    # ... and makes a change the first set never makes.
    first_changes = {(e.file, e.find, e.replace) for s in FIRST for e in s.edits}
    assert not first_changes & {(e.file, e.find, e.replace) for s in SAMPLES for e in s.edits}


# --- grading ------------------------------------------------------------------------------


def _hit(sample: Any, **over: Any) -> dict[str, Any]:
    comment = {
        "id": "R1",
        "severity": "major",
        "category": sample.cls,
        "file": sample.at_file,
        "line": sample.at_line,
        "req_id": None,
        **over,
    }
    return {"id": sample.id, "review": {"comments": [comment]}}


def _clean(sample: Any, *comments: dict[str, Any]) -> dict[str, Any]:
    return {"id": sample.id, "review": {"comments": list(comments)}}


def _answers(caught: int, false_alarms: int = 0) -> dict[str, dict[str, Any]]:
    answers = {s.id: _hit(s) for s in DEFECT_SAMPLES[:caught]}
    for s in DEFECT_SAMPLES[caught:]:
        answers[s.id] = _hit(s, file="rtl/elsewhere.sv")
    for n, s in enumerate(CLEAN_SAMPLES):
        alarm = {"id": "R1", "severity": "major", "category": "reset", "file": "x", "line": 1}
        answers[s.id] = _clean(s, alarm) if n < false_alarms else _clean(s)
    return answers


def test_grades_the_new_set_with_the_first_sets_thresholds() -> None:
    perfect = grade.grade(SAMPLES, _answers(17))
    assert (perfect.caught, perfect.false_alarms, perfect.passed) == (17, 0, True)
    assert set(perfect.by_class) == {s.cls for s in DEFECT_SAMPLES}
    assert grade.grade(SAMPLES, _answers(12, false_alarms=1)).passed  # 71 %, 1 of 8
    assert not grade.grade(SAMPLES, _answers(11)).passed  # 65 %
    assert not grade.grade(SAMPLES, _answers(17, false_alarms=2)).passed
    assert not grade.grade(SAMPLES, {}).passed  # no review at all


def test_a_defect_is_caught_by_its_class_an_alternative_or_its_req_near_its_line() -> None:
    with_req = next(s for s in DEFECT_SAMPLES if s.req and s.also)
    no_req = next(s for s in DEFECT_SAMPLES if s.req is None)
    one = grade.grade_one
    assert one(with_req, _hit(with_req)).caught
    assert one(with_req, _hit(with_req, category=with_req.also[0])).caught
    assert "latch" not in with_req.categories
    assert one(with_req, _hit(with_req, category="latch", req_id=with_req.req)).caught
    assert not one(with_req, _hit(with_req, category="latch")).caught
    assert one(with_req, _hit(with_req, line=with_req.at_line + 3, severity="minor")).caught
    assert not one(with_req, _hit(with_req, line=with_req.at_line + 4)).caught
    other = next(c for c in ("latch", "reset") if c not in no_req.categories)
    assert not one(no_req, _hit(no_req, category=other, req_id="REQ-TIM-001")).caught
    assert not one(no_req, _hit(no_req, file="rtl/tiny_other.sv")).caught
    assert not one(no_req, None).caught


def test_a_minor_note_on_a_clean_diff_is_not_a_false_alarm() -> None:
    sample = next(s for s in CLEAN_SAMPLES if s.id == "r2-c7")
    note = {"id": "R1", "severity": "minor", "category": "spec_mismatch", "file": "x", "line": 1}
    assert not grade.grade_one(sample, _clean(sample, note)).false_alarm
    assert grade.grade_one(sample, _clean(sample, {**note, "severity": "blocker"})).false_alarm


def test_grade_cli_selects_the_set(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    answers = tmp_path / "answers.jsonl"
    answers.write_text("".join(json.dumps(a) + "\n" for a in _answers(17).values()))
    assert grade.main([str(answers), "--set", "defects2", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["defects"], report["caught"], report["clean"]) == (17, 17, 8)
    assert grade.main([str(answers), "--defects", str(DEFECTS2)]) == 0
    capsys.readouterr()
    # The default and `--set defects` stay the first set: these answers have none of its ids.
    assert grade.main([str(answers), "--json"]) == 1
    first = json.loads(capsys.readouterr().out)
    assert (first["defects"], first["clean"], first["caught"]) == (16, 4, 0)
    assert grade.main([str(answers), "--set", "defects"]) == 1
    capsys.readouterr()
    assert grade.SETS == {"defects": DEFECTS, "defects2": DEFECTS2}


# --- the suite ----------------------------------------------------------------------------


def test_the_suite_review_holdout2_reads_defects2() -> None:
    assert (SUITE.name, SUITE.kind, SUITE.data, SUITE.logs) == (
        "review-holdout2",
        "review",
        DEFECTS2,
        None,
    )
    assert [s.id for s in SUITE.items()] == [s.id for s in SAMPLES]
    first = DEFECT_SAMPLES[0]
    assert SUITE.prompt(first) == f"digital-rtl/review[block={first.block}]"
    assert SUITE.target(first) == [f"{first.at_file}:{first.at_line}"]
    assert SUITE.target(CLEAN_SAMPLES[0]) == ["clean"]
    script = (REPO / "evals" / "run-claude-code.sh").read_text()
    assert "review-holdout2" not in script  # not in the default real-model list (cost)


def test_the_fake_suite_passes_end_to_end(tmp_path: Path) -> None:
    out = tmp_path / "review-holdout2"
    result = evals.run_suite("review-holdout2", out)
    assert (result.status, result.exit_code) == ("pass", 0)
    summary = json.loads((out / "summary.json").read_text())
    assert [i["id"] for i in summary["items"]] == [s.id for s in SAMPLES]
    answers = [json.loads(ln) for ln in (out / "answers.jsonl").read_text().splitlines()]
    assert len(answers) == 25 and all(a["accepted"] and a["review"] for a in answers)
    report = grade.grade(SAMPLES, {a["id"]: a for a in answers})
    assert (report.caught, report.false_alarms) == (17, 0)
    assert summary["metrics"]["thresholds"] == {
        "min_recall": 0.7,
        "max_false_alarms": 1,
        "line_slack": 3,
    }
    assert "review-holdout2" in (out / "summary.md").read_text()


def test_the_fake_suite_fails_on_two_false_alarms(tmp_path: Path) -> None:
    alarm = {
        "id": "R1",
        "severity": "major",
        "category": "spec_mismatch",
        "file": "rtl/tiny_top.sv",
        "line": 22,
        "claim": "fake: a false alarm",
        "evidence": "chip.yml:29 base: 0x4",
        "suggestion": "none",
        "req_id": None,
    }
    # Each on a line its clean diff changes: the read mux (r2-c4), the GPIO select (r2-c7).
    overrides = {
        sid: {"comments": [{**alarm, "line": line}], "verdict": "changes_requested"}
        for sid, line in (("r2-c4", 38), ("r2-c7", 22))
    }
    options = evals.EvalOptions(only=("r2-01", "r2-c4", "r2-c7"), fake_overrides=overrides)
    result = evals.run_suite("review-holdout2", tmp_path / "out", options)
    assert (result.status, result.exit_code) == ("fail", 1)
    lines = (tmp_path / "out" / "answers.jsonl").read_text().splitlines()
    answers = [json.loads(ln) for ln in lines]
    assert {a["id"] for a in answers} == {"r2-01", "r2-c4", "r2-c7"}
    assert all(a["accepted"] for a in answers)
    metrics = json.loads((tmp_path / "out" / "summary.json").read_text())["metrics"]
    assert (metrics["caught"], metrics["false_alarms"], metrics["passed"]) == (1, 2, False)


# --- the clean diffs lint clean (needs Verilator) -------------------------------------------


@pytest.mark.e2e
@pytest.mark.skipif(
    shutil.which("verilator") is None or shutil.which("make") is None,
    reason="needs make and verilator",
)
@pytest.mark.parametrize("sample", CLEAN_SAMPLES, ids=lambda s: s.id)
def test_every_clean_diff_lints_clean(tmp_path: Path, sample: Any) -> None:
    root = _project(tmp_path, sample)
    done = subprocess.run(
        ["make", "lint", f"BLOCK={sample.block}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr

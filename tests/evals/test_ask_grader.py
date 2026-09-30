"""M1-13: the `/ask` eval data stays true to tinysoc, and the grader grades deterministically."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from chipgraph.app.context import AppContext
from chipgraph.app.ingest import run_ingest
from chipgraph.packs.assist.ask import AskAnswer, check_answer
from chipgraph.packs.assist.ask._project import AskProject

REPO = Path(__file__).resolve().parents[2]
TINYSOC = REPO / "examples" / "tinysoc"
QUESTIONS = REPO / "evals" / "ask" / "tinysoc.yml"


def _load_grade() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ask_grade", REPO / "evals" / "ask" / "grade.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ask_grade"] = module
    spec.loader.exec_module(module)
    return module


grade = _load_grade()


@pytest.fixture(scope="module")
def tinysoc_project(tmp_path_factory: pytest.TempPathFactory) -> AskProject:
    root = tmp_path_factory.mktemp("evals") / "tinysoc"
    shutil.copytree(TINYSOC, root)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    ctx = AppContext.load(root)
    run_ingest(ctx)
    return AskProject.load(ctx)


def _raw_questions() -> list[dict[str, object]]:
    return yaml.safe_load(QUESTIONS.read_text())["questions"]


# --- the eval data --------------------------------------------------------------------


def test_twenty_questions_five_without_an_answer() -> None:
    questions = grade.load_questions(QUESTIONS)
    assert len(questions) == 20
    assert [q.id for q in questions] == [f"q{i:02d}" for i in range(1, 21)]
    assert sum(q.unknown for q in questions) == 5
    assert all(q.expected for q in questions if not q.unknown)


@pytest.mark.parametrize(
    "item", [q for q in _raw_questions() if not q.get("unknown")], ids=lambda q: str(q["id"])
)
def test_expected_citations_exist_in_tinysoc(
    item: dict[str, object], tinysoc_project: AskProject
) -> None:
    for expected in item["expected"]:  # type: ignore[attr-defined]
        cite = str(expected["cite"])
        if cite.startswith("model:"):
            assert "has" not in expected
        else:
            # A file citation must name the text it expects on those lines.
            path, _, lines = cite.rpartition(":")
            start, _, end = lines.partition("-")
            text = (TINYSOC / path).read_text().splitlines()[int(start) - 1 : int(end or start)]
            assert str(expected["has"]) in "\n".join(text), cite
        # And ask_check accepts it: the file is indexed and the line exists, or the key does.
        check = check_answer(tinysoc_project, AskAnswer(answer="x", citations=(cite,)))
        assert check.ok, (cite, check.reasons)


# --- the grader -----------------------------------------------------------------------


def _answers(tmp_path: Path, answers: list[dict[str, object]]) -> Path:
    path = tmp_path / "answers.jsonl"
    path.write_text("".join(json.dumps(a) + "\n" for a in answers))
    return path


def _perfect() -> list[dict[str, object]]:
    """A hand-written, all-correct answer set, citing a matching line or key per question."""
    out: list[dict[str, object]] = []
    for q in grade.load_questions(QUESTIONS):
        if q.unknown:
            out.append({"id": q.id, "answer": "I don't know", "citations": [], "unknown": True})
            continue
        facts = " ".join(alts[0] for alts in q.facts)
        cite = q.expected[0]
        if ":" in cite and "-" in cite.rpartition(":")[2] and not cite.startswith("model:"):
            path, _, lines = cite.rpartition(":")
            cite = f"{path}:{lines.partition('-')[2]}"  # the last line of the range
        out.append({"id": q.id, "answer": f"Answer: {facts}", "citations": [cite]})
    return out


def test_perfect_answers_pass(tmp_path: Path) -> None:
    report = grade.grade(
        grade.load_questions(QUESTIONS), grade.load_answers(_answers(tmp_path, _perfect()))
    )
    assert report.passed
    assert (report.answerable, report.correct, report.correct_rate) == (15, 15, 1.0)
    assert (report.unanswerable, report.said_unknown, report.invented) == (5, 5, 0)


def test_grader_counts_failures_by_kind(tmp_path: Path) -> None:
    answers = {a["id"]: a for a in _perfect()}
    # q01: a wrong citation; q02: a missing fact; q03: said unknown; q04: not checked.
    answers["q01"] = {"id": "q01", "answer": "0", "citations": ["chip.yml:1"]}
    answers["q02"] = {**answers["q02"], "answer": "It is at 0x2."}
    answers["q03"] = {"id": "q03", "answer": "I don't know", "citations": [], "unknown": True}
    answers["q04"] = {**answers["q04"], "checked": False}
    # q16: an invented answer to a question tinysoc cannot answer; q17 missing entirely.
    answers["q16"] = {"id": "q16", "answer": "115200", "citations": ["chip.yml:1"]}
    del answers["q17"]
    report = grade.grade(
        grade.load_questions(QUESTIONS),
        grade.load_answers(_answers(tmp_path, list(answers.values()))),
    )
    by_id = {g.id: g for g in report.grades}
    assert by_id["q01"].note == "no expected citation"
    assert by_id["q02"].missing_facts == ["rw | read/write | read-write | read and write"]
    assert "said unknown" in by_id["q03"].note
    assert "not checked" in by_id["q04"].note
    assert by_id["q16"].invented and not by_id["q16"].passed
    assert not by_id["q17"].passed and not by_id["q17"].invented
    assert (report.correct, report.said_unknown, report.invented) == (11, 3, 1)
    assert not report.passed


def test_ninety_percent_passes_and_one_invented_answer_fails(tmp_path: Path) -> None:
    questions = grade.load_questions(QUESTIONS)
    answers = {a["id"]: a for a in _perfect()}
    answers["q05"] = {"id": "q05", "answer": "8", "citations": ["model:port:tiny_top.addr"]}
    report = grade.grade(questions, grade.load_answers(_answers(tmp_path, list(answers.values()))))
    assert report.correct == 14 and report.correct_rate >= 0.9 and report.passed
    answers["q20"] = {"id": "q20", "answer": "1000 gates", "citations": ["rtl/tiny_gpio.sv:1"]}
    report = grade.grade(questions, grade.load_answers(_answers(tmp_path, list(answers.values()))))
    assert report.invented == 1 and not report.passed


def test_citation_matching() -> None:
    parse = grade.parse_citation
    assert parse("chip.yml:24-26").matches(parse("chip.yml:26"))
    assert parse("./chip.yml:25").matches(parse("chip.yml:24-26"))
    assert not parse("chip.yml:27").matches(parse("chip.yml:24-26"))
    assert parse("model:reset:rst_n").matches(parse("reset:rst_n"))
    assert not parse("model:reset:rst_n").matches(parse("chip.yml:41"))


def test_main_exit_codes_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = _answers(tmp_path, _perfect())
    assert grade.main([str(good)]) == 0
    assert "verdict: PASS" in capsys.readouterr().out
    assert grade.main([str(good), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] and payload["correct_citation_rate"] == 1.0
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert grade.main([str(empty)]) == 1
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n")
    assert grade.main([str(bad)]) == 2

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
    """An all-correct answer set: each reference answer, citing a matching line or key."""
    out: list[dict[str, object]] = []
    for q in grade.load_questions(QUESTIONS):
        if q.unknown:
            out.append({"id": q.id, "answer": "I don't know", "citations": [], "unknown": True})
            continue
        cite = q.expected[0]
        if ":" in cite and "-" in cite.rpartition(":")[2] and not cite.startswith("model:"):
            path, _, lines = cite.rpartition(":")
            cite = f"{path}:{lines.partition('-')[2]}"  # the last line of the range
        out.append({"id": q.id, "answer": q.reference, "citations": [cite]})
    return out


def test_every_answerable_question_has_a_reference_that_states_its_facts() -> None:
    for q in grade.load_questions(QUESTIONS):
        if q.unknown:
            assert not q.reference, q.id
            continue
        assert q.reference, q.id
        assert all(grade.states_fact(q.reference, alts) for alts in q.facts), q.id


def test_no_fact_is_a_bare_number() -> None:
    # A bare digit is found in almost any answer; facts name the context or the unit.
    for q in grade.load_questions(QUESTIONS):
        for alts in q.facts:
            for alt in alts:
                assert not alt.strip().isdigit(), (q.id, alt)


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
    answers["q01"] = {"id": "q01", "answer": "COMPARE resets to 0.", "citations": ["chip.yml:1"]}
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


# --- facts: word boundaries and regexes ------------------------------------------------


def test_literal_facts_match_on_word_boundaries() -> None:
    assert grade.states_fact("DIR is at offset 0x2, RW.", ("0x2",))
    assert not grade.states_fact("DIR is at offset 0x20.", ("0x2",))
    assert not grade.states_fact("DIR is at offset 10x2.", ("0x2",))
    assert grade.states_fact("only wdata[7:0] is used", ("[7:0]",))
    assert grade.states_fact("Access: RW", ("rw",))
    assert not grade.states_fact("Access: rwx", ("rw",))
    assert grade.states_fact("owned by the GPIO block", ("gpio", "tiny_gpio"))
    assert grade.states_fact("owned by tiny_gpio", ("gpio", "tiny_gpio"))


def test_regex_facts() -> None:
    assert grade.states_fact("It clears the interrupt", ("re:\\bclear(s|ed|ing)?\\b",))
    assert not grade.states_fact("It is unclear", ("re:\\bclear(s|ed|ing)?\\b",))
    pattern = grade.fact_pattern("re:ACTIVE[- ]LOW")
    assert pattern.search("rst_n is active-low") is not None  # case-insensitive


def _fact_check(qid: str, text: str) -> bool:
    [question] = [q for q in grade.load_questions(QUESTIONS) if q.id == qid]
    return grade.grade_one(question, {"answer": text, "citations": [question.expected[0]]}).passed


@pytest.mark.parametrize(
    ("qid", "right", "wrong"),
    [
        ("q01", "COMPARE has a reset value of 0.", "COMPARE resets to 1; it is at offset 0x0."),
        ("q01", "It is 0 after reset.", "COMPARE (bit 0) resets to 0xFF."),
        ("q02", "DIR is at offset 0x2, RW.", "DIR is at offset 0x1, RW; 2 fields."),
        ("q05", "pin_out is 8 bits wide.", "pin_out is 32 bits wide; DIR has 8 entries."),
        ("q05", "pin_out is [7:0].", "pin_out is 128 bits wide."),
        ("q06", "only the low 8 bits, wdata[7:0]", "all 32 bits of wdata; 8 registers"),
        ("q07", "addr is 4 bits wide", "addr is 2 bits wide per block; 4 blocks"),
        ("q08", "interrupt line 0", "interrupt line 1; base 0x0, offset 0"),
        ("q09", "gpio's base address is 0x4", "gpio's base is 0x40; it has 4 registers"),
        ("q11", "rst_n is active low", "rst_n is active high, never low"),
    ],
)
def test_a_wrong_answer_with_the_digit_elsewhere_fails(qid: str, right: str, wrong: str) -> None:
    assert _fact_check(qid, right)
    assert not _fact_check(qid, wrong)


def test_bad_facts_are_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    for fact in ('"re:("', '"re:"'):
        bad.write_text(
            f"questions:\n  - id: q1\n    expected: [{{cite: 'model:x'}}]\n    facts: [[{fact}]]\n"
        )
        with pytest.raises(grade.GradeError):
            grade.load_questions(bad)


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

"""M1-13: the safe FTS5 query builder never lets FTS5 syntax through, on hostile input."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipgraph.core.model import DesignModel, ModelStore
from chipgraph.packs.assist.ask.fts import MAX_TERMS, fts_query, question_terms

HOSTILE = [
    "",
    "   ",
    '"',
    '""""',
    "*",
    "reg*",
    "NEAR(timer gpio, 2)",
    "timer NEAR gpio",
    "(timer OR gpio",
    "timer) AND (",
    "NOT timer",
    "AND OR NOT",
    "^timer",
    "text:timer",
    "path:rtl",
    "{text path}: timer",
    'what is "COUNT',
    "timer - gpio",
    "--",
    "a.b.c.",
    "-REQ-TIM-004-",
    "'; DROP TABLE documents; --",
    "\x00\x01 timer",
    "é ü 中文 timer",
    "the of and is",
    "0x3 ?? !!",
]


def test_empty_and_stop_word_questions_give_no_query() -> None:
    assert fts_query("") is None
    assert fts_query("   ") is None
    assert fts_query("what is the of and") is None
    assert fts_query("* ( ) ^ :") is None


def test_terms_are_quoted_and_ored() -> None:
    assert fts_query("What is the reset value of COUNT?") == '"reset" OR "value" OR "count"'


def test_ids_dotted_names_and_snake_case_stay_one_term() -> None:
    terms = question_terms("What does REQ-TIM-004 say about CTRL.IRQ_CLR and pin_out?")
    assert "req-tim-004" in terms
    assert "ctrl.irq_clr" in terms
    assert "pin_out" in terms


def test_plural_adds_the_singular() -> None:
    terms = question_terms("Which registers and interrupts?")
    assert terms == ["registers", "register", "interrupts", "interrupt"]


def test_operators_become_plain_quoted_words() -> None:
    query = fts_query("timer NEAR gpio AND NOT (count*)")
    assert query == '"timer" OR "near" OR "gpio" OR "count"'
    assert "*" not in query and "(" not in query


def test_term_count_is_capped() -> None:
    question = " ".join(f"word{i}" for i in range(100))
    assert len(question_terms(question)) == MAX_TERMS


@pytest.mark.parametrize("question", HOSTILE)
def test_hostile_input_never_reaches_sqlite_as_syntax(tmp_path: Path, question: str) -> None:
    store = ModelStore(tmp_path / "model.db")
    store.write(DesignModel.build([], []))
    store.add_document("doc/a.md", "timer and gpio\nREQ-TIM-004 COUNT reset\n")
    query = fts_query(question)
    if query is None:
        return
    for token in query.split(" OR "):
        assert token.startswith('"') and token.endswith('"')
        assert '"' not in token[1:-1]
    store.search(query)  # must not raise sqlite3.OperationalError


def test_non_ascii_words_are_kept_whole() -> None:
    assert question_terms("Giá trị reset của thanh ghi COMPARE?") == [
        "giá",
        "trị",
        "reset",
        "của",
        "thanh",
        "ghi",
        "compare",
    ]

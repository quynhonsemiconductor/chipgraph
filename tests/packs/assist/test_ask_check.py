"""M1-13: `ask_check` accepts answers with valid citations and rejects everything else."""

from __future__ import annotations

from pathlib import Path

import pytest
from ask_helpers import ingested_tinysoc, project

from chipgraph.app.context import AppContext
from chipgraph.packs.assist.ask import AskAnswer, ask_check, check_answer
from chipgraph.packs.assist.ask.check import MAX_RANGE


@pytest.fixture(scope="module")
def tinysoc(tmp_path_factory: pytest.TempPathFactory) -> AppContext:
    return ingested_tinysoc(tmp_path_factory.mktemp("ask") / "tinysoc")


def _check(ctx: AppContext, *citations: str, text: str = "The reset value is 0.", unknown=False):
    return ask_check(ctx, AskAnswer(answer=text, citations=citations, unknown=unknown))


# --- accepted -------------------------------------------------------------------------


def test_document_line_and_model_key_are_accepted(tinysoc: AppContext) -> None:
    result = _check(tinysoc, "doc/specs/TINY_TIMER_MAS.md:60", "model:register:timer.COMPARE")
    assert result.ok and result.reasons == ()
    assert result.answer is not None
    assert result.answer.citations == (
        "doc/specs/TINY_TIMER_MAS.md:60",
        "model:register:timer.COMPARE",
    )
    doc, key = result.citations
    assert doc.kind == "document" and "`COMPARE`" in (doc.text or "")
    assert key.kind == "model" and "reset_value=0" in (key.text or "")


def test_citations_are_normalised_and_deduplicated(tinysoc: AppContext) -> None:
    result = _check(
        tinysoc,
        "./doc/specs/TINY_TIMER_MAS.md:60",
        "doc/specs/TINY_TIMER_MAS.md:60",
        "register:timer.COMPARE",  # a bare model key
        " model:register:timer.COMPARE ",
        "rtl/tiny_timer.sv:28-31",
    )
    assert result.ok
    assert result.answer is not None
    assert result.answer.citations == (
        "doc/specs/TINY_TIMER_MAS.md:60",
        "model:register:timer.COMPARE",
        "rtl/tiny_timer.sv:28-31",
    )


def test_unknown_needs_no_citation(tinysoc: AppContext) -> None:
    result = _check(tinysoc, text="I don't know: no UART in the sources.", unknown=True)
    assert result.ok
    assert result.answer is not None and result.answer.unknown
    assert result.answer.answer.startswith("I don't know")
    empty = _check(tinysoc, text="", unknown=True)
    assert empty.ok and empty.answer is not None and empty.answer.answer


def test_first_and_last_lines_are_citable(tinysoc: AppContext) -> None:
    lines = len((tinysoc.root / "chip.yml").read_text().splitlines())
    assert _check(tinysoc, "chip.yml:1").ok
    assert _check(tinysoc, f"chip.yml:{lines}").ok


# --- rejected -------------------------------------------------------------------------


def test_no_citation_is_rejected_unless_unknown(tinysoc: AppContext) -> None:
    result = _check(tinysoc)
    assert not result.ok and result.answer is None
    assert any("at least one valid citation" in r for r in result.reasons)


def test_invented_file_is_rejected(tinysoc: AppContext) -> None:
    result = _check(tinysoc, "doc/specs/TINY_UART_MAS.md:12")
    assert not result.ok
    [verdict] = result.citations
    assert not verdict.valid and "not an indexed document" in (verdict.reason or "")


def test_invented_model_key_is_rejected_with_a_hint(tinysoc: AppContext) -> None:
    result = _check(tinysoc, "model:register:timer.COMPAR")
    assert not result.ok
    [verdict] = result.citations
    assert "no model key" in (verdict.reason or "")
    assert "register:timer.COMPARE" in (verdict.reason or "")


def test_out_of_range_lines_are_rejected(tinysoc: AppContext) -> None:
    lines = len((tinysoc.root / "chip.yml").read_text().splitlines())
    for citation in (
        f"chip.yml:{lines + 1}",
        "chip.yml:0",
        "chip.yml:9-3",
        f"chip.yml:1-{MAX_RANGE + 1}",
        f"chip.yml:{lines - 1}-{lines + 5}",
    ):
        result = _check(tinysoc, citation)
        assert not result.ok, citation
        assert not result.citations[0].valid


def test_a_file_on_disk_that_is_not_indexed_is_rejected(tinysoc: AppContext) -> None:
    assert (tinysoc.root / "Makefile").is_file()
    result = _check(tinysoc, "Makefile:19")
    assert not result.ok
    assert "not an indexed document" in (result.citations[0].reason or "")


def test_one_invalid_citation_rejects_the_whole_answer(tinysoc: AppContext) -> None:
    result = _check(tinysoc, "model:register:timer.COMPARE", "model:register:timer.PRESCALER")
    assert not result.ok
    assert [c.valid for c in result.citations] == [True, False]
    assert len(result.reasons) == 1 and "PRESCALER" in result.reasons[0]


def test_an_unknown_answer_with_an_invented_citation_is_rejected(tinysoc: AppContext) -> None:
    assert not _check(tinysoc, "rtl/tiny_uart.sv:1", text="I don't know", unknown=True).ok


def test_empty_text_and_garbage_citations_are_rejected(tinysoc: AppContext) -> None:
    assert not _check(tinysoc, "model:register:timer.COMPARE", text="  ").ok
    for citation in ("", "   ", "somewhere", "chip.yml", "chip.yml:abc"):
        assert not _check(tinysoc, citation).ok, citation


def test_nda_sources_are_not_citable(tmp_path: Path) -> None:
    ctx = ingested_tinysoc(
        tmp_path / "t", profile_extra='data:\n  nda_paths: ["doc/specs/TINY_GPIO_MAS.md"]\n'
    )
    loaded = project(ctx)
    doc = check_answer(loaded, AskAnswer(answer="x", citations=("doc/specs/TINY_GPIO_MAS.md:64",)))
    assert not doc.ok and "nda" in (doc.citations[0].reason or "")
    key = check_answer(loaded, AskAnswer(answer="x", citations=("model:register:gpio.DIR",)))
    assert not key.ok and "nda" in (key.citations[0].reason or "")
    assert check_answer(loaded, AskAnswer(answer="x", citations=("rtl/tiny_gpio.sv:10",))).ok

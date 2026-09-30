"""M1-13: `ask_context` on tinysoc returns the right citations, and nothing from `nda` files."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from ask_helpers import EVAL_QUESTIONS, copy_tinysoc, ingested_tinysoc, project

from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.packs.assist.ask import ask_context, retrieve
from chipgraph.packs.assist.ask.retrieve import MAX_LIMIT


@pytest.fixture(scope="module")
def tinysoc(tmp_path_factory: pytest.TempPathFactory) -> AppContext:
    return ingested_tinysoc(tmp_path_factory.mktemp("ask") / "tinysoc")


def _citations(context: object) -> list[str]:
    sources = context.sources  # type: ignore[attr-defined]
    return [s.citation for s in sources] + [s.defined_at for s in sources if s.defined_at]


def _answerable() -> list[dict[str, object]]:
    data = yaml.safe_load(EVAL_QUESTIONS.read_text())
    return [q for q in data["questions"] if not q.get("unknown")]


def _matches(citation: str, expected: str) -> bool:
    if citation.startswith("model:") or expected.startswith("model:"):
        return citation == expected
    path, _, lines = expected.rpartition(":")
    start, _, end = lines.partition("-")
    cpath, _, cline = citation.rpartition(":")
    return cpath == path and int(start) <= int(cline) <= int(end or start)


def test_typed_lookup_finds_the_register(tinysoc: AppContext) -> None:
    context = ask_context(tinysoc, "What is the reset value of the timer COMPARE register?")
    first = context.sources[0]
    assert first.citation == "model:register:timer.COMPARE"
    assert first.origin == "lookup"
    assert "reset_value=0" in first.text and "offset=0x1" in first.text
    assert first.defined_at == "doc/specs/TINY_TIMER_MAS.md:60"
    assert not context.no_sources


def test_requirement_id_and_its_document_line(tinysoc: AppContext) -> None:
    context = ask_context(tinysoc, "What does requirement REQ-TIM-004 say?")
    cites = _citations(context)
    assert "model:requirement:REQ-TIM-004" in cites
    assert "doc/specs/TINY_TIMER_MAS.md:78" in cites
    [doc] = [s for s in context.sources if s.citation == "doc/specs/TINY_TIMER_MAS.md:78"]
    assert doc.kind == "document" and "IRQ_CLR" in doc.text
    assert [line.line for line in doc.context] == [76, 77, 79, 80]


def test_a_port_in_the_named_module_ranks_first(tinysoc: AppContext) -> None:
    context = ask_context(tinysoc, "How wide is the pin_out port of tiny_gpio?")
    ports = [s.citation for s in context.sources if s.citation.startswith("model:port:")]
    assert set(ports[:2]) == {"model:port:tiny_gpio.pin_out", "model:port:spec.gpio.pin_out"}


def test_module_lookup_shows_who_instantiates_it(tinysoc: AppContext) -> None:
    context = ask_context(
        tinysoc, "Which module instantiates tiny_timer, and under what instance name?"
    )
    [module] = [s for s in context.sources if s.citation == "model:module:tiny_timer"]
    assert "module:tiny_top as u_timer" in module.text


@pytest.mark.parametrize("question", _answerable(), ids=lambda q: str(q["id"]))
def test_every_eval_question_retrieves_an_expected_citation(
    tinysoc: AppContext, question: dict[str, object]
) -> None:
    context = ask_context(tinysoc, str(question["question"]))
    expected = [str(e["cite"]) for e in question["expected"]]  # type: ignore[attr-defined]
    cites = _citations(context)
    assert any(_matches(c, e) for c in cites for e in expected), (cites, expected)


def test_no_usable_term_means_no_sources(tinysoc: AppContext) -> None:
    context = ask_context(tinysoc, "what is the of and?")
    assert context.no_sources and context.sources == () and context.query is None
    assert "unknown" in context.note


def test_no_match_is_no_sources(tinysoc: AppContext) -> None:
    context = ask_context(tinysoc, "zebracorn quux")
    assert context.no_sources and context.query == '"zebracorn" OR "quux"'


def test_hostile_question_does_not_raise(tinysoc: AppContext) -> None:
    context = ask_context(tinysoc, '" OR * NEAR( timer ) AND NOT ^ text:"')
    assert context.query is not None and "*" not in context.query


def test_limit_is_respected_and_clamped(tinysoc: AppContext) -> None:
    assert len(ask_context(tinysoc, "timer gpio register reset", limit=3).sources) == 3
    loaded = project(tinysoc)
    many = retrieve(loaded, "timer gpio register reset clock port", limit=10_000)
    assert len(many.sources) <= MAX_LIMIT


def test_no_model_store_is_an_error(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "t")
    with pytest.raises(AppError, match="chipgraph ingest"):
        ask_context(AppContext.load(root), "timer")


def test_nothing_from_an_nda_file_is_returned(tmp_path: Path) -> None:
    ctx = ingested_tinysoc(
        tmp_path / "t",
        profile_extra='data:\n  nda_paths: ["doc/specs/TINY_GPIO_MAS.md", "rtl/tiny_gpio.sv"]\n',
    )
    nda = {"doc/specs/TINY_GPIO_MAS.md", "rtl/tiny_gpio.sv"}
    loaded = project(ctx)
    for question in (
        "At which offset is the GPIO DIR register?",
        "How wide is the pin_out port of tiny_gpio?",
        "What does REQ-GPIO-001 say about DATA_OUT?",
    ):
        context = retrieve(loaded, question, limit=50)
        for source in context.sources:
            if source.kind == "document":
                assert source.citation.rsplit(":", 1)[0] not in nda
            else:
                entity = loaded.model.get(source.citation.removeprefix("model:"))
                assert entity is not None and entity.source.file not in nda, source
            assert source.defined_at is None or source.defined_at.rsplit(":", 1)[0] not in nda
    # A visible entity's summary does not leak the keys of hidden ones either.
    block = retrieve(loaded, "What does the gpio block contain?", limit=50)
    [gpio] = [s for s in block.sources if s.citation == "model:block:gpio"]
    assert "module:tiny_gpio" not in gpio.text and "register:gpio" not in gpio.text
    # The timer side is untouched.
    assert "model:register:timer.CTRL" in _citations(retrieve(loaded, "timer CTRL register"))

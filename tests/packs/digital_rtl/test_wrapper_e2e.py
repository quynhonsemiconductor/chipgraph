"""M2-12 end to end, in process, with the fake runtime and a real emacs.

The scripted author writes `rtl/emacs/tiny_fifo_wrap.src.sv` -> the engine expands it
(`digital-rtl/wrapper_gen`, tool `emacs_auto`) -> `wrap_check`, `lint` and `naming` pass. A
first source whose template leaves three IP outputs unmapped is rejected with the expanded
lines, and the second try (the first moved aside: the author can only create) is accepted.
Skips without emacs and verilog-mode. No model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from wrapper_helpers import (
    AUTHOR,
    GEN,
    OUT,
    SRC,
    UNMAPPED_OUTPUTS,
    WrapperRuntime,
    create,
    get_context,
    make_project,
    model_style_source,
    needs_emacs,
    next_task,
    source_text,
    submit,
)

from chipgraph.app.build import make_scheduler
from chipgraph.app.context import AppContext
from chipgraph.checks.generated import verify
from chipgraph.core.engine.records import RecordStore
from chipgraph.core.state.journal import read

EXAMPLE_OUT = Path(__file__).resolve().parents[3] / "examples" / "tinysoc" / OUT

pytestmark = needs_emacs


def _run(root: Path, runtime: WrapperRuntime, target: str = GEN):  # type: ignore[no-untyped-def]
    ctx = AppContext.load(root)
    ctx.registry.register("runtime", "generic", runtime)
    return ctx, asyncio.run(make_scheduler(ctx, target).run(target))


def _checks(ctx: AppContext, run_id: str) -> list[tuple[str, str]]:
    events = read(ctx.layout.journal(run_id)).events
    return [
        (e.payload["check_id"], e.payload["status"]) for e in events if e.type == "check_result"
    ]


def test_a_rejected_template_is_fixed_on_the_second_try(tmp_path: Path) -> None:
    root = make_project(tmp_path / "tinysoc", source=False, generated=False, ingest=True)
    runtime = WrapperRuntime(root, [model_style_source(UNMAPPED_OUTPUTS), model_style_source()])
    ctx, summary = _run(root, runtime)

    assert summary.ok, summary
    assert set(summary.done) == {AUTHOR, GEN}, summary
    assert len(runtime.tasks) == 2
    first, second = runtime.tasks
    assert first.role == "rtl-author" and first.allowed_writes == (SRC,)
    assert "feedback" not in first.context
    feedback = second.context["feedback"]
    assert "check `wrap_check`: fail" in feedback
    assert "wrap.untemplated" in feedback and ".full_o (full_o)" in feedback
    assert (root / SRC).read_text() == model_style_source()

    generated = (root / OUT).read_text()
    assert verify(generated) is True
    assert ".full_o                     (o_full),                // Templated" in generated
    production = RecordStore(ctx.layout).get(GEN)
    assert production is not None and production.output_hashes

    results = _checks(ctx, summary.run_id)
    assert results[0] == ("wrap_check", "fail")  # the author's first try
    assert results[1] == ("wrap_check", "pass")  # its second
    assert [name for name, _ in results[2:]] == ["wrap_check", "format", "lint", "naming"]
    assert {status for name, status in results[1:] if name != "format"} == {"pass"}


def test_the_committed_sample_is_what_the_pipeline_writes(tmp_path: Path) -> None:
    root = make_project(tmp_path / "tinysoc", source=False, generated=False, ingest=True)
    runtime = WrapperRuntime(root, [source_text()])
    _, summary = _run(root, runtime)
    assert summary.ok, summary
    assert len(runtime.tasks) == 1
    assert (root / OUT).read_text() == EXAMPLE_OUT.read_text()


def test_the_create_only_author_through_the_claude_code_flow(tmp_path: Path) -> None:
    """What the real run does: the author cannot overwrite, so the engine moves its first
    attempt aside, shows it in `previous_outputs` with the redo text, and it writes anew."""
    root = make_project(
        tmp_path / "tinysoc", source=False, generated=False, runtime="claude-code", ingest=True
    )
    next_task(root)
    context = get_context(root)
    assert context["previous_outputs"] == []
    assert set(context["rtl"]) >= {"wrapper"}
    create(root, SRC, model_style_source(UNMAPPED_OUTPUTS))
    first = submit(root)
    assert first["status"] == "rejected", first
    assert "wrap.untemplated" in first["redo"] and f"{SRC}:" in first["redo"]
    assert ".full_o (full_o)" in first["redo"]

    next_task(root)
    assert not (root / SRC).exists()
    [entry] = get_context(root)["previous_outputs"]
    assert entry["path"] == SRC and "AUTO_TEMPLATE" in entry["content"]
    create(root, SRC, model_style_source())
    second = submit(root)
    assert second["accepted"] is True, (second["reasons"], second["checks"])

    ctx = AppContext.load(root)
    summary = asyncio.run(make_scheduler(ctx, GEN).run(GEN))  # the engine's generator
    assert summary.ok and GEN in summary.done, summary
    generated = (root / OUT).read_text()
    assert verify(generated) is True and "(o_full),                // Templated" in generated

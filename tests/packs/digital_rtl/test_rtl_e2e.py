"""M2-04 end to end, in process: the approved plan of tinysoc's new `pwm` block ->
`digital-rtl/rtl_module` for the new module `tiny_pwm`, written by a scripted author ->
format -> lint (Verilator when it is on PATH, else skipped) -> naming -> style ->
hardcode -> accepted. A first module that breaks the naming rule is rejected, its
failures are the next try's redo text, and the fixed module is accepted. The engine's
formatter ran on it before the other checks, and the hash recorded is the formatted file's.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from rtl_author_helpers import OUTPUT, TASK, RtlRuntime, fx, profile_of, render_pwm

from chipgraph.app.build import make_scheduler
from chipgraph.app.context import AppContext
from chipgraph.core.engine.records import RecordStore
from chipgraph.core.state.artifacts import hash_file
from chipgraph.core.state.journal import read

pytestmark = pytest.mark.skipif(
    shutil.which("verilator") is None or shutil.which("make") is None,
    reason="the lint check needs verilator and make",
)

CHECK_ORDER = ["format", "lint", "naming", "style", "hardcode"]


def test_plan_to_an_accepted_module_through_every_check(tmp_path: Path) -> None:
    root = fx.make_rtl_project(tmp_path / "tinysoc", exemplars="a", runtime="generic")
    good = render_pwm(profile_of("a"))
    bad = render_pwm(profile_of("a"), bad_signal="BadSignal")
    messy = "\n".join(line + "  " for line in good.splitlines()) + "\n\n"
    runtime = RtlRuntime(root, [bad, messy])
    ctx = AppContext.load(root)
    ctx.registry.register("runtime", "generic", runtime)
    summary = asyncio.run(make_scheduler(ctx, TASK).run(TASK))

    assert summary.ok and summary.done == (TASK,), summary
    assert len(runtime.tasks) == 2
    first, second = runtime.tasks
    assert first.role == "rtl-author" and first.allowed_writes == (OUTPUT,)
    assert "feedback" not in first.context
    feedback = second.context["feedback"]
    assert "check `naming`: fail" in feedback and "BadSignal" in feedback
    assert "constraint" in feedback

    text = (root / OUTPUT).read_text()
    assert text == good.rstrip("\n") + "\n"  # the engine's formatter ran
    production = RecordStore(ctx.layout).get(TASK)
    assert production is not None
    assert production.output_hashes == {f".:{OUTPUT}": hash_file(root / OUTPUT)}

    events = read(ctx.layout.journal(summary.run_id)).events
    checks = [e.payload["check_id"] for e in events if e.type == "check_result"]
    assert checks == CHECK_ORDER * 2  # each try: the rule's checks, in order, once
    statuses = [e.payload["status"] for e in events if e.type == "check_result"][-5:]
    assert statuses == ["pass"] * 5

"""Worker for the resume/kill acceptance test (test_resume_kill.py).

Not a test module (no `test_*` functions) and not imported by anything: spawned as a
subprocess, runs a slow a->b->c chain, and gets SIGKILL'd by the parent process partway
through. Writes the run id to `sys.argv[2]` as soon as it is generated, so the parent can
find this run's journal and records while the child is still running.

Usage: python _resume_kill_worker.py <repo_root> <run_id_out_file> <delay_seconds>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scheduler_fixtures import UppercaseExecutor

from chipgraph.core.contracts import InputSpec, RuleSpec
from chipgraph.core.engine import scheduler as scheduler_mod
from chipgraph.core.engine.graph import StaticForeach, build_graph
from chipgraph.core.engine.scheduler import Scheduler
from chipgraph.core.state.artifacts import ArtifactStore, LabelRules
from chipgraph.core.state.layout import StateLayout, new_run_id


def chain_rules() -> list[RuleSpec]:
    """The same a->b->c chain the parent test builds to compare against."""
    return [
        RuleSpec(id="p/a", kind="gen", outputs=("a.txt",)),
        RuleSpec(
            id="p/b",
            kind="gen",
            outputs=("b.txt",),
            inputs=(InputSpec(source="path", selector="a.txt"),),
        ),
        RuleSpec(
            id="p/c",
            kind="gen",
            outputs=("c.txt",),
            inputs=(InputSpec(source="path", selector="b.txt"),),
        ),
    ]


def main() -> None:
    root = Path(sys.argv[1])
    run_id_path = Path(sys.argv[2])
    delay_s = float(sys.argv[3])

    layout = StateLayout(root)
    store = ArtifactStore(root, LabelRules())
    graph = build_graph(chain_rules(), StaticForeach({}))
    executor = UppercaseExecutor(root, delay_s=delay_s)
    scheduler = Scheduler(
        graph, layout=layout, store=store, executors={"gen": executor}, concurrency=1
    )

    def _new_run_id_and_record() -> str:
        run_id = new_run_id()
        run_id_path.write_text(run_id, encoding="utf-8")
        return run_id

    # Capture the run id the instant it is generated, before any instance executes, so
    # the parent test can poll this run's records while we are still running.
    scheduler_mod.new_run_id = _new_run_id_and_record  # type: ignore[assignment]

    asyncio.run(scheduler.run("*"))


if __name__ == "__main__":
    main()

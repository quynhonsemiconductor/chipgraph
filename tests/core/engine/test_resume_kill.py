"""The kill test: chipgraph's key resume acceptance criterion (docs/IMPLEMENTATION_PLAN.md,
M0-08: "kill o tung loai buoc roi resume, ket qua giong chay lien").

Runs the scheduler in a real subprocess against a slow a->b->c chain, kills it with
SIGKILL right after the first instance has recorded (so the second is caught mid-flight),
then resumes in this process and compares the outcome against an uninterrupted run of the
same chain in a separate temporary repo.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from scheduler_fixtures import UppercaseExecutor

from chipgraph.core.contracts import InputSpec, RuleSpec, RunSpec
from chipgraph.core.engine.graph import StaticForeach, build_graph
from chipgraph.core.engine.records import RecordStore
from chipgraph.core.engine.scheduler import Scheduler
from chipgraph.core.state.artifacts import ArtifactStore, LabelRules
from chipgraph.core.state.layout import StateLayout

_WORKER = Path(__file__).parent / "_resume_kill_worker.py"
_POLL_TIMEOUT_S = 20.0
_POLL_INTERVAL_S = 0.05


def _chain_rules() -> list[RuleSpec]:
    return [
        RuleSpec(id="p/a", kind="gen", outputs=("a.txt",), run=RunSpec(use="cmd")),
        RuleSpec(
            id="p/b",
            kind="gen",
            outputs=("b.txt",),
            inputs=(InputSpec(source="path", selector="a.txt"),),
            run=RunSpec(use="cmd"),
        ),
        RuleSpec(
            id="p/c",
            kind="gen",
            outputs=("c.txt",),
            inputs=(InputSpec(source="path", selector="b.txt"),),
            run=RunSpec(use="cmd"),
        ),
    ]


def _wait_until(predicate: object, timeout_s: float = _POLL_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(_POLL_INTERVAL_S)
    raise TimeoutError("condition not met before timeout")


def test_kill_mid_run_then_resume_matches_an_uninterrupted_run(tmp_path: Path) -> None:
    killed_root = tmp_path / "killed"
    killed_root.mkdir()
    clean_root = tmp_path / "clean"
    clean_root.mkdir()

    run_id_path = killed_root / "run_id.txt"
    proc = subprocess.Popen(
        [sys.executable, str(_WORKER), str(killed_root), str(run_id_path), "0.3"]
    )
    layout = StateLayout(killed_root)
    records = RecordStore(layout)
    try:
        _wait_until(lambda: run_id_path.exists())
        run_id = run_id_path.read_text(encoding="utf-8").strip()
        # 'a' finished (has a record) and 'b' has been journaled as running but has not
        # recorded yet: this is the instance the kill catches mid-flight.
        _wait_until(lambda: records.get("p/a[]") is not None)
        assert records.get("p/b[]") is None
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:  # pragma: no cover - safety net if the above raised
            proc.kill()
            proc.wait(timeout=10)

    assert proc.returncode is not None and proc.returncode != 0
    assert records.get("p/a[]") is not None
    assert records.get("p/b[]") is None  # killed before it could record

    store = ArtifactStore(killed_root, LabelRules())
    graph = build_graph(_chain_rules(), StaticForeach({}))
    executor = UppercaseExecutor(killed_root)
    scheduler = Scheduler(graph, layout=layout, store=store, executors={"gen": executor})

    resumed_summary = asyncio.run(scheduler.resume(run_id))

    assert resumed_summary.ok
    assert resumed_summary.run_id == run_id
    # 'a' already had a record and is untouched: not re-run. 'b' was mid-flight when
    # killed (no record): it runs again. 'c' depends on 'b' and had never run either.
    assert executor.calls.get("p/a[]", 0) == 0
    assert executor.calls["p/b[]"] == 1
    assert executor.calls["p/c[]"] == 1
    assert set(resumed_summary.done) == {"p/b[]", "p/c[]"}
    assert resumed_summary.skipped_fresh == ("p/a[]",)

    clean_layout = StateLayout(clean_root)
    clean_store = ArtifactStore(clean_root, LabelRules())
    clean_graph = build_graph(_chain_rules(), StaticForeach({}))
    clean_executor = UppercaseExecutor(clean_root)
    clean_scheduler = Scheduler(
        clean_graph, layout=clean_layout, store=clean_store, executors={"gen": clean_executor}
    )
    clean_summary = asyncio.run(clean_scheduler.run("*"))

    assert clean_summary.ok
    assert set(clean_summary.done) == {"p/a[]", "p/b[]", "p/c[]"}

    # Same result as an uninterrupted run: same final artifacts, same records, and both
    # runs account for every instance as finished-ok with nothing failed or blocked.
    for name in ("a.txt", "b.txt", "c.txt"):
        assert (killed_root / name).read_text() == (clean_root / name).read_text()

    killed_records = records.all()
    clean_records = RecordStore(clean_layout).all()
    assert set(killed_records) == set(clean_records) == {"p/a[]", "p/b[]", "p/c[]"}
    for iid in killed_records:
        assert killed_records[iid].inputs_hash == clean_records[iid].inputs_hash
        assert killed_records[iid].output_hashes == clean_records[iid].output_hashes

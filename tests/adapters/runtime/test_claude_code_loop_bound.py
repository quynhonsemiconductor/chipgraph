"""M2-02b property: for any sequence of check results, a task in runtime claude-code is
dispatched at most `tries + max_infra_retries` times, and it always ends.

Drives the queue the way `next_task`/`submit` do, with `loop.judge` deciding each
submit, over random sequences of passes, failures (new or repeated outputs), errored
checks and `needs_human` reports. No project and no model.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from pathlib import Path

import pytest

from chipgraph.adapters.runtime.claude_code import loop
from chipgraph.core.contracts import (
    AgentResult,
    ArtifactRef,
    Budget,
    CheckResult,
    Issue,
    RuleInstance,
    RuleSpec,
)
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime import TaskQueue
from chipgraph.core.state.layout import StateLayout

TASK = "p/write[]"
KINDS = ("pass", "fail", "same", "error", "needs_human")


def _result(kind: str, n: int) -> CheckResult:
    status = {"pass": "pass", "error": "error"}.get(kind, "fail")
    msg = "same failure" if kind == "same" else f"failure {n}"
    issues = () if status == "pass" else (Issue(file="rtl/a.sv", line=1, msg=msg),)
    return CheckResult(
        check_id="lint", status=status, issues=issues, duration_s=0.0, idempotency_key=f"k{n}"
    )


def _run(tmp_path: Path, tries: int, pick: Callable[[int], str]) -> tuple[int, str]:
    rule = RuleSpec(
        id="p/write", kind="agent", role="author", outputs=("rtl/a.sv",), checks=("lint",),
        budget=Budget(tries=tries),
    )  # fmt: skip
    instance = RuleInstance(
        rule_id="p/write", outputs=(ArtifactRef(kind="rtl", path="rtl/a.sv"),), instance_id=TASK
    )
    task = AgentTask(
        instance=instance, role="author", allowed_writes=("rtl/a.sv",), budget=rule.budget
    )
    queue = TaskQueue(StateLayout(tmp_path))
    queue.enqueue(
        task, inputs_hash="0", output_hashes={}, max_dispatches=loop.max_dispatches(tries)
    )
    for n in range(100):
        record = queue.require(TASK)
        if record.status not in ("ready", "rejected"):
            break
        record = queue.dispatch(TASK, run_id="r", baseline={})
        kind = pick(n)
        if kind == "needs_human":
            queue.needs_human(
                TASK, result=AgentResult(status="needs_human", open_questions=("q?",))
            )
            continue
        record = queue.mark_submitted(TASK)
        outputs = {"rtl/a.sv": "same" if kind == "same" else f"h{n}"}
        agent = AgentResult(status="done", files_written=("rtl/a.sv",))
        verdict = asyncio.run(loop.judge(rule, record, [_result(kind, n)], agent, outputs))
        if verdict.accepted:
            queue.accept(TASK, result=agent, output_hashes=outputs, budget_state=verdict.state)
        elif verdict.status == "needs_human":
            queue.needs_human(
                TASK,
                result=AgentResult(status="needs_human", open_questions=verdict.questions),
                budget_state=verdict.state,
            )
        else:
            queue.reject(
                TASK,
                result=AgentResult(status="failed"),
                reasons=("x",),
                counted=verdict.counted,
                exhausted=verdict.status == "budget_exhausted",
                budget_state=verdict.state,
            )
    final = queue.require(TASK)
    return final.dispatches, final.status


@pytest.mark.parametrize("seed", range(80))
def test_dispatches_never_exceed_tries_plus_infra_retries(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    tries = rng.randint(1, 4)
    dispatches, status = _run(tmp_path, tries, lambda _: rng.choice(KINDS))
    assert 1 <= dispatches <= tries + loop.MAX_INFRA_RETRIES
    assert status in ("accepted", "budget_exhausted", "needs_human")


def test_the_worst_case_reaches_the_bound_and_stops(tmp_path: Path) -> None:
    # One failed try, then errors only: tries - 1 + max_infra_retries + 1 dispatches.
    dispatches, status = _run(tmp_path, 2, lambda n: "fail" if n == 0 else "error")
    assert (dispatches, status) == (2 + loop.MAX_INFRA_RETRIES, "budget_exhausted")

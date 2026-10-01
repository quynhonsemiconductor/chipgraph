"""The Inspect AI side: one `Task` per suite, a solver per runtime, the grader as scorer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, scorer
from inspect_ai.solver import Generate, TaskState, solver

from .runtimes import Runtime
from .suites import Suite

RUN_KEY = "run"
"""The sample metadata key the solver stores its `SampleRun` under."""


def dataset(suite: Suite, items: list[Any]) -> MemoryDataset:
    """The suite's items as Inspect samples: input = the command arguments."""
    return MemoryDataset(
        [
            Sample(
                id=item.id,
                input=suite.prompt(item),
                target=suite.target(item),
                metadata={"suite": suite.name},
            )
            for item in items
        ],
        name=suite.name,
    )


@solver
def runtime_solver(suite: Suite, runtime: Runtime, items: dict[str, Any]) -> Callable[..., Any]:
    """Answer each sample with `runtime`; the answer goes to the sample's metadata."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        run = await runtime.run(items[str(state.sample_id)])
        state.metadata[RUN_KEY] = asdict(run)
        if run.answer is None:
            state.output.completion = run.note or "no answer"
        elif suite.kind == "ask":
            state.output.completion = str(run.answer.get("answer", ""))
        else:
            state.output.completion = str(run.answer.get("label"))
        return state

    return solve


@scorer(metrics=[accuracy()])
def grade_scorer(suite: Suite, items: dict[str, Any]) -> Callable[..., Any]:
    """The suite's deterministic grader (`grade_one`) on the solver's answer."""
    grader = suite.grader

    async def score(state: TaskState, target: Target) -> Score:
        run = state.metadata.get(RUN_KEY) or {}
        grade = grader.grade_one(items[str(state.sample_id)], run.get("answer"))
        return Score(
            value=CORRECT if grade.passed else INCORRECT,
            answer=state.output.completion,
            explanation=grade.note or None,
            metadata={"grade": asdict(grade), "not_run": run.get("not_run")},
        )

    return score


def suite_task(suite: Suite, items: list[Any], runtime: Runtime, info: dict[str, Any]) -> Task:
    by_id = {item.id: item for item in items}
    return Task(
        name=f"chipgraph_{suite.name.replace('-', '_')}",
        dataset=dataset(suite, items),
        solver=runtime_solver(suite, runtime, by_id),
        scorer=grade_scorer(suite, by_id),
        metadata={"suite": suite.name, "data": suite.data.name, **info},
    )


__all__ = ["RUN_KEY", "dataset", "grade_scorer", "runtime_solver", "suite_task"]

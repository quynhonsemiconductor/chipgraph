"""Shared fakes for the agent rule loop tests: a scripted `AgentRuntime`, content-driven
checks and a small project (one agent rule, one `gen` rule that depends on it).

Not a test module itself; imported by the `test_agent_rule*.py` modules.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from scheduler_fixtures import UppercaseExecutor

from chipgraph.core.contracts import AgentResult, Budget, InputSpec, RuleSpec, RunSpec
from chipgraph.core.contracts.check import CheckResult, Issue
from chipgraph.core.contracts.rule import RuleInstance
from chipgraph.core.engine.agent_rule import AgentRuleExecutor
from chipgraph.core.engine.graph import StaticForeach, build_graph
from chipgraph.core.engine.scheduler import Scheduler
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.state.artifacts import ArtifactStore, LabelRules
from chipgraph.core.state.layout import StateLayout

AGENT = "p/write[]"
NEXT = "p/next[]"
OUTPUT = "rtl/a.sv"


@dataclass
class Step:
    """What the fake runtime does on one call."""

    content: str | None = "good"
    """Written to every output (`None`: write nothing)."""
    status: str = "done"
    questions: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    files: tuple[str, ...] | None = None
    """`files_written` (default: the outputs, when content was written)."""
    tokens: int | None = None
    cost: float | None = None
    raises: BaseException | None = None


@dataclass
class FakeRuntime:
    """A scripted `AgentRuntime`: call n runs `steps[n]` (the last step repeats)."""

    root: Path
    steps: Sequence[Step] | Callable[[int, AgentTask], Step]
    name: str = "fake"
    tasks: list[AgentTask] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return len(self.tasks)

    def _step(self, n: int, task: AgentTask) -> Step:
        if callable(self.steps):
            return self.steps(n, task)
        return self.steps[min(n, len(self.steps) - 1)]

    async def run_task(self, task: AgentTask) -> AgentResult:
        step = self._step(len(self.tasks), task)
        self.tasks.append(task)
        if step.raises is not None:
            raise step.raises
        if step.content is not None:
            for path in task.allowed_writes:
                out = self.root / path
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(step.content, encoding="utf-8")
        files = step.files
        if files is None:
            files = task.allowed_writes if step.content is not None else ()
        return AgentResult(
            status=step.status,  # type: ignore[arg-type]
            files_written=files,
            assumptions=step.assumptions,
            open_questions=step.questions,
            tokens=step.tokens,
            cost=step.cost,
        )


def result(
    check_id: str,
    status: str,
    *,
    issues: Sequence[Issue] = (),
    log_tail: str = "",
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=status,  # type: ignore[arg-type]
        issues=tuple(issues),
        log_tail=log_tail,
        duration_s=0.0,
        idempotency_key=f"key-{check_id}-{status}",
    )


class ContentChecks:
    """A `CheckRunner` whose verdict comes from the output content (or a script).

    By default `lint` passes when the output contains "good" and otherwise fails with
    one issue naming the content. `script[check_id]` (a list of statuses, the last
    repeating) overrides that per call.
    """

    def __init__(
        self,
        root: Path,
        *,
        script: dict[str, list[str]] | None = None,
        issue_rule: str = "",
    ) -> None:
        self.root = root
        self.script = script or {}
        self.issue_rule = issue_rule
        self.calls: list[str] = []

    async def run(self, check_id: str, instance: RuleInstance) -> CheckResult:
        n = sum(1 for c in self.calls if c == check_id)
        self.calls.append(check_id)
        if check_id in self.script:
            seq = self.script[check_id]
            status = seq[min(n, len(seq) - 1)]
            issues = (
                (Issue(file=OUTPUT, line=1, rule=self.issue_rule, msg=f"{check_id} {status}"),)
                if status == "fail"
                else ()
            )
            if status == "error":
                issues = (Issue(msg=f"{check_id}: tool not found"),)
            return result(check_id, status, issues=issues)
        out = self.root / OUTPUT
        content = out.read_text(encoding="utf-8") if out.is_file() else ""
        if "good" in content:
            return result(check_id, "pass")
        return result(
            check_id,
            "fail",
            issues=(Issue(file=OUTPUT, line=3, rule=self.issue_rule, msg=f"bad: {content!r}"),),
        )


def agent_rule(
    *, budget: Budget | None = None, checks: tuple[str, ...] = ("lint",), role: str = "author"
) -> RuleSpec:
    return RuleSpec(
        id="p/write",
        kind="agent",
        role=role,
        inputs=(InputSpec(source="path", selector="spec.md"),),
        outputs=(OUTPUT,),
        checks=checks,
        budget=budget if budget is not None else Budget(tries=3),
    )


def next_rule() -> RuleSpec:
    return RuleSpec(
        id="p/next",
        kind="gen",
        run=RunSpec(use="cmd"),
        inputs=(InputSpec(source="path", selector=OUTPUT),),
        outputs=("out/next.txt",),
    )


@dataclass
class Project:
    root: Path
    runtime: FakeRuntime
    checks: ContentChecks
    executor: AgentRuleExecutor
    gen: UppercaseExecutor
    scheduler: Scheduler
    store: ArtifactStore
    layout: StateLayout


def project(
    root: Path,
    steps: Sequence[Step] | Callable[[int, AgentTask], Step],
    *,
    budget: Budget | None = None,
    rule_checks: tuple[str, ...] = ("lint",),
    checks: ContentChecks | None = None,
    max_infra_retries: int = 2,
    max_cost: float | None = None,
    stagnation: bool = True,
    remember: bool = True,
) -> Project:
    """A project with `spec.md`, the agent rule `p/write` and the gen rule `p/next`."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "spec.md").write_text("spec v1\n", encoding="utf-8")
    store = ArtifactStore(root, LabelRules())
    layout = StateLayout(root)
    runtime = FakeRuntime(root, steps)
    executor = AgentRuleExecutor(
        runtime,
        store=store,
        layout=layout if remember else None,
        max_infra_retries=max_infra_retries,
        max_cost=max_cost,
        stagnation=stagnation,
    )
    gen = UppercaseExecutor(root)
    graph = build_graph(
        [agent_rule(budget=budget, checks=rule_checks), next_rule()], StaticForeach({})
    )
    checks = checks if checks is not None else ContentChecks(root)
    scheduler = Scheduler(
        graph,
        layout=layout,
        store=store,
        executors={"agent": executor, "gen": gen},
        checks=checks,
    )
    return Project(root, runtime, checks, executor, gen, scheduler, store, layout)


__all__ = [
    "AGENT",
    "NEXT",
    "OUTPUT",
    "ContentChecks",
    "FakeRuntime",
    "Project",
    "Step",
    "agent_rule",
    "next_rule",
    "project",
    "result",
]

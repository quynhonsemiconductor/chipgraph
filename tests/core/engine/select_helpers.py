"""Shared fakes for the best-of-N tests: candidates with known check results, a scripted
runtime and check runner per candidate, and a small repo with one agent rule.

Not a test module itself; imported by the `test_select_*.py` modules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fanout_helpers import init_repo

from chipgraph.adapters.vcs.git import GitVcs
from chipgraph.core.config.models import FanoutCfg, SelectCfg
from chipgraph.core.contracts import AgentResult, ArtifactRef, Budget, InputSpec, RuleSpec
from chipgraph.core.contracts.check import CheckResult, Issue
from chipgraph.core.contracts.rule import RuleInstance
from chipgraph.core.engine.agent_rule import AgentRuleExecutor
from chipgraph.core.engine.scheduler import ExecContext, ExecEventType, ExecOutcome
from chipgraph.core.engine.select import (
    BestOfNExecutor,
    Candidate,
    CandidateEnv,
    CandidateParts,
    MetricProvider,
    ReviewScore,
    Tiebreak,
)
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.state.artifacts import ArtifactStore, LabelRules

OUTPUT = "rtl/a.sv"
RULE_ID = "p/write"


def check(
    status: str = "pass",
    *,
    warnings: int = 0,
    errors: int | None = None,
    infos: int = 0,
    check_id: str = "lint",
) -> CheckResult:
    """A check result with `warnings` warning issues and (when failing) `errors` errors."""
    n_errors = errors if errors is not None else (1 if status == "fail" else 0)
    issues = [Issue(file=OUTPUT, line=1, rule="e", msg=f"error {i}") for i in range(n_errors)]
    issues += [
        Issue(file=OUTPUT, line=1, rule="w", severity="warning", msg=f"warning {i}")
        for i in range(warnings)
    ]
    issues += [
        Issue(file=OUTPUT, line=1, rule="i", severity="info", msg=f"info {i}") for i in range(infos)
    ]
    return CheckResult(
        check_id=check_id,
        status=status,  # type: ignore[arg-type]
        issues=tuple(issues),
        duration_s=0.0,
        idempotency_key=f"{check_id}-{status}-{n_errors}-{warnings}",
    )


def cand(
    cid: str,
    *checks: CheckResult,
    passed: bool | None = None,
    finished: bool = True,
    workspace: Path | None = None,
) -> Candidate:
    """A candidate; `passed` defaults to whether every check is ok."""
    ok = all(c.ok for c in checks) if passed is None else passed
    return Candidate(id=cid, passed=ok, checks=checks, finished=finished, workspace=workspace)


@dataclass
class Areas:
    """A `MetricProvider` that reads the area from the workspace name's table."""

    table: Mapping[str, float | None]
    calls: list[str] = field(default_factory=list)

    def area(self, workspace: Path) -> float | None:
        self.calls.append(workspace.name)
        return self.table.get(workspace.name)


@dataclass
class Critic:
    """A tiebreak with fixed scores; it records which candidates it was asked about."""

    scores: Mapping[str, ReviewScore]
    asked: list[tuple[str, ...]] = field(default_factory=list)

    async def __call__(self, tied: tuple[Candidate, ...]) -> Mapping[str, ReviewScore]:
        self.asked.append(tuple(c.id for c in tied))
        return {c.id: self.scores[c.id] for c in tied if c.id in self.scores}


def score(verdict: str = "approve", **counts: Any) -> ReviewScore:
    return ReviewScore(verdict=verdict, **counts)  # type: ignore[arg-type]


assert isinstance(Areas({}), MetricProvider)


# --- a candidate's scripted behaviour ---------------------------------------------------


@dataclass
class Behaviour:
    """What one candidate does: the content it writes, its check results per try."""

    content: str = "x\n"
    lint: Sequence[CheckResult] = (check("pass"),)
    """The `lint` result of try n (the last repeats)."""
    tokens: int | Callable[[AgentTask], int] | None = None
    cost: float | None = None
    delay: float = 0.0
    raises: BaseException | None = None
    raise_calls: int = 0
    """The first `raise_calls` calls raise an infra error (the runtime crashed)."""
    started: asyncio.Event | None = None


@dataclass
class ScriptedRuntime:
    """Writes the behaviour's content to every output in its workspace."""

    root: Path
    behaviour: Behaviour
    name: str = "scripted"
    tasks: list[AgentTask] = field(default_factory=list)
    spec_seen: list[str] = field(default_factory=list)

    async def run_task(self, task: AgentTask) -> AgentResult:
        self.tasks.append(task)
        spec = self.root / "spec.md"
        self.spec_seen.append(spec.read_text() if spec.is_file() else "")
        b = self.behaviour
        if b.started is not None:
            b.started.set()
        if b.delay:
            await asyncio.sleep(b.delay)
        if b.raises is not None:
            raise b.raises
        if len(self.tasks) <= b.raise_calls:
            raise RuntimeError("the runtime crashed")
        for rel in task.allowed_writes:
            out = self.root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(b.content, encoding="utf-8")
        tokens = b.tokens(task) if callable(b.tokens) else b.tokens
        return AgentResult(
            status="done", files_written=task.allowed_writes, tokens=tokens, cost=b.cost
        )


@dataclass
class ScriptedChecks:
    """The behaviour's `lint` results, one per call (the last repeats)."""

    behaviour: Behaviour
    calls: int = 0

    async def run(self, check_id: str, instance: RuleInstance) -> CheckResult:
        seq = self.behaviour.lint
        result = seq[min(self.calls, len(seq) - 1)]
        self.calls += 1
        return result


@dataclass
class Factory:
    """An `InnerFactory`: an `AgentRuleExecutor` per candidate workspace."""

    behaviours: Mapping[str, Behaviour]
    envs: list[CandidateEnv] = field(default_factory=list)
    runtimes: dict[str, ScriptedRuntime] = field(default_factory=dict)
    fail_for: frozenset[str] = frozenset()
    guard: bool = True

    def __call__(self, env: CandidateEnv) -> CandidateParts:
        self.envs.append(env)
        if env.candidate_id in self.fail_for:
            raise RuntimeError(f"cannot build candidate {env.candidate_id}")
        behaviour = self.behaviours[env.candidate_id]
        runtime = ScriptedRuntime(env.workspace, behaviour)
        self.runtimes[env.candidate_id] = runtime
        executor = AgentRuleExecutor(
            env.guard(runtime) if self.guard else runtime,
            store=ArtifactStore(env.workspace, LabelRules()),
            max_cost=env.max_cost,
        )
        return CandidateParts(executor=executor, checks=ScriptedChecks(behaviour))


@dataclass
class Plain:
    """The plain executor: writes "plain" into the user's tree (never for a candidate)."""

    root: Path
    calls: list[str] = field(default_factory=list)

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        self.calls.append(instance.instance_id)
        return ExecOutcome(ok=True, message="plain run")


def rule(budget: Budget | None = None) -> RuleSpec:
    return RuleSpec(
        id=RULE_ID,
        kind="agent",
        role="author",
        inputs=(InputSpec(source="path", selector="spec.md"),),
        outputs=(OUTPUT,),
        checks=("lint",),
        budget=budget if budget is not None else Budget(tries=3),
    )


def instance(params: Mapping[str, str] | None = None) -> RuleInstance:
    p = dict(params or {})
    return RuleInstance(
        rule_id=RULE_ID,
        params=p,
        inputs=(ArtifactRef(kind="spec", path="spec.md"),),
        outputs=(ArtifactRef(kind="rtl", path=OUTPUT),),
        instance_id=RuleInstance.make_id(RULE_ID, p),
    )


def repo(root: Path) -> Path:
    init_repo(root, {"spec.md": "spec v1\n", OUTPUT: "old\n"})
    return root


@dataclass
class Journal:
    """An `ExecContext` whose events are kept in a list."""

    events: list[tuple[ExecEventType, dict[str, Any]]] = field(default_factory=list)

    def ctx(self) -> ExecContext:
        def emit(type_: ExecEventType, payload: dict[str, Any]) -> None:
            self.events.append((type_, payload))

        return ExecContext(run_id="run1", run_check=None, emit=emit)

    def of(self, type_: str, key: str, value: object) -> list[dict[str, Any]]:
        return [p for t, p in self.events if t == type_ and p.get(key) == value]


def best_of(
    root: Path,
    tmp: Path,
    factory: Factory,
    *,
    n: int = 3,
    cfg: SelectCfg | None = None,
    max_parallel: int | None = None,
    metrics: MetricProvider | None = None,
    tiebreak: Tiebreak | None = None,
    max_cost: float | None = None,
) -> tuple[BestOfNExecutor, Plain]:
    plain = Plain(root)
    executor = BestOfNExecutor(
        factory,
        GitVcs(),
        root=root,
        default=plain,
        cfg=cfg if cfg is not None else SelectCfg(rules={RULE_ID: n}),
        fanout=FanoutCfg(max_parallel=max_parallel),
        metrics=metrics,
        tiebreak=tiebreak,
        max_cost=max_cost,
        tmp_root=tmp,
    )
    return executor, plain

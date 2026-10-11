"""Building a `Scheduler` for a target: load rules from the profile's packs, expand
`foreach` over the profile's blocks, and wire up the executors, check runner and gate
evaluator (DESIGN.md 3.3, 3.4).

The graph also knows the packs' named targets (`targets/*.yml` in a pack, e.g.
`rtl:<block>`; `chipgraph.core.engine.targets`), so a target may be one everywhere a rule
selector may. With `review: {use: github}` the PR gates (`pr:<...>`) are read from the
pull request's reviews (`GitHubGateChecker`); every other gate, and every gate with the
`file` adapter, from the decision files (`chipgraph approve`).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from chipgraph.adapters.review.github import GitHubGateChecker
from chipgraph.adapters.runtime.claude_code import ClaudeCodeExecutor, ClaudeCodeRuntime
from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.app.executors import HumanExecutor, RunExecutor
from chipgraph.core.contracts import ArtifactRef, RuleInstance, RuleSpec
from chipgraph.core.engine.agent_rule import AgentRuleExecutor
from chipgraph.core.engine.graph import (
    BuildGraph,
    ForeachEntry,
    ForeachResolver,
    GraphError,
    build_graph,
)
from chipgraph.core.engine.rules import RuleLoadError, load_pack_rules
from chipgraph.core.engine.scheduler import AgentStub, Executor, GateChecker, Scheduler
from chipgraph.core.engine.targets import TargetError, TargetRegistry, load_pack_targets
from chipgraph.core.plugin_api.pack import Pack, discover_packs
from chipgraph.core.plugin_api.registry import PluginError
from chipgraph.core.runtime import TaskQueue
from chipgraph.core.runtime.roles import RoleError, check_agent_rules, task_denied_reads
from chipgraph.core.state.trace import Tracer

PLAN_MODULES = "plan.modules"
"""The `foreach` selector over the modules of every block's approved plan (M2-03)."""
PLAN_BLOCK = "plan.block"
"""The `foreach` selector over the blocks with an approved plan (M2-11a)."""
PLAN_BLOCKS = "plan.blocks"
"""The `foreach` selector over every block, after its approved plan's files (M2-11a)."""
PLAN_SELECTORS = (PLAN_MODULES, PLAN_BLOCK, PLAN_BLOCKS)


def builtin_packs_dir() -> Path | None:
    """The directory of the packs shipped inside the `chipgraph.packs` package (D36).

    Found through `importlib.resources`, so it is the same whether chipgraph runs from a
    checkout or is installed from a wheel. `None` if the package is not on a real
    filesystem (e.g. imported from a zip), since packs are loaded from directories.
    """
    root = resources.files("chipgraph.packs")
    return root if isinstance(root, Path) and root.is_dir() else None


def pack_search_paths(ctx: AppContext) -> list[Path]:
    """The search paths packs are discovered from, in order (DESIGN.md 3.2, D36).

    The project's `.chipgraph/packs/`, then the built-in packs, then each entry of
    `$CHIPGRAPH_PACK_PATH`. Two packs with the same name anywhere on the path are an error.
    """
    return pack_search_paths_for(ctx.root)


def pack_search_paths_for(root: Path) -> list[Path]:
    """`pack_search_paths` for the project at `root`."""
    paths = [root / ".chipgraph" / "packs"]
    builtin = builtin_packs_dir()
    if builtin is not None:
        paths.append(builtin)
    env_value = os.environ.get("CHIPGRAPH_PACK_PATH", "")
    paths.extend(Path(p) for p in env_value.split(os.pathsep) if p)
    return paths


def load_rules(ctx: AppContext) -> list[RuleSpec]:
    """Load every rule from the packs named in the profile's `packs`."""
    return rules_for_packs(ctx.root, ctx.require_profile().profile.packs)


def rules_for_packs(root: Path, packs: Iterable[str]) -> list[RuleSpec]:
    """Load every rule of the packs named `packs`, searched for from the project at `root`."""
    rules: list[RuleSpec] = []
    for pack in _packs(root, packs):
        try:
            rules.extend(load_pack_rules(pack))
        except RuleLoadError as exc:
            raise AppError(str(exc)) from exc
    return rules


def _packs(root: Path, packs: Iterable[str]) -> list[Pack]:
    search_paths = pack_search_paths_for(root)
    try:
        available: dict[str, Pack] = discover_packs(search_paths)
    except PluginError as exc:
        raise AppError(str(exc)) from exc
    found: list[Pack] = []
    for name in packs:
        pack = available.get(name)
        if pack is None:
            searched = ", ".join(str(p) for p in search_paths)
            raise AppError(f"pack {name!r} not found; searched: {searched}")
        found.append(pack)
    return found


def targets_for_packs(root: Path, packs: Iterable[str]) -> TargetRegistry:
    """The named targets of the packs named `packs` (`<pack>/targets/*.yml`)."""
    try:
        return TargetRegistry(
            spec for pack in _packs(root, packs) for spec in load_pack_targets(pack)
        )
    except TargetError as exc:
        raise AppError(str(exc)) from exc


def load_targets(ctx: AppContext) -> TargetRegistry:
    """The named targets of the profile's packs."""
    return targets_for_packs(ctx.root, ctx.require_profile().profile.packs)


def _uses_layout(rules: Iterable[RuleSpec]) -> bool:
    return any(
        "{layout." in text
        for rule in rules
        for text in (*rule.outputs, *(i.selector for i in rule.inputs))
    )


class _ProfileForeach:
    """The project's `ForeachResolver`: `blocks`, and the selectors over approved plans.

    `blocks` expands to one entry `{"block": b}` per key of `profile.blocks`, sorted,
    with the block's layout directories as template values (`{layout.rtl}`,
    `{layout.tb}`, `chipgraph.packs.digital_rtl.layout`) when a rule uses them. From the
    `digital-rtl` pack: `plan.modules` expands to one entry per module of every block's
    approved plan (each with the module's write set, its dependencies as ordering edges,
    the approved plan and the style files as inputs), `plan.block` to one entry per block
    with an approved plan and `plan.blocks` to one per block (both with the plan's files
    and the testbench as inputs, when it is approved). Any other `foreach` expression
    needs the Design Model, which arrives in M1.
    """

    def __init__(
        self,
        ctx: AppContext,
        rules: Sequence[RuleSpec],
        plans: Callable[[], Any] | None = None,
    ) -> None:
        self._ctx = ctx
        self._blocks = sorted(ctx.require_profile().profile.blocks)
        self._layout = _uses_layout(rules)
        self._plans_factory = plans
        self._plans: Any = None

    def _vars(self, block: str) -> dict[str, str]:
        if not self._layout:
            return {}
        from chipgraph.packs.digital_rtl.layout import LayoutError, layout_vars

        try:
            return layout_vars(self._ctx.require_profile(), block)
        except LayoutError as exc:
            raise AppError(str(exc)) from exc

    def expand(self, expr: str) -> list[dict[str, str]]:
        return [dict(entry.params) for entry in self.expand_entries(expr)]

    def expand_entries(self, expr: str) -> list[ForeachEntry]:
        if expr == "blocks":
            return [
                ForeachEntry(params={"block": block}, vars=self._vars(block))
                for block in self._blocks
            ]
        if expr in PLAN_SELECTORS and self._plans_factory is not None:
            if self._plans is None:  # once per graph, however many rules use it
                self._plans = self._plans_factory()
            if expr == PLAN_MODULES:
                return list(self._plans.modules())
            if expr == PLAN_BLOCK:
                return list(self._plans.blocks_with_plan())
            return list(self._plans.every_block())
        raise AppError(f"foreach {expr!r} needs the Design Model (M1)")


def _resolver_for(ctx: AppContext, rules: Iterable[RuleSpec] | None = None) -> ForeachResolver:
    """The project's resolver for a graph of `rules` (default: the profile's packs' rules)."""
    rules = load_rules(ctx) if rules is None else list(rules)
    plans: Callable[[], Any] | None = None
    if any(rule.foreach in PLAN_SELECTORS for rule in rules):
        from chipgraph.packs.digital_rtl.plan.resolver import PlanExpansion

        def _plans() -> PlanExpansion:
            return PlanExpansion(ctx, rules)

        plans = _plans
    return _ProfileForeach(ctx, rules, plans)


def project_graph(ctx: AppContext, rules: Sequence[RuleSpec] | None = None) -> BuildGraph:
    """The project's build graph: the packs' rules, expanded, with the packs' targets."""
    rules = load_rules(ctx) if rules is None else list(rules)
    try:
        return build_graph(
            rules, _resolver_for(ctx, rules), repo=ctx.store.repo, targets=load_targets(ctx)
        )
    except GraphError as exc:
        raise AppError(str(exc)) from exc


def graph_artifacts(graph: BuildGraph) -> list[ArtifactRef]:
    """Every input and output of every instance of `graph`."""
    return [ref for inst in graph.instances.values() for ref in (*inst.inputs, *inst.outputs)]


def denied_reads_for(
    ctx: AppContext, graph: BuildGraph | None, role: str | None, keep: Iterable[str] = ()
) -> tuple[str, ...]:
    """What a task of `role` must not read in this project (`task_denied_reads`).

    The one computation both runtimes use: the in-process loop filters the redo text by
    it, runtime `claude-code` sets it on the task for the guard and `submit`.
    """
    return task_denied_reads(
        role,
        artifacts=graph_artifacts(graph) if graph is not None else (),
        profile=ctx.require_profile().profile,
        keep=keep,
    )


def agent_executor(ctx: AppContext, graph: BuildGraph | None = None) -> Executor:
    """The executor for `kind: agent` rules, chosen by the profile's `runtime` (D35).

    `claude-code` queues each agent task for the user's Claude Code session (the plugin
    command runs it and hands it back over MCP). Any other runtime runs in-process: when
    an `AgentRuntime` plugin is registered under the profile's runtime name, its agent
    rules run in the engine's loop (`AgentRuleExecutor`: write, check at once, triage,
    retry within the budget, DESIGN.md 5.2). No such plugin ships yet (the API runtimes
    arrive later), so without one agent rules fail with the scheduler's `AgentStub`.
    """
    profile = ctx.require_profile().profile
    if profile.runtime == "claude-code":
        return ClaudeCodeExecutor(ClaudeCodeRuntime(TaskQueue(ctx.layout), ctx.store))
    if profile.runtime in ctx.registry.names("runtime"):
        runtime = ctx.registry.get("runtime", profile.runtime)

        def denied(rule: RuleSpec, instance: RuleInstance) -> tuple[str, ...]:
            keep = [ref.path for ref in instance.outputs if ref.path is not None]
            return denied_reads_for(ctx, graph, rule.role, keep)

        return AgentRuleExecutor(runtime, store=ctx.store, layout=ctx.layout, denied_reads=denied)
    return AgentStub()


def gate_checker(ctx: AppContext) -> GateChecker:
    """The gates of this project: PR gates from GitHub reviews with `review: github`.

    With `review: {use: github}` a gate whose id starts with `pr:` is the pull request's
    review (`GitHubGateChecker`, pinned to the PR's head commit); any other gate, and
    every gate otherwise, is the decision files' (`GateEvaluator`, `chipgraph approve`).
    """
    if ctx.pr_review is not None:
        return GitHubGateChecker(ctx.pr_review, fallback=ctx.gates)
    return ctx.gates


def make_scheduler(
    ctx: AppContext, target: str, *, concurrency: int = 4, tracer: Tracer | None = None
) -> Scheduler:
    """Build a `Scheduler` ready to run/resume `target` for this project."""
    graph = project_graph(ctx)
    try:
        graph.select(target)
    except GraphError as exc:
        raise AppError(str(exc)) from exc
    try:  # every agent rule must fit its role's tool table (DESIGN.md 5.1)
        check_agent_rules(graph.rules, graph.instances.values())
    except RoleError as exc:
        raise AppError(str(exc)) from exc

    run_executor = RunExecutor(ctx)
    executors = {
        "gen": run_executor,
        "import": run_executor,
        "human": HumanExecutor(ctx),
        "agent": agent_executor(ctx, graph),
    }
    return Scheduler(
        graph,
        layout=ctx.layout,
        store=ctx.store,
        executors=executors,  # type: ignore[arg-type]
        checks=ProfileCheckRunner(ctx),
        gates=gate_checker(ctx),
        concurrency=concurrency,
        tracer=tracer,
    )


__all__ = [
    "PLAN_BLOCK",
    "PLAN_BLOCKS",
    "PLAN_MODULES",
    "PLAN_SELECTORS",
    "agent_executor",
    "denied_reads_for",
    "gate_checker",
    "graph_artifacts",
    "load_rules",
    "load_targets",
    "make_scheduler",
    "pack_search_paths",
    "pack_search_paths_for",
    "project_graph",
    "rules_for_packs",
    "targets_for_packs",
]

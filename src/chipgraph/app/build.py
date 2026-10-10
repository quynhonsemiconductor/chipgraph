"""Building a `Scheduler` for a target: load rules from the profile's packs, expand
`foreach` over the profile's blocks, and wire up the executors, check runner and gate
evaluator (DESIGN.md 3.3, 3.4).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from importlib import resources
from pathlib import Path

from chipgraph.adapters.runtime.claude_code import ClaudeCodeExecutor, ClaudeCodeRuntime
from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.app.executors import HumanExecutor, RunExecutor
from chipgraph.core.contracts import RuleSpec
from chipgraph.core.engine.agent_rule import AgentRuleExecutor
from chipgraph.core.engine.graph import ForeachEntry, ForeachResolver, GraphError, build_graph
from chipgraph.core.engine.rules import RuleLoadError, load_pack_rules
from chipgraph.core.engine.scheduler import AgentStub, Executor, Scheduler
from chipgraph.core.plugin_api.pack import Pack, discover_packs
from chipgraph.core.plugin_api.registry import PluginError
from chipgraph.core.runtime import TaskQueue
from chipgraph.core.runtime.roles import RoleError, check_agent_rules
from chipgraph.core.state.trace import Tracer

PLAN_MODULES = "plan.modules"
"""The `foreach` selector over the modules of every block's approved plan (M2-03)."""


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
    search_paths = pack_search_paths_for(root)
    try:
        available: dict[str, Pack] = discover_packs(search_paths)
    except PluginError as exc:
        raise AppError(str(exc)) from exc

    rules: list[RuleSpec] = []
    for name in packs:
        pack = available.get(name)
        if pack is None:
            searched = ", ".join(str(p) for p in search_paths)
            raise AppError(f"pack {name!r} not found; searched: {searched}")
        try:
            rules.extend(load_pack_rules(pack))
        except RuleLoadError as exc:
            raise AppError(str(exc)) from exc
    return rules


class _ProfileForeach:
    """The project's `ForeachResolver`: `blocks`, and `plan.modules`.

    `blocks` expands to one param set `{"block": b}` per key of `profile.blocks`,
    sorted. `plan.modules` expands to one entry per module of every block's approved
    plan (`plan_modules`, from the `digital-rtl` pack; each entry carries the module's
    write set, its dependencies as ordering edges and the approved plan as an input).
    Any other `foreach` expression needs the Design Model, which arrives in M1.
    """

    def __init__(
        self,
        block_names: Iterable[str],
        plan_modules: Callable[[], list[ForeachEntry]] | None = None,
    ) -> None:
        self._blocks = sorted(block_names)
        self._plan_modules = plan_modules
        self._plan_entries: list[ForeachEntry] | None = None

    def expand(self, expr: str) -> list[dict[str, str]]:
        return [dict(entry.params) for entry in self.expand_entries(expr)]

    def expand_entries(self, expr: str) -> list[ForeachEntry]:
        if expr == "blocks":
            return [ForeachEntry(params={"block": block}) for block in self._blocks]
        if expr == PLAN_MODULES and self._plan_modules is not None:
            if self._plan_entries is None:  # once per graph, however many rules use it
                self._plan_entries = self._plan_modules()
            return list(self._plan_entries)
        raise AppError(f"foreach {expr!r} needs the Design Model (M1)")


def _resolver_for(ctx: AppContext, rules: Iterable[RuleSpec] | None = None) -> ForeachResolver:
    """The project's resolver for a graph of `rules` (default: the profile's packs' rules)."""
    rules = load_rules(ctx) if rules is None else list(rules)
    plan_modules: Callable[[], list[ForeachEntry]] | None = None
    if any(rule.foreach == PLAN_MODULES for rule in rules):
        from chipgraph.packs.digital_rtl.plan.resolver import plan_modules as expand_plans

        def _expand() -> list[ForeachEntry]:
            return expand_plans(ctx, rules)

        plan_modules = _expand
    return _ProfileForeach(ctx.require_profile().profile.blocks, plan_modules)


def agent_executor(ctx: AppContext) -> Executor:
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
        return AgentRuleExecutor(runtime, store=ctx.store, layout=ctx.layout)
    return AgentStub()


def make_scheduler(
    ctx: AppContext, target: str, *, concurrency: int = 4, tracer: Tracer | None = None
) -> Scheduler:
    """Build a `Scheduler` ready to run/resume `target` for this project."""
    rules = load_rules(ctx)
    resolver = _resolver_for(ctx, rules)
    try:
        graph = build_graph(rules, resolver, repo=ctx.store.repo)
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
        "agent": agent_executor(ctx),
    }
    return Scheduler(
        graph,
        layout=ctx.layout,
        store=ctx.store,
        executors=executors,  # type: ignore[arg-type]
        checks=ProfileCheckRunner(ctx),
        gates=ctx.gates,
        concurrency=concurrency,
        tracer=tracer,
    )


__all__ = [
    "PLAN_MODULES",
    "agent_executor",
    "load_rules",
    "make_scheduler",
    "pack_search_paths",
    "pack_search_paths_for",
    "rules_for_packs",
]

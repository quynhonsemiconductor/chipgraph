"""Building a `Scheduler` for a target: load rules from the profile's packs, expand
`foreach` over the profile's blocks, and wire up the executors, check runner and gate
evaluator (DESIGN.md 3.3, 3.4).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from importlib import resources
from pathlib import Path

from chipgraph.adapters.runtime.claude_code import ClaudeCodeRuntime
from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.app.executors import HumanExecutor, RunExecutor
from chipgraph.core.contracts import RuleSpec
from chipgraph.core.engine.graph import ForeachResolver, GraphError, build_graph
from chipgraph.core.engine.rules import RuleLoadError, load_pack_rules
from chipgraph.core.engine.scheduler import AgentStub, Executor, Scheduler
from chipgraph.core.plugin_api.pack import Pack, discover_packs
from chipgraph.core.plugin_api.registry import PluginError
from chipgraph.core.runtime import AgentRuntimeExecutor, TaskQueue
from chipgraph.core.runtime.roles import RoleError, check_agent_rules
from chipgraph.core.state.trace import Tracer


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
    paths = [ctx.root / ".chipgraph" / "packs"]
    builtin = builtin_packs_dir()
    if builtin is not None:
        paths.append(builtin)
    env_value = os.environ.get("CHIPGRAPH_PACK_PATH", "")
    paths.extend(Path(p) for p in env_value.split(os.pathsep) if p)
    return paths


def load_rules(ctx: AppContext) -> list[RuleSpec]:
    """Load every rule from the packs named in the profile's `packs`."""
    resolved = ctx.require_profile()
    search_paths = pack_search_paths(ctx)
    try:
        available: dict[str, Pack] = discover_packs(search_paths)
    except PluginError as exc:
        raise AppError(str(exc)) from exc

    rules: list[RuleSpec] = []
    for name in resolved.profile.packs:
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
    """The M0 `ForeachResolver`: only the `blocks` expression is supported.

    It expands to one param set `{"block": b}` per key of `profile.blocks`, sorted.
    Any other `foreach` expression needs the Design Model, which arrives in M1.
    """

    def __init__(self, block_names: Iterable[str]) -> None:
        self._blocks = sorted(block_names)

    def expand(self, expr: str) -> list[dict[str, str]]:
        if expr != "blocks":
            raise AppError(f"foreach {expr!r} needs the Design Model (M1)")
        return [{"block": block} for block in self._blocks]


def _resolver_for(ctx: AppContext) -> ForeachResolver:
    return _ProfileForeach(ctx.require_profile().profile.blocks)


def agent_executor(ctx: AppContext) -> Executor:
    """The executor for `kind: agent` rules, chosen by the profile's `runtime` (D35).

    `claude-code` queues each agent task for the user's Claude Code session (the plugin
    command runs it and hands it back over MCP). The API runtimes arrive in M1-11b;
    until then their agent rules fail with the scheduler's `AgentStub` message.
    """
    if ctx.require_profile().profile.runtime == "claude-code":
        return AgentRuntimeExecutor(ClaudeCodeRuntime(TaskQueue(ctx.layout), ctx.store))
    return AgentStub()


def make_scheduler(
    ctx: AppContext, target: str, *, concurrency: int = 4, tracer: Tracer | None = None
) -> Scheduler:
    """Build a `Scheduler` ready to run/resume `target` for this project."""
    rules = load_rules(ctx)
    resolver = _resolver_for(ctx)
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


__all__ = ["agent_executor", "load_rules", "make_scheduler", "pack_search_paths"]

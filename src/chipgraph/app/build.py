"""Building a `Scheduler` for a target: load rules from the profile's packs, expand
`foreach` over the profile's blocks, and wire up the executors, check runner and gate
evaluator (DESIGN.md 3.3, 3.4).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.app.executors import HumanExecutor, RunExecutor
from chipgraph.core.contracts import RuleSpec
from chipgraph.core.engine.graph import ForeachResolver, GraphError, build_graph
from chipgraph.core.engine.rules import RuleLoadError, load_pack_rules
from chipgraph.core.engine.scheduler import AgentStub, Scheduler
from chipgraph.core.plugin_api.pack import Pack, discover_packs
from chipgraph.core.plugin_api.registry import PluginError


def _chipgraph_repo_root() -> Path | None:
    """The root of the chipgraph repository itself (`packs/` next to `pyproject.toml`), if any.

    `None` when chipgraph is installed as a package with no `packs/` sibling (e.g. from
    a wheel), in which case only a project's own packs and `$CHIPGRAPH_PACK_PATH` apply.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "packs").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def pack_search_paths(ctx: AppContext) -> list[Path]:
    """The search paths packs are discovered from, in order (DESIGN.md 3.2)."""
    paths = [ctx.root / ".chipgraph" / "packs"]
    chipgraph_root = _chipgraph_repo_root()
    if chipgraph_root is not None:
        paths.append(chipgraph_root / "packs")
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


def make_scheduler(ctx: AppContext, target: str, *, concurrency: int = 4) -> Scheduler:
    """Build a `Scheduler` ready to run/resume `target` for this project."""
    rules = load_rules(ctx)
    resolver = _resolver_for(ctx)
    try:
        graph = build_graph(rules, resolver, repo=ctx.store.repo)
        graph.select(target)
    except GraphError as exc:
        raise AppError(str(exc)) from exc

    run_executor = RunExecutor(ctx)
    executors = {
        "gen": run_executor,
        "import": run_executor,
        "human": HumanExecutor(ctx),
        "agent": AgentStub(),
    }
    return Scheduler(
        graph,
        layout=ctx.layout,
        store=ctx.store,
        executors=executors,  # type: ignore[arg-type]
        checks=ProfileCheckRunner(ctx),
        gates=ctx.gates,
        concurrency=concurrency,
    )


__all__ = ["load_rules", "make_scheduler", "pack_search_paths"]

"""`ProfileCheckRunner`: maps a check id to a project's `.chipgraph.yml` adapter config
and runs it, satisfying the scheduler's `CheckRunner` protocol.

Backs both `chipgraph check` (run directly, no run id, no caching) and the `checks=`
argument to `Scheduler` (see `app/build.py`). A run id may be set after construction
(`chipgraph resume RUN_ID` knows its run id ahead of time); when it is set, results are
cached in that run's `IdempotencyStore`.
"""

from __future__ import annotations

from chipgraph.app.context import AppContext
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue, RuleInstance
from chipgraph.core.plugin_api.protocols import Check, ToolAdapter
from chipgraph.core.plugin_api.registry import PluginError
from chipgraph.core.plugin_api.types import ToolContext
from chipgraph.core.state.idempotency import IdempotencyStore, make_key


class ProfileCheckRunner:
    """Runs a check id through the adapter `.chipgraph.yml` configures for it."""

    def __init__(self, ctx: AppContext, *, run_id: str | None = None) -> None:
        self.ctx = ctx
        self.run_id = run_id

    async def run(self, check_id: str, instance: RuleInstance) -> CheckResult:
        resolved = self.ctx.require_profile()
        block = instance.params.get("block")
        profile = resolved.for_block(block) if block is not None else resolved.profile

        cfg = profile.adapters.get(check_id)
        if cfg is None:
            return CheckResult(
                check_id=check_id,
                status="error",
                issues=(
                    Issue(
                        severity="error",
                        msg=(
                            f"no adapter configured for check {check_id!r}; "
                            f"configured adapters: {sorted(profile.adapters)}"
                        ),
                    ),
                ),
                duration_s=0.0,
                idempotency_key=make_key({"check_id": check_id, "error": "unknown-check"}),
            )

        plugin = self._resolve_plugin(cfg.use)
        if plugin is None:
            return CheckResult(
                check_id=check_id,
                status="error",
                issues=(
                    Issue(
                        severity="error",
                        msg=(
                            f"no check or tool adapter named {cfg.use!r} "
                            f"(configured for check {check_id!r})"
                        ),
                    ),
                ),
                duration_s=0.0,
                idempotency_key=make_key(
                    {"check_id": check_id, "error": "unknown-adapter", "use": cfg.use}
                ),
            )

        args = {k: v for k, v in cfg.model_dump().items() if k != "use"}
        spec = CheckSpec(id=check_id, capability=check_id, adapter=cfg.use, args=args)
        tool_ctx = ToolContext(
            repo_root=self.ctx.root,
            runner=self.ctx.registry.get("runner", "local"),
            env=dict(profile.env.vars),
            params=dict(instance.params),
        )

        result = await plugin.run(spec, tool_ctx)

        if self.run_id is not None:
            IdempotencyStore(self.ctx.layout, self.run_id).put(result)

        return result

    def _resolve_plugin(self, use: str) -> Check | ToolAdapter | None:
        for kind in ("check", "tool"):
            try:
                return self.ctx.registry.get(kind, use)  # type: ignore[no-any-return]
            except PluginError:
                continue
        return None


__all__ = ["ProfileCheckRunner"]

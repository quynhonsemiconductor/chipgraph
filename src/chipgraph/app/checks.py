"""`ProfileCheckRunner`: maps a check id to a project's `.chipgraph.yml` adapter config
and runs it, satisfying the scheduler's `CheckRunner` protocol.

Backs both `chipgraph check` (run directly, no run id, no caching) and the `checks=`
argument to `Scheduler` (see `app/build.py`). A run id may be set after construction
(`chipgraph resume RUN_ID` knows its run id ahead of time); when it is set, results are
cached in that run's `IdempotencyStore`.
"""

from __future__ import annotations

from chipgraph.app.context import AppContext
from chipgraph.app.findings import findings_from_check, layer_for_check
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue, RuleInstance
from chipgraph.core.plugin_api.protocols import Check, ToolAdapter
from chipgraph.core.plugin_api.registry import PluginError
from chipgraph.core.plugin_api.types import ToolContext
from chipgraph.core.state.artifacts import hash_inputs
from chipgraph.core.state.findings import FindingStore
from chipgraph.core.state.idempotency import IdempotencyStore, make_key


class ProfileCheckRunner:
    """Runs a check id through the adapter `.chipgraph.yml` configures for it."""

    def __init__(self, ctx: AppContext, *, run_id: str | None = None) -> None:
        self.ctx = ctx
        self.run_id = run_id
        self.finding_store = FindingStore(ctx.layout)

    async def run(self, check_id: str, instance: RuleInstance) -> CheckResult:
        resolved = self.ctx.require_profile()
        block = instance.params.get("block")
        profile = resolved.for_block(block) if block is not None else resolved.profile

        cfg = profile.adapters.get(check_id)
        if cfg is None:
            return self._record(
                check_id,
                CheckResult(
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
                ),
            )

        plugin = self._resolve_plugin(cfg.use)
        if plugin is None:
            return self._record(
                check_id,
                CheckResult(
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

        # Idempotent resume (DESIGN.md 6.2): a `pass` result is reused when the same
        # check runs again in the same run with the same files. Only plugins with a
        # `key_for(spec, ctx)` (today `CmdTool`) can be keyed before running; the
        # built-in checks fold the files they read into their key inside `run()`, so
        # they always run. The cache key adds the hashes of the instance's inputs and
        # outputs, so editing a file between a kill and `resume` runs the check again.
        # Failures are never reused: a fixed file must be checked again.
        store = IdempotencyStore(self.ctx.layout, self.run_id) if self.run_id is not None else None
        key_for = getattr(plugin, "key_for", None)
        cache_key: str | None = None
        if store is not None and callable(key_for):
            refs = [r for r in (*instance.inputs, *instance.outputs) if r.path is not None]
            files = self.ctx.store.current_hashes(refs)
            cache_key = make_key(
                {"check": str(key_for(spec, tool_ctx)), "files": hash_inputs(files.items())}
            )
            cached = store.get(cache_key)
            if cached is not None and cached.ok:
                return self._record(check_id, cached)

        result = await plugin.run(spec, tool_ctx)

        if store is not None and cache_key is not None and result.ok:
            store.put(result.model_copy(update={"idempotency_key": cache_key}))

        return self._record(check_id, result)

    def _record(self, check_id: str, result: CheckResult) -> CheckResult:
        """Record `result`'s issues as findings, never failing the check for it.

        Only an `OSError` writing to the finding store is swallowed; anything else
        (e.g. a bug building a `Finding`) is a real error and propagates.
        """
        try:
            layer = layer_for_check(check_id, result.check_id)
            findings = findings_from_check(
                result, layer=layer, store=self.ctx.store, run_id=self.run_id
            )
            if findings:
                self.finding_store.upsert(findings)
        except OSError:
            pass
        return result

    def _resolve_plugin(self, use: str) -> Check | ToolAdapter | None:
        for kind in ("check", "tool"):
            try:
                return self.ctx.registry.get(kind, use)  # type: ignore[no-any-return]
            except PluginError:
                continue
        return None


__all__ = ["ProfileCheckRunner"]

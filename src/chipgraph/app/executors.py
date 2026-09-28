"""`Executor`s for the scheduler: one per `RuleKind` (DESIGN.md 3.3, 3.4).

`gen` and `import` rules run through a tool adapter (`rule.run`); `human` rules just
check whether their outputs exist yet; `agent` rules use the scheduler's own
`AgentStub` (agent rules arrive in M2).
"""

from __future__ import annotations

from chipgraph.app.context import AppContext
from chipgraph.core.contracts import CheckSpec, RuleInstance, RuleSpec
from chipgraph.core.contracts.types import FailureLabel
from chipgraph.core.engine.scheduler import ExecOutcome
from chipgraph.core.plugin_api.registry import PluginError
from chipgraph.core.plugin_api.types import ToolContext


class RunExecutor:
    """Runs a `gen`/`import` rule's `run` spec through a tool adapter."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        if rule.run is None:
            return ExecOutcome(
                ok=False, failure_label="infra", message=f"rule {rule.id!r} has no 'run'"
            )

        try:
            tool = self.ctx.registry.get("tool", rule.run.use)
        except PluginError as exc:
            return ExecOutcome(ok=False, failure_label="infra", message=str(exc))

        spec = CheckSpec(
            id=rule.id, capability=rule.kind, adapter=rule.run.use, args=dict(rule.run.args)
        )
        env = dict(self.ctx.profile.env.vars) if self.ctx.profile is not None else {}
        tool_ctx = ToolContext(
            repo_root=self.ctx.root,
            runner=self.ctx.registry.get("runner", "local"),
            env=env,
            params=dict(instance.params),
        )

        result = await tool.run(spec, tool_ctx)
        if result.status != "pass":
            label: FailureLabel = "verification" if result.status == "fail" else "infra"
            message = (
                "; ".join(issue.msg for issue in result.issues)
                or result.log_tail
                or f"tool {rule.run.use!r} did not pass for rule {rule.id!r}"
            )
            return ExecOutcome(ok=False, failure_label=label, message=message)
        return ExecOutcome(ok=True)


class HumanExecutor:
    """A `human` rule 'runs' by having a person write its outputs by hand."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        missing = [
            ref.path
            for ref in instance.outputs
            if ref.path is not None and not self.ctx.store.exists(ref)
        ]
        if missing:
            return ExecOutcome(
                ok=False,
                failure_label="planning",
                message=f"waiting for a person to write {', '.join(missing)}",
            )
        return ExecOutcome(ok=True)


__all__ = ["HumanExecutor", "RunExecutor"]

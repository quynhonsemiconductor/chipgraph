"""A local plugin that registers the same (kind, name) twice, to test duplicate handling.

The second `register` must be refused by the registry (surfaced as a `PluginError` naming
this file).
"""

from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api.types import ToolContext


class _DupCheck:
    id = "dup_check"
    name = "dup"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        return CheckResult(
            check_id=spec.id, status="pass", issues=(), duration_s=0.0, idempotency_key="dup"
        )


def register(api: object) -> None:
    api.register("check", "dup_check", _DupCheck)  # type: ignore[attr-defined]
    api.register("check", "dup_check", _DupCheck)  # type: ignore[attr-defined]

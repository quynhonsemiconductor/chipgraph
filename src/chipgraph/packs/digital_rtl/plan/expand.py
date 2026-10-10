"""`PlanExpandTool`: the `plan_expand` tool of rule `digital-rtl/plan_expand`.

The rule is gated by `plan:{block}`, so the scheduler runs this only once a person has
approved the plan at its current hash. It checks the plan once more (all of
`plan_check`, existing files included) and writes the approved plan artifact
(`plan/<block>.approved.json` by default): the block, the plan's path and hash, the
modules in build order with their dependencies and write sets. Every `plan.modules`
instance takes that artifact as an input, so it runs after this step.

Rule args (both `{block}` templates): `plan` (default `plan/{block}.plan.yml`) and
`out` (default `plan/{block}.approved.json`).
"""

from __future__ import annotations

import json
import time

from chipgraph.checks._common import compute_idempotency_key
from chipgraph.checks.plan_check import (
    DEFAULT_PLAN_PATH,
    PlanEnvError,
    check_plan_file,
    topo_order,
)
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.plugin_api.types import ToolContext

DEFAULT_APPROVED_PATH = "plan/{block}.approved.json"


def _result(spec: CheckSpec, status: str, issues: tuple[Issue, ...], start: float) -> CheckResult:
    return CheckResult(
        check_id=spec.id,
        status=status,  # type: ignore[arg-type]
        issues=issues,
        duration_s=time.monotonic() - start,
        idempotency_key=compute_idempotency_key(spec.id, spec.args, ()),
    )


class PlanExpandTool:
    """Writes a block's approved plan artifact once its gate is approved."""

    name = "plan_expand"
    capability = "gen"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        block = ctx.params.get("block")
        plan_t = spec.args.get("plan", DEFAULT_PLAN_PATH)
        out_t = spec.args.get("out", DEFAULT_APPROVED_PATH)
        if not block or not isinstance(plan_t, str) or not isinstance(out_t, str):
            msg = "plan_expand needs a block param and string 'plan'/'out' templates"
            return _result(spec, "error", (Issue(msg=msg),), start)
        try:
            report = check_plan_file(ctx.repo_root, block, plan_path=plan_t)
        except PlanEnvError as exc:
            return _result(spec, "error", (Issue(rule="plan.env", msg=str(exc)),), start)
        if not report.exists:
            issue = Issue(file=report.path, rule="plan.missing", msg=f"no plan at {report.path}")
            return _result(spec, "fail", (issue,), start)
        if not report.ok or report.plan is None:
            errors = tuple(i for i in report.issues if i.severity == "error")
            return _result(spec, "fail", errors, start)

        plan = report.plan
        order = topo_order(plan)
        artifact = {
            "schema_version": 1,
            "block": block,
            "plan": report.path,
            "plan_sha256": report.sha256,
            "top": plan.top,
            "order": order,
            "modules": [
                {
                    "name": name,
                    "depends_on": list(module.depends_on),
                    "writes": list(module.write_paths),
                    "budget": module.budget.model_dump(mode="json"),
                }
                for name in order
                if (module := plan.module(name)) is not None
            ],
        }
        out = ctx.repo_root / out_t.replace("{block}", block)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        warnings = tuple(i for i in report.issues if i.severity != "error")
        return _result(spec, "pass", warnings, start)


__all__ = ["DEFAULT_APPROVED_PATH", "PlanExpandTool"]

"""A sample local plugin: a layout check whose rule is a formula, not a data template.

It registers a `check` named `tiny_layout` that accepts every `design/<block>/<module>.sv`
file only when `<module>` starts with `<block>`. That relationship between two path
segments cannot be written with the data-only `layout` check (which matches templates and
checks each placeholder against an independent list of allowed values), so it demonstrates
V5: a project drops to a small Python function when a convention is a formula.
"""

from __future__ import annotations

import re
import time

from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.plugin_api.types import ToolContext

_PATH = re.compile(r"design/(?P<block>[^/]+)/(?P<module>[^/]+)\.sv$")


class TinyLayoutCheck:
    """Every `design/<block>/<module>.sv` must have a module name prefixed by its block."""

    id = "tiny_layout"
    name = "Tiny layout (formula)"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        issues: list[Issue] = []
        for path in sorted(p.relative_to(ctx.repo_root).as_posix() for p in _sv_files(ctx)):
            match = _PATH.search(path)
            if match is None:
                issues.append(
                    Issue(file=path, rule="tiny_layout", severity="error", msg="not a design file")
                )
                continue
            block, module = match["block"], match["module"]
            if not module.startswith(block):
                issues.append(
                    Issue(
                        file=path,
                        rule="tiny_layout",
                        severity="error",
                        msg=f"module {module!r} must start with block {block!r}",
                    )
                )
        status = "fail" if issues else "pass"
        return CheckResult(
            check_id=spec.id,
            status=status,
            issues=tuple(issues),
            duration_s=time.monotonic() - start,
            idempotency_key=f"tiny_layout:{len(issues)}",
        )


def _sv_files(ctx: ToolContext) -> list:
    return [p for p in ctx.repo_root.rglob("*.sv") if p.is_file()]


def register(api: object) -> None:
    """Register the check with the host, as the local-plugin API requires."""
    api.register("check", "tiny_layout", TinyLayoutCheck)  # type: ignore[attr-defined]

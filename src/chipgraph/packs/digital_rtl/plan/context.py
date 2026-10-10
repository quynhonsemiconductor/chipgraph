"""What a `planner` task gets in its context, on top of its inputs (DESIGN.md 5.1, 5.3).

So the reply is a valid plan the first time: the block's slice of the Design Model
(requirements, interface ports, existing modules and their ports), the layout paths a
module may write, the files that already exist there and those other rules produce,
the files each module's writes must include, the limits, the naming rule for module
names, and the plan file's JSON Schema.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from chipgraph.checks._common import compile_template, iter_repo_files
from chipgraph.checks.plan_check import PlanEnv, PlanEnvError, load_env
from chipgraph.core.config.loader import ResolvedProfile
from chipgraph.core.contracts import RuleSpec
from chipgraph.packs.digital_rtl.plan.model import Plan


def _ports(ports: Sequence[Any]) -> list[dict[str, Any]]:
    return [{"name": p.name, "direction": p.direction, "width": p.width} for p in ports]


def _naming(env: PlanEnv) -> dict[str, Any] | None:
    if env.naming is None:
        return None
    module = env.naming.kind_patterns.get("module")
    return {
        "rules": env.naming_ref,
        "module": (
            {"rule": module[0], "pattern": module[1].pattern, "message": module[2]}
            if module is not None
            else None
        ),
        "vocabulary": dict(env.naming.vocabulary.replace) if env.naming.vocabulary else {},
    }


def planner_context(
    root: Path,
    resolved: ResolvedProfile,
    rules: Sequence[RuleSpec],
    block: str,
    *,
    plan_path: str,
) -> dict[str, Any]:
    """The extra context of block `block`'s planner task (`get_context` key `plan`)."""
    try:
        env = load_env(root, block, resolved=resolved, rules=rules)
    except PlanEnvError as exc:
        return {"block": block, "plan_path": plan_path, "note": str(exc)}
    layout = [compile_template(t) for t in env.layout]
    in_layout = [f for f in iter_repo_files(root) if any(t.match(f) is not None for t in layout)]
    taken = {**env.agent_outputs, **env.engine_outputs}
    return {
        "block": block,
        "plan_path": plan_path,
        "requirements": [
            {"id": r.ref, "declared": r.declared, "text": r.text} for r in env.requirements
        ],
        "interface": {"source": env.interface_source, "ports": _ports(env.interface)},
        "existing_modules": [
            {"name": m.name, "file": m.file, "ports": _ports(m.ports)} for m in env.existing_modules
        ],
        "layout": list(env.layout),
        "existing_files": [f for f in in_layout if f not in taken],
        "other_rules_write": sorted(
            p for p in taken if any(t.match(p) is not None for t in layout)
        ),
        "module_outputs": [
            {"rule": rule_id, "template": template} for rule_id, template in env.module_outputs
        ],
        "limits": env.limits.model_dump(mode="json"),
        "naming": _naming(env),
        "schema": Plan.model_json_schema(),
    }


__all__ = ["planner_context"]

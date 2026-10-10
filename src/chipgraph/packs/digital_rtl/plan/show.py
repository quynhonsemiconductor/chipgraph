"""`chipgraph plan show <block>`: what the approver of gate `plan:<block>` reads.

The summary: the gate's status (and the command that approves it), the check's errors
and warnings, the modules (summary, requirements, dependencies, write sets, ports,
budget) in build order, requirement coverage, the limits used, the assumptions and the
open questions.
"""

from __future__ import annotations

from typing import Any

from chipgraph.app.build import load_rules
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.checks.plan_check import (
    PlanEnvError,
    check_plan_file,
    dependency_depth,
    topo_order,
)
from chipgraph.packs.digital_rtl.plan.resolver import gate_status, plan_gates


def plan_summary(ctx: AppContext, block: str) -> dict[str, Any]:
    """The summary of `block`'s plan, as plain data (`--json` prints it as is)."""
    resolved = ctx.require_profile()
    if block not in resolved.profile.blocks:
        raise AppError(
            f"unknown block {block!r}; blocks: {', '.join(sorted(resolved.profile.blocks))}"
        )
    rules = load_rules(ctx)
    gate = plan_gates(rules, resolved.profile.blocks, repo=ctx.store.repo)[block]
    try:
        report = check_plan_file(
            ctx.root, block, plan_path=gate.plan_path, resolved=resolved, rules=rules
        )
    except PlanEnvError as exc:
        raise AppError(str(exc)) from exc
    status = gate_status(ctx, gate)
    summary: dict[str, Any] = {
        "block": block,
        "path": report.path,
        "exists": report.exists,
        "sha256": report.sha256,
        "gate": {
            "id": gate.gate_id,
            "status": status,
            "instance": gate.instance.instance_id,
            "approve": (
                f"chipgraph approve {gate.gate_id} --instance '{gate.instance.instance_id}'"
            ),
        },
        "ok": report.ok,
        "errors": [_issue(i) for i in report.issues if i.severity == "error"],
        "warnings": [_issue(i) for i in report.issues if i.severity == "warning"],
    }
    plan = report.plan
    if plan is None:
        return summary
    limits = resolved.profile.plan
    order = topo_order(plan)
    summary.update(
        {
            "top": plan.top,
            "order": order,
            "modules": [
                {
                    "name": m.name,
                    "summary": m.summary,
                    "reqs": list(m.reqs),
                    "depends_on": list(m.depends_on),
                    "writes": [w.model_dump(mode="json") for w in m.writes],
                    "ports": [p.model_dump(mode="json") for p in m.interface.ports],
                    "budget": m.budget.model_dump(mode="json"),
                }
                for name in order
                if (m := plan.module(name)) is not None
            ],
            "coverage": _coverage(report),
            "limits": {
                "modules": [len(plan.modules), limits.max_modules],
                "depth": [dependency_depth(plan), limits.max_depth],
                "tries": [sum(m.budget.tries for m in plan.modules), limits.max_total_tries],
            },
            "assumptions": list(plan.assumptions),
            "open_questions": list(plan.open_questions),
        }
    )
    return summary


def _issue(issue: Any) -> str:
    where = f"{issue.file}:{issue.line}" if issue.line else str(issue.file)
    return f"{where} [{issue.rule}] {issue.msg}"


def _coverage(report: Any) -> list[dict[str, Any]]:
    """Each block requirement: the modules covering it, or why it is unassigned, or missing."""
    plan = report.plan
    env = report.env
    if plan is None or env is None:
        return []
    rows: list[dict[str, Any]] = []
    for req in env.requirements:
        modules = [m.name for m in plan.modules if req.refs & set(m.reqs)]
        reasons = [u.reason for u in plan.unassigned_reqs if u.req in req.refs]
        rows.append(
            {
                "req": req.ref,
                "modules": modules,
                "unassigned": reasons[0] if reasons and not modules else None,
            }
        )
    return rows


def render_text(summary: dict[str, Any]) -> str:
    """The summary as text, for a person."""
    lines = [f"plan for block {summary['block']}: {summary['path']}"]
    gate = summary["gate"]
    lines.append(f"gate {gate['id']}: {gate['status']}")
    if not summary["exists"]:
        lines.append("no plan yet: build rule digital-rtl/plan for this block first")
        return "\n".join(lines) + "\n"
    lines.append(f"check: {'ok' if summary['ok'] else 'FAILS'}")
    for error in summary["errors"]:
        lines.append(f"  error   {error}")
    for warning in summary["warnings"]:
        lines.append(f"  warning {warning}")
    if "modules" not in summary:
        return "\n".join(lines) + "\n"

    limits = summary["limits"]
    lines.append(
        f"top: {summary['top']}   modules {limits['modules'][0]}/{limits['modules'][1]}, "
        f"depth {limits['depth'][0]}/{limits['depth'][1]}, "
        f"tries {limits['tries'][0]}/{limits['tries'][1]}"
    )
    lines.append("")
    lines.append("modules (build order):")
    for m in summary["modules"]:
        lines.append(f"  {m['name']}: {m['summary']}")
        lines.append(f"    reqs:       {', '.join(m['reqs']) or '-'}")
        lines.append(f"    depends on: {', '.join(m['depends_on']) or '-'}")
        writes = [w["path"] + (" (replaces)" if w["replaces"] else "") for w in m["writes"]]
        lines.append(f"    writes:     {', '.join(writes)}")
        ports = [
            f"{p['name']}:{p['direction']}[{p['width']}]" + (" internal" if p["internal"] else "")
            for p in m["ports"]
        ]
        lines.append(f"    ports:      {', '.join(ports) or '-'}")
        lines.append(f"    budget:     {m['budget']['tries']} tries, tier {m['budget']['tier']}")
    lines.append("")
    lines.append("requirement coverage:")
    for row in summary["coverage"]:
        if row["modules"]:
            lines.append(f"  {row['req']}: {', '.join(row['modules'])}")
        elif row["unassigned"]:
            lines.append(f"  {row['req']}: unassigned ({row['unassigned']})")
        else:
            lines.append(f"  {row['req']}: NOT COVERED")
    if not summary["coverage"]:
        lines.append("  (the block has no requirements in the Design Model)")
    lines.append("")
    lines.append("assumptions:")
    lines.extend(f"  - {a}" for a in summary["assumptions"] or ["none"])
    lines.append("open questions (read before approving):")
    lines.extend(f"  - {q}" for q in summary["open_questions"] or ["none"])
    if gate["status"] != "approved":
        lines.append("")
        lines.append(f"approve: {gate['approve']}")
    return "\n".join(lines) + "\n"


__all__ = ["plan_summary", "render_text"]

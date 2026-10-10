"""`foreach: plan.modules`: one rule instance per module of an approved plan (DESIGN.md 5.3).

The gate `plan:<block>` hangs on the `gen` rule `digital-rtl/plan_expand`, whose only
file input is the plan: approving it pins the plan file's hash, and editing the plan
afterwards puts the gate back to waiting (the existing gate behaviour, `gate.py`).

`plan_modules` is what the project's resolver calls for `plan.modules`. For each block
it reads the plan only when that gate is approved at the plan's current hash, and only
when the plan still passes `plan_check` (limits included: defence in depth). Anything
else (no plan, not approved, edited since, invalid, over a limit) expands to nothing.
The graph is rebuilt on every build and `next_task`, so a plan that changes simply
yields no instances until it is approved again. Each module's entry carries:

- params `block` and `module`;
- the module's write set (`writes`): the instance's outputs must stay inside it;
- its `depends_on` as ordering edges to the same rule's instances of those modules;
- the approved plan artifact (`plan_expand`'s output) as an input, so every expanded
  instance runs after the gate and the expansion.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.checks.plan_check import (
    DEFAULT_PLAN_PATH,
    PlanEnvError,
    PlanReport,
    check_plan_file,
    dependency_depth,
    topo_order,
)
from chipgraph.core.config.models import PlanCfg
from chipgraph.core.contracts import RuleInstance, RuleSpec
from chipgraph.core.engine.gate import GateStatus, gate_id_for
from chipgraph.core.engine.graph import ForeachEntry, GraphError, build_graph

PLAN_RULE = "digital-rtl/plan"
"""The agent rule that writes a block's plan."""
EXPAND_RULE = "digital-rtl/plan_expand"
"""The `gen` rule behind the gate `plan:<block>`; it writes the approved plan artifact."""


class _Blocks:
    def __init__(self, blocks: Iterable[str]) -> None:
        self._blocks = sorted(blocks)

    def expand(self, expr: str) -> list[dict[str, str]]:
        if expr != "blocks":
            raise GraphError(f"rule {EXPAND_RULE!r} must be 'foreach: blocks', not {expr!r}")
        return [{"block": b} for b in self._blocks]


@dataclass(frozen=True)
class PlanGate:
    """A block's plan gate: its id, the instance it is evaluated on, and the paths."""

    block: str
    gate_id: str
    instance: RuleInstance
    plan_path: str
    approved_path: str


def plan_gates(
    rules: Iterable[RuleSpec], blocks: Iterable[str], *, repo: str = "."
) -> dict[str, PlanGate]:
    """Every block's `PlanGate`, from the loaded `digital-rtl/plan_expand` rule.

    Raises `AppError` when the rule is not loaded (the `digital-rtl` pack is off) or has
    no gate.
    """
    rule = next((r for r in rules if r.id == EXPAND_RULE), None)
    if rule is None:
        raise AppError(
            f"foreach 'plan.modules' needs rule {EXPAND_RULE!r}: add the 'digital-rtl' pack "
            "to the profile's packs"
        )
    if rule.gate is None:
        raise AppError(f"rule {EXPAND_RULE!r} must have a gate (plan:{{block}})")
    try:
        graph = build_graph([rule], _Blocks(blocks), repo=repo)
    except GraphError as exc:
        raise AppError(str(exc)) from exc
    gates: dict[str, PlanGate] = {}
    for instance in graph.instances.values():
        block = instance.params["block"]
        plan_path = next(
            (ref.path for ref in instance.inputs if ref.path is not None),
            DEFAULT_PLAN_PATH.format(block=block),
        )
        approved = instance.outputs[0].path or ""
        gates[block] = PlanGate(
            block=block,
            gate_id=gate_id_for(rule.gate, instance.params),
            instance=instance,
            plan_path=plan_path,
            approved_path=approved,
        )
    return gates


def gate_status(ctx: AppContext, gate: PlanGate) -> GateStatus:
    """The status of a block's plan gate, at the plan's current hash."""
    return ctx.gates.status(gate.gate_id, gate.instance)


def within_limits(report: PlanReport, limits: PlanCfg) -> bool:
    """The plan is inside the profile's limits (checked again, apart from `plan_check`)."""
    plan = report.plan
    if plan is None:
        return False
    return (
        len(plan.modules) <= limits.max_modules
        and dependency_depth(plan) <= limits.max_depth
        and sum(m.budget.tries for m in plan.modules) <= limits.max_total_tries
    )


def block_entries(ctx: AppContext, rules: Sequence[RuleSpec], gate: PlanGate) -> list[ForeachEntry]:
    """The `plan.modules` entries of one block: none unless its plan is approved and valid."""
    if gate_status(ctx, gate) != "approved":
        return []
    resolved = ctx.require_profile()
    try:
        report = check_plan_file(
            ctx.root,
            gate.block,
            plan_path=gate.plan_path,
            resolved=resolved,
            rules=rules,
            check_existing=False,  # checked before approval; now the files are the plan's
        )
    except PlanEnvError:
        return []
    if not report.ok or report.plan is None or not within_limits(report, resolved.profile.plan):
        return []
    plan = report.plan
    entries: list[ForeachEntry] = []
    for name in topo_order(plan):
        module = plan.module(name)
        assert module is not None
        entries.append(
            ForeachEntry(
                params={"block": gate.block, "module": module.name},
                inputs=(gate.approved_path,),
                after=tuple(
                    {"block": gate.block, "module": dep} for dep in dict.fromkeys(module.depends_on)
                ),
                writes=frozenset(module.write_paths),
            )
        )
    return entries


def plan_modules(ctx: AppContext, rules: Sequence[RuleSpec]) -> list[ForeachEntry]:
    """`foreach: plan.modules`: every module of every block's approved, valid plan."""
    resolved = ctx.require_profile()
    gates = plan_gates(rules, resolved.profile.blocks, repo=ctx.store.repo)
    entries: list[ForeachEntry] = []
    for block in sorted(gates):
        entries.extend(block_entries(ctx, rules, gates[block]))
    return entries


__all__ = [
    "EXPAND_RULE",
    "PLAN_RULE",
    "PlanGate",
    "block_entries",
    "gate_status",
    "plan_gates",
    "plan_modules",
    "within_limits",
]

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
  instance runs after the gate and the expansion;
- the profile's `style.exemplars` and naming rules file as inputs (M2-11a), so editing
  one makes the module stale and it is written again. An exemplar an approved plan
  writes, or one a role of a `plan.modules` agent rule must not read, is left out;
- the block's layout directories as template values (`{layout.rtl}`, `{layout.tb}`,
  `chipgraph.packs.digital_rtl.layout`).

Two more selectors over the same approved plans (M2-11a), whose entries take every
module's write set and the block's testbench (the outputs of the `tb-author` rules over
`blocks`) as inputs, so their instances run after all of them:

- `plan.block`: one entry per block with an approved, valid plan (`digital-rtl/open_pr`,
  `digital-rtl/pr_gate`); none for the others;
- `plan.blocks`: one entry per block of the profile; a block without an approved plan
  gets a plain entry, with no extra input (`digital-rtl/review`, which also reviews a
  change no plan made).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.checks.plan_check import (
    DEFAULT_PLAN_PATH,
    PLAN_MODULES,
    PlanEnvError,
    PlanReport,
    check_plan_file,
    dependency_depth,
    topo_order,
)
from chipgraph.core.config.loader import ResolvedProfile
from chipgraph.core.config.models import PlanCfg
from chipgraph.core.contracts import RuleInstance, RuleSpec
from chipgraph.core.engine.gate import GateStatus, gate_id_for
from chipgraph.core.engine.graph import ForeachEntry, GraphError, build_graph, kind_for
from chipgraph.core.runtime.roles import denied_kind, find_role
from chipgraph.packs.digital_rtl.layout import LayoutError, fill_vars, layout_vars
from chipgraph.packs.digital_rtl.plan.model import Plan

PLAN_RULE = "digital-rtl/plan"
"""The agent rule that writes a block's plan."""
EXPAND_RULE = "digital-rtl/plan_expand"
"""The `gen` rule behind the gate `plan:<block>`; it writes the approved plan artifact."""
PLAN_BLOCK = "plan.block"
"""The selector over the blocks with an approved, valid plan."""
PLAN_BLOCKS = "plan.blocks"
"""The selector over every block, with the plan's files as inputs when it is approved."""
TB_ROLE = "tb-author"
"""The role of the rules whose outputs are a block's testbench."""


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


@dataclass(frozen=True)
class ApprovedPlan:
    """A block's plan, approved at its current hash and still valid."""

    gate: PlanGate
    plan: Plan
    vars: Mapping[str, str] = field(default_factory=dict)

    @property
    def block(self) -> str:
        return self.gate.block

    def module_writes(self) -> tuple[str, ...]:
        """Every file of the plan's write sets, in build order."""
        return tuple(
            path
            for name in topo_order(self.plan)
            if (module := self.plan.module(name)) is not None
            for path in module.write_paths
        )


def _vars(resolved: ResolvedProfile, block: str) -> dict[str, str]:
    try:
        return layout_vars(resolved, block)
    except LayoutError as exc:
        raise AppError(str(exc)) from exc


def approved_plan(
    ctx: AppContext, rules: Sequence[RuleSpec], gate: PlanGate
) -> ApprovedPlan | None:
    """The block's plan when it is approved at its current hash and valid, else None."""
    if gate_status(ctx, gate) != "approved":
        return None
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
        return None
    if not report.ok or report.plan is None or not within_limits(report, resolved.profile.plan):
        return None
    return ApprovedPlan(gate=gate, plan=report.plan, vars=_vars(resolved, gate.block))


def approved_plans(ctx: AppContext, rules: Sequence[RuleSpec]) -> dict[str, ApprovedPlan]:
    """Every block's `ApprovedPlan`, for the blocks that have one."""
    resolved = ctx.require_profile()
    gates = plan_gates(rules, resolved.profile.blocks, repo=ctx.store.repo)
    found: dict[str, ApprovedPlan] = {}
    for block in sorted(gates):
        approved = approved_plan(ctx, rules, gates[block])
        if approved is not None:
            found[block] = approved
    return found


def _naming_file(root: Path, resolved: ResolvedProfile, block: str) -> str | None:
    """The naming rules file the profile names, repo-relative, when it is in the repo."""
    from chipgraph.checks.naming import _resolve_rules_path, _RulesError
    from chipgraph.packs.digital_rtl.rtl.context import _naming_ref

    ref = _naming_ref(resolved, block)
    if ref is None:
        return None
    try:
        path = _resolve_rules_path(ref, root)
    except _RulesError:
        return None
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    return rel if path.is_file() else None


def style_inputs(
    ctx: AppContext,
    rules: Sequence[RuleSpec],
    block: str,
    *,
    planned: Iterable[str] = (),
) -> tuple[str, ...]:
    """The style files a planned module is written after: exemplars and naming rules.

    Only files that exist; never one in `planned` (a file an approved plan writes:
    it would be an input of the module that writes it), and never one a role of an
    agent rule over `plan.modules` must not read.
    """
    resolved = ctx.require_profile()
    roles = [
        role
        for rule in rules
        if rule.foreach == PLAN_MODULES and rule.kind == "agent" and rule.role is not None
        if (role := find_role(rule.role)) is not None
    ]
    skip = set(planned)
    candidates = [*resolved.for_block(block).style.exemplars]
    naming = _naming_file(ctx.root, resolved, block)
    if naming is not None:
        candidates.append(naming)
    out: list[str] = []
    for rel in dict.fromkeys(candidates):
        if rel in skip or not (ctx.root / rel).is_file():
            continue
        if any(denied_kind(role, kind_for(rel), rel) is not None for role in roles):
            continue
        out.append(rel)
    return tuple(out)


def module_entries(
    ctx: AppContext,
    rules: Sequence[RuleSpec],
    approved: ApprovedPlan,
    *,
    planned: Iterable[str] = (),
) -> list[ForeachEntry]:
    """The `plan.modules` entries of one block's approved plan."""
    plan = approved.plan
    style = style_inputs(ctx, rules, approved.block, planned=planned)
    entries: list[ForeachEntry] = []
    for name in topo_order(plan):
        module = plan.module(name)
        assert module is not None
        entries.append(
            ForeachEntry(
                params={"block": approved.block, "module": module.name},
                inputs=(approved.gate.approved_path, *style),
                after=tuple(
                    {"block": approved.block, "module": dep}
                    for dep in dict.fromkeys(module.depends_on)
                ),
                writes=frozenset(module.write_paths),
                vars=dict(approved.vars),
            )
        )
    return entries


def block_entries(ctx: AppContext, rules: Sequence[RuleSpec], gate: PlanGate) -> list[ForeachEntry]:
    """The `plan.modules` entries of one block: none unless its plan is approved and valid."""
    approved = approved_plan(ctx, rules, gate)
    if approved is None:
        return []
    return module_entries(ctx, rules, approved, planned=approved.module_writes())


def testbench_files(
    rules: Sequence[RuleSpec], block: str, values: Mapping[str, str]
) -> tuple[str, ...]:
    """The block's testbench: the outputs of the `tb-author` rules over `blocks`."""
    out: list[str] = []
    for rule in sorted(rules, key=lambda r: r.id):
        if rule.kind == "agent" and rule.role == TB_ROLE and rule.foreach == "blocks":
            out.extend(fill_vars(t, {**values, "block": block}) for t in rule.outputs)
    return tuple(dict.fromkeys(out))


def block_files(
    rules: Sequence[RuleSpec], approved: ApprovedPlan
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The block's RTL (every module's write set) and testbench files."""
    return approved.module_writes(), testbench_files(rules, approved.block, approved.vars)


def plan_block_entry(rules: Sequence[RuleSpec], approved: ApprovedPlan) -> ForeachEntry:
    """The `plan.block` / `plan.blocks` entry of a block with an approved plan."""
    rtl, tb = block_files(rules, approved)
    return ForeachEntry(
        params={"block": approved.block},
        inputs=tuple(dict.fromkeys((approved.gate.approved_path, *rtl, *tb))),
        vars=dict(approved.vars),
    )


@dataclass
class PlanExpansion:
    """The approved plans of a project, read once per graph, and the entries they give."""

    ctx: AppContext
    rules: Sequence[RuleSpec]
    _approved: dict[str, ApprovedPlan] | None = None

    @property
    def approved(self) -> dict[str, ApprovedPlan]:
        if self._approved is None:
            self._approved = approved_plans(self.ctx, self.rules)
        return self._approved

    def modules(self) -> list[ForeachEntry]:
        """`plan.modules`: every module of every approved plan."""
        planned = {p for a in self.approved.values() for p in a.module_writes()}
        entries: list[ForeachEntry] = []
        for block in sorted(self.approved):
            entries.extend(
                module_entries(self.ctx, self.rules, self.approved[block], planned=planned)
            )
        return entries

    def blocks_with_plan(self) -> list[ForeachEntry]:
        """`plan.block`: one entry per block with an approved plan."""
        return [plan_block_entry(self.rules, self.approved[b]) for b in sorted(self.approved)]

    def every_block(self) -> list[ForeachEntry]:
        """`plan.blocks`: every block; an approved plan's files as inputs when it has one."""
        resolved = self.ctx.require_profile()
        entries: list[ForeachEntry] = []
        for block in sorted(resolved.profile.blocks):
            approved = self.approved.get(block)
            if approved is not None:
                entries.append(plan_block_entry(self.rules, approved))
            else:
                entries.append(ForeachEntry(params={"block": block}, vars=_vars(resolved, block)))
        return entries


def plan_modules(ctx: AppContext, rules: Sequence[RuleSpec]) -> list[ForeachEntry]:
    """`foreach: plan.modules`: every module of every block's approved, valid plan."""
    return PlanExpansion(ctx, rules).modules()


__all__ = [
    "EXPAND_RULE",
    "PLAN_BLOCK",
    "PLAN_BLOCKS",
    "PLAN_RULE",
    "TB_ROLE",
    "ApprovedPlan",
    "PlanExpansion",
    "PlanGate",
    "approved_plan",
    "approved_plans",
    "block_entries",
    "block_files",
    "gate_status",
    "module_entries",
    "plan_block_entry",
    "plan_gates",
    "plan_modules",
    "style_inputs",
    "testbench_files",
    "within_limits",
]

"""`PlanCheck`: a block's plan is valid before a person approves it (DESIGN.md 5.1, 5.3).

A built-in check, deterministic and fast: a pure function (`validate_plan`) over the
plan file (`chipgraph.packs.digital_rtl.plan.Plan`), the block's slice of the Design
Model, the profile and the build graph's other rules (`PlanEnv`). The Planner gets its
issues as the redo text of the normal agent loop. It rejects:

- write sets: two modules writing the same file (F2), a write outside the block's
  layout paths (`blocks.<b>.layout`), a write to a path another rule produces (a file
  a `gen`/`import`/`human` rule writes is the engine's or a person's, never a plan's),
  an existing file the plan does not say it replaces (`replaces: true`), and a module
  whose write set misses a file a `foreach: plan.modules` rule will write for it;
- dependencies: an unknown module, a module depending on itself, a cycle;
- requirements: an id that is not one of the block's requirements in the model, and a
  block requirement neither assigned to a module nor listed in `unassigned_reqs`;
- interfaces (F1): a port that is not on the block's interface in the model, or whose
  direction or width differs from it; `internal: true` helper ports only off the top;
- limits (profile `plan:`): modules, dependency depth, total tries;
- module names against the project's naming rule, when one is configured.

`open_questions` are a **warning**: they do not block, but the approver must read
them (`chipgraph plan show`).

An existing file counts as the plan's own once a chipgraph build of this block's plan
nodes wrote it (its hash is a production record's): re-planning does not have to mark
every file it wrote last time `replaces: true`.

Example profile usage (`.chipgraph.yml`):

```yaml
adapters:
  plan_check: { use: plan_check }      # optional arg: plan: "plan/{block}.plan.yml"
plan: { max_modules: 12, max_depth: 4, max_total_tries: 36 }
```
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from chipgraph.checks._common import (
    compile_template,
    compute_idempotency_key,
    error_result,
    sha256_bytes,
    sha256_file,
)
from chipgraph.checks._naming_pyslang import Decl
from chipgraph.checks.naming import (
    _check_decl,
    _CompiledRules,
    _load_rules,
    _resolve_rules_path,
    _RulesError,
)
from chipgraph.core.config.loader import ResolvedProfile
from chipgraph.core.config.loader import load as load_profile
from chipgraph.core.config.models import PlanCfg, Profile
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue, RuleSpec
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.engine.graph import GraphError, build_graph
from chipgraph.core.engine.records import RecordStore
from chipgraph.core.model.model import DesignModel
from chipgraph.core.model.store import ModelStore, default_model_db_path
from chipgraph.core.plugin_api.types import ToolContext
from chipgraph.core.state.layout import StateLayout
from chipgraph.packs.digital_rtl.plan.model import Plan, PlanModule

DEFAULT_PLAN_PATH = "plan/{block}.plan.yml"
"""Where a block's plan lives (the output of rule `digital-rtl/plan`)."""

PLAN_MODULES = "plan.modules"
"""The `foreach` selector that expands to one instance per module of an approved plan."""

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


class PlanEnvError(Exception):
    """There is nothing to check a plan against: no profile, or no Design Model."""


# --- the environment a plan is checked against ----------------------------------------


@dataclass(frozen=True)
class ReqInfo:
    """One requirement of the Design Model."""

    key: str
    name: str
    text: str
    block: str | None
    """The owning block's name, if the model says."""
    declared: bool = True
    """The spec declares its id; an inferred one (D37) has only its model key."""

    @property
    def ref(self) -> str:
        """How a plan names it: the declared REQ id, else the inferred key (`timer.h1a2b`)."""
        return self.name if self.declared else self.key.removeprefix("requirement:")

    @property
    def refs(self) -> frozenset[str]:
        """The strings a plan may use for it: its id, its key, the key without the kind."""
        refs = {self.key, self.key.removeprefix("requirement:")}
        if self.declared:
            refs.add(self.name)
        return frozenset(refs)


@dataclass(frozen=True)
class PortInfo:
    """One port: name, direction and width as the Design Model knows them."""

    name: str
    direction: str | None
    width: int | str | None


@dataclass(frozen=True)
class ModuleInfo:
    """An existing module of the block in the Design Model."""

    name: str
    file: str | None
    ports: tuple[PortInfo, ...]


@dataclass(frozen=True)
class PlanEnv:
    """Everything `validate_plan` checks a plan against, for one block."""

    block: str
    requirements: tuple[ReqInfo, ...] = ()
    """The block's requirements."""
    other_requirements: tuple[ReqInfo, ...] = ()
    """Every other requirement of the model (so a wrong block is named as such)."""
    interface: tuple[PortInfo, ...] = ()
    """The block's interface ports, from the spec (else the top RTL module)."""
    interface_source: str = "none"
    """Where `interface` came from: `spec`, `rtl:<module>` or `none`."""
    existing_modules: tuple[ModuleInfo, ...] = ()
    layout: tuple[str, ...] = ()
    """The block's layout templates, `{block}`/`{BLOCK}` filled in."""
    engine_outputs: Mapping[str, str] = field(default_factory=dict)
    """Path -> instance id, for every output of a non-agent rule."""
    agent_outputs: Mapping[str, str] = field(default_factory=dict)
    """Path -> instance id, for every output of an agent rule not expanded from a plan."""
    module_outputs: tuple[tuple[str, str], ...] = ()
    """(rule id, output template) of every rule with `foreach: plan.modules`."""
    existing: Mapping[str, str] = field(default_factory=dict)
    """Path -> content hash, for the plan's write paths that exist on disk."""
    built: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """Path -> hashes a build of this block's plan nodes recorded for it."""
    limits: PlanCfg = field(default_factory=PlanCfg)
    naming: _CompiledRules | None = None
    naming_ref: str | None = None


# --- validation (pure) ----------------------------------------------------------------


def _source_lines(source: str | None) -> tuple[dict[str, int], dict[int, int]]:
    """Line numbers of the plan's top-level keys and of each `modules[i]` item."""
    if not source:
        return {}, {}
    try:
        node = yaml.compose(source)
    except yaml.YAMLError:
        return {}, {}
    keys: dict[str, int] = {}
    modules: dict[int, int] = {}
    if not isinstance(node, yaml.MappingNode):
        return keys, modules
    for key_node, value_node in node.value:
        name = str(key_node.value)
        keys[name] = key_node.start_mark.line + 1
        if name == "modules" and isinstance(value_node, yaml.SequenceNode):
            for index, item in enumerate(value_node.value):
                modules[index] = item.start_mark.line + 1
    return keys, modules


def _fill(template: str, params: Mapping[str, str]) -> str | None:
    """`template` with `{key}` from `params` (`{KEY}` upper-cased); `None` if one is unknown."""
    missing = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal missing
        key = match.group(1)
        if key in params:
            return params[key]
        if key.isupper() and key.lower() in params:
            return params[key.lower()].upper()
        missing = True
        return match.group(0)

    filled = _PLACEHOLDER_RE.sub(_replace, template)
    return None if missing else filled


def _layout_template(template: str, block: str) -> str:
    """A layout template with `{block}`/`{BLOCK}` filled in; other fields stay fields."""
    return template.replace("{block}", block).replace("{BLOCK}", block.upper())


def dependency_depth(plan: Plan) -> int:
    """The longest chain of dependencies, counted in modules (no cycle assumed)."""
    deps = {m.name: [d for d in m.depends_on if d != m.name] for m in plan.modules}
    memo: dict[str, int] = {}

    def depth(name: str, seen: frozenset[str]) -> int:
        if name in memo:
            return memo[name]
        children = [d for d in deps.get(name, []) if d in deps and d not in seen]
        value = 1 + max((depth(d, seen | {d}) for d in children), default=0)
        memo[name] = value
        return value

    return max((depth(m.name, frozenset({m.name})) for m in plan.modules), default=0)


def _find_cycle(plan: Plan) -> list[str] | None:
    """One dependency cycle among known modules (as a path ending where it starts), or None."""
    deps = {m.name: [d for d in m.depends_on if d != m.name] for m in plan.modules}
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(deps, white)
    stack: list[str] = []

    def visit(name: str) -> list[str] | None:
        color[name] = gray
        stack.append(name)
        for dep in deps[name]:
            if dep not in deps:
                continue
            if color[dep] == gray:
                return [*stack[stack.index(dep) :], dep]
            if color[dep] == white:
                found = visit(dep)
                if found is not None:
                    return found
        stack.pop()
        color[name] = black
        return None

    for name in deps:
        if color[name] == white:
            found = visit(name)
            if found is not None:
                return found
    return None


def topo_order(plan: Plan) -> list[str]:
    """The modules in build order: dependencies first, ties by plan order (no cycle)."""
    order: list[str] = []
    done: set[str] = set()
    pending = [m.name for m in plan.modules]
    known = set(pending)
    while pending:
        progressed = False
        for name in list(pending):
            module = plan.module(name)
            assert module is not None
            if all(d in done or d not in known or d == name for d in module.depends_on):
                order.append(name)
                done.add(name)
                pending.remove(name)
                progressed = True
        if not progressed:  # a cycle: keep plan order for the rest
            order.extend(pending)
            break
    return order


def validate_plan(
    plan: Plan,
    env: PlanEnv,
    *,
    plan_path: str,
    source: str | None = None,
    check_existing: bool = True,
) -> list[Issue]:
    """Every problem with `plan` for `env.block`, as `Issue`s on `plan_path`, sorted by line.

    Pure: reads nothing but its arguments. `source` (the file's text) only gives the
    issues line numbers. With `check_existing=False` the existing-file rule is skipped:
    once a person approved the plan, the files it writes exist by design.
    """
    key_lines, module_lines = _source_lines(source)
    issues: list[Issue] = []

    def add(rule: str, msg: str, *, line: int | None = None, severity: str = "error") -> None:
        issues.append(
            Issue(file=plan_path, line=line, rule=rule, severity=severity, msg=msg)  # type: ignore[arg-type]
        )

    index_of = {id(m): i for i, m in enumerate(plan.modules)}

    def mline(module: PlanModule) -> int | None:
        return module_lines.get(index_of[id(module)])

    # --- structure
    if plan.block != env.block:
        add(
            "plan.block",
            f"the plan is for block {plan.block!r}, but this is block {env.block!r}",
            line=key_lines.get("block"),
        )
    names: dict[str, PlanModule] = {}
    for module in plan.modules:
        if module.name in names:
            add(
                "plan.duplicate_module",
                f"module {module.name!r} is listed twice",
                line=mline(module),
            )
        else:
            names[module.name] = module
    top = names.get(plan.top)
    if top is None:
        add(
            "plan.top",
            f"top {plan.top!r} is not one of the plan's modules: {', '.join(names)}",
            line=key_lines.get("top"),
        )

    # --- limits
    limits = env.limits
    if len(plan.modules) > limits.max_modules:
        add(
            "plan.limit.modules",
            f"the plan has {len(plan.modules)} modules; the limit is {limits.max_modules} "
            "(profile plan.max_modules)",
            line=key_lines.get("modules"),
        )
    total_tries = sum(m.budget.tries for m in plan.modules)
    if total_tries > limits.max_total_tries:
        add(
            "plan.limit.tries",
            f"the modules' budgets add up to {total_tries} tries; the limit is "
            f"{limits.max_total_tries} (profile plan.max_total_tries)",
            line=key_lines.get("modules"),
        )

    # --- dependencies
    for module in plan.modules:
        for dep in module.depends_on:
            if dep == module.name:
                add(
                    "plan.self_dependency",
                    f"module {module.name!r} depends on itself",
                    line=mline(module),
                )
            elif dep not in names:
                add(
                    "plan.unknown_dependency",
                    f"module {module.name!r} depends on {dep!r}, which is not a module of "
                    f"this plan ({', '.join(names)})",
                    line=mline(module),
                )
    cycle = _find_cycle(plan)
    if cycle is not None:
        add(
            "plan.dependency_cycle",
            f"dependency cycle: {' -> '.join(cycle)}",
            line=key_lines.get("modules"),
        )
    else:
        depth = dependency_depth(plan)
        if depth > limits.max_depth:
            add(
                "plan.limit.depth",
                f"the longest dependency chain is {depth} modules; the limit is "
                f"{limits.max_depth} (profile plan.max_depth)",
                line=key_lines.get("modules"),
            )

    # --- requirements
    block_reqs = {ref: req for req in env.requirements for ref in req.refs}
    other_reqs = {ref: req for req in env.other_requirements for ref in req.refs}

    def known_req(ref: str, where: str, line: int | None) -> ReqInfo | None:
        req = block_reqs.get(ref)
        if req is not None:
            return req
        other = other_reqs.get(ref)
        if other is not None:
            add(
                "plan.unknown_req",
                f"{where}: {ref!r} is a requirement of block {other.block!r}, not of {env.block!r}",
                line=line,
            )
        else:
            add(
                "plan.unknown_req",
                f"{where}: {ref!r} is not a requirement in the Design Model; do not invent "
                "requirements",
                line=line,
            )
        return None

    covered: set[str] = set()
    for module in plan.modules:
        for ref in module.reqs:
            req = known_req(ref, f"module {module.name!r}", mline(module))
            if req is not None:
                covered.add(req.key)
    for item in plan.unassigned_reqs:
        req = known_req(item.req, "unassigned_reqs", key_lines.get("unassigned_reqs"))
        if req is not None:
            covered.add(req.key)
    for req in env.requirements:
        if req.key not in covered:
            add(
                "plan.unassigned_req",
                f"requirement {req.ref} is neither assigned to a module nor listed in "
                "unassigned_reqs with a reason",
                line=key_lines.get("unassigned_reqs") or key_lines.get("modules"),
            )

    # --- interfaces
    interface = {p.name: p for p in env.interface}
    for module in plan.modules:
        seen_ports: set[str] = set()
        for port in module.interface.ports:
            where = f"module {module.name!r} port {port.name!r}"
            if port.name in seen_ports:
                add("plan.duplicate_port", f"{where} is listed twice", line=mline(module))
                continue
            seen_ports.add(port.name)
            if port.internal:
                if module.name == plan.top:
                    add(
                        "plan.internal_port_on_top",
                        f"{where} is marked internal, but {module.name!r} is the block's "
                        "top: its ports are the block's interface",
                        line=mline(module),
                    )
                continue
            known = interface.get(port.name)
            if known is None:
                listed = ", ".join(interface) or "none (run `chipgraph ingest`)"
                add(
                    "plan.unknown_port",
                    f"{where} is not on block {env.block!r}'s interface in the Design Model "
                    f"({listed}); take ports from the model, or mark a helper port between "
                    "modules `internal: true`",
                    line=mline(module),
                )
                continue
            if known.direction is not None and known.direction != port.direction:
                add(
                    "plan.port_mismatch",
                    f"{where} is {port.direction}, but the Design Model has {known.direction}",
                    line=mline(module),
                )
            if known.width is not None and str(known.width) != str(port.width):
                add(
                    "plan.port_mismatch",
                    f"{where} has width {port.width}, but the Design Model has {known.width}",
                    line=mline(module),
                )

    # --- write sets
    layout = [compile_template(t) for t in env.layout]
    writers: dict[str, str] = {}
    for module in plan.modules:
        for write in module.writes:
            path = write.path
            where = f"module {module.name!r} writes {path}"
            other = writers.get(path)
            if other is not None and other != module.name:
                add(
                    "plan.write_overlap",
                    f"modules {other!r} and {module.name!r} both write {path}; the write "
                    "sets of modules must not overlap (F2)",
                    line=mline(module),
                )
            writers.setdefault(path, module.name)
            if not any(t.match(path) is not None for t in layout):
                listed = ", ".join(env.layout) or "none"
                add(
                    "plan.write_layout",
                    f"{where}, outside block {env.block!r}'s layout paths ({listed})",
                    line=mline(module),
                )
            if path in env.engine_outputs:
                add(
                    "plan.write_engine",
                    f"{where}, which rule instance {env.engine_outputs[path]!r} produces; "
                    "a file a gen, import or human rule writes is never a plan's (F2)",
                    line=mline(module),
                )
            elif path in env.agent_outputs:
                add(
                    "plan.write_taken",
                    f"{where}, which rule instance {env.agent_outputs[path]!r} produces",
                    line=mline(module),
                )
            elif (
                check_existing
                and path in env.existing
                and not write.replaces
                and env.existing[path] not in env.built.get(path, frozenset())
            ):
                add(
                    "plan.write_existing",
                    f"{where}, which already exists; set `replaces: true` on that write "
                    "to replace it on purpose",
                    line=mline(module),
                )
        for rule_id, template in env.module_outputs:
            path_or_none = _fill(template, {"block": env.block, "module": module.name})
            if path_or_none is not None and path_or_none not in module.write_paths:
                add(
                    "plan.write_missing",
                    f"module {module.name!r}: rule {rule_id!r} writes {path_or_none} for it, "
                    "which is not in its writes",
                    line=mline(module),
                )

    # --- naming
    if env.naming is not None:
        for module in plan.modules:
            line = mline(module) or 1
            for issue in _check_decl(plan_path, Decl("module", module.name, line), env.naming):
                add("plan.naming", f"module name {issue.msg} ({issue.rule})", line=line)

    # --- what the approver must read
    for question in plan.open_questions:
        add(
            "plan.open_questions",
            f"open question for the approver: {question}",
            line=key_lines.get("open_questions"),
            severity="warning",
        )

    issues.sort(key=lambda i: (i.line or 0, i.rule, i.msg))
    return issues


# --- reading a plan file --------------------------------------------------------------


def parse_plan(text: str, *, plan_path: str) -> tuple[Plan | None, list[Issue]]:
    """Parse a plan file's text: the plan, or `None` and the schema issues."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [Issue(file=plan_path, rule="plan.schema", msg=f"invalid YAML: {exc}")]
    try:
        return Plan.model_validate(data), []
    except ValidationError as exc:
        issues = [
            Issue(
                file=plan_path,
                rule="plan.schema",
                msg=f"{'.'.join(str(p) for p in err['loc']) or '<file>'}: {err['msg']}",
            )
            for err in exc.errors()
        ]
        return None, issues


# --- the environment, from a project --------------------------------------------------


class _BlocksOnly:
    """Expands `blocks` only; any other selector (a plan's modules) to nothing."""

    def __init__(self, blocks: Iterable[str]) -> None:
        self._blocks = sorted(blocks)

    def expand(self, expr: str) -> list[dict[str, str]]:
        return [{"block": b} for b in self._blocks] if expr == "blocks" else []


def project_rules(root: Path, resolved: ResolvedProfile) -> list[RuleSpec]:
    """The rules of the profile's packs (the same as `chipgraph build` loads)."""
    from chipgraph.app.build import rules_for_packs  # lazy: app imports this module's users

    return rules_for_packs(root, resolved.profile.packs)


def _other_outputs(
    rules: Sequence[RuleSpec], blocks: Iterable[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Outputs of the graph's rules that are not expanded from a plan: non-agent, agent."""
    fixed = [r for r in rules if r.foreach != PLAN_MODULES]
    try:
        graph = build_graph(fixed, _BlocksOnly(blocks))
    except GraphError:
        return {}, {}
    engine: dict[str, str] = {}
    agent: dict[str, str] = {}
    for iid, instance in graph.instances.items():
        target = agent if graph.rules[instance.rule_id].kind == "agent" else engine
        for ref in instance.outputs:
            if ref.path is not None:
                target[ref.path] = iid
    return engine, agent


def _instance_params(instance_id: str) -> tuple[str, dict[str, str]]:
    rule_id, _, rest = instance_id.partition("[")
    params: dict[str, str] = {}
    for pair in rest.removesuffix("]").split(","):
        key, sep, value = pair.partition("=")
        if sep:
            params[key] = value
    return rule_id, params


def _built(root: Path, rules: Sequence[RuleSpec], block: str) -> dict[str, frozenset[str]]:
    """Path -> hashes recorded when this block's plan nodes last produced it."""
    plan_rules = {r.id for r in rules if r.foreach == PLAN_MODULES}
    found: dict[str, set[str]] = {}
    for iid, record in RecordStore(StateLayout(root)).all().items():
        rule_id, params = _instance_params(iid)
        if rule_id not in plan_rules or params.get("block") != block:
            continue
        for key, digest in record.output_hashes.items():
            found.setdefault(key.partition(":")[2], set()).add(digest)
    return {path: frozenset(hashes) for path, hashes in found.items()}


def _req(entity: Any) -> ReqInfo:
    block = entity.attrs.get("block")
    return ReqInfo(
        key=entity.key,
        name=str(entity.name),
        text=str(getattr(entity, "text", None) or ""),
        block=block.removeprefix("block:") if isinstance(block, str) else None,
        declared=entity.attrs.get("id_source", "declared") == "declared",
    )


def _port(entity: Any) -> PortInfo:
    return PortInfo(name=str(entity.name), direction=entity.direction, width=entity.width)


def _interface(
    model: DesignModel, block: str, profile: Profile, modules: Sequence[ModuleInfo]
) -> tuple[tuple[PortInfo, ...], str]:
    """The block's interface: its spec ports, else its top RTL module's (ports_diff `top`)."""
    key = f"block:{block}"
    spec = [
        _port(p)
        for p in model.by_kind("port")
        if p.attrs.get("block") == key and p.attrs.get("origin") == "spec"
    ]
    if spec:
        return tuple(spec), "spec"
    top: str | None = None
    cfg = profile.adapters.get("ports_diff")
    raw_top = cfg.model_dump().get("top") if cfg is not None else None
    if isinstance(raw_top, str):
        top = _fill(raw_top, {"block": block})
    if top is None and len(modules) == 1:
        top = modules[0].name
    for module in modules:
        if module.name == top:
            return module.ports, f"rtl:{module.name}"
    return (), "none"


def _naming(profile: Profile, root: Path) -> tuple[_CompiledRules | None, str | None]:
    ref = profile.naming.rules
    if ref is None:
        cfg = profile.adapters.get("naming")
        dumped = cfg.model_dump() if cfg is not None else {}
        if dumped.get("use") == "naming" and isinstance(dumped.get("rules"), str):
            ref = dumped["rules"]
    if ref is None:
        return None, None
    try:
        rules, _ = _load_rules(_resolve_rules_path(ref, root))
    except _RulesError as exc:
        raise PlanEnvError(f"naming rules {ref!r}: {exc}") from exc
    return _CompiledRules(rules), ref


def load_env(
    root: Path,
    block: str,
    *,
    resolved: ResolvedProfile | None = None,
    rules: Sequence[RuleSpec] | None = None,
    write_paths: Iterable[str] = (),
) -> PlanEnv:
    """The `PlanEnv` of `block` in the project at `root`.

    `resolved` and `rules` default to the project's profile and its packs' rules.
    `write_paths` are the plan's writes, whose current hashes are looked up. Raises
    `PlanEnvError` when there is no profile or no Design Model (run `chipgraph ingest`).
    """
    if resolved is None:
        resolved = load_profile(root)
        if resolved is None:
            raise PlanEnvError(f"no .chipgraph.yml found from {root}")
    profile = resolved.for_block(block)
    db = default_model_db_path(root)
    if not db.is_file():
        raise PlanEnvError(f"no Design Model at {db}; run `chipgraph ingest` first")
    model = ModelStore(db).read()
    if rules is None:
        rules = project_rules(root, resolved)

    key = f"block:{block}"
    reqs = [_req(e) for e in model.by_kind("requirement")]
    modules = tuple(
        ModuleInfo(
            name=str(m.name),
            file=getattr(m, "file", None),
            ports=tuple(_port(p) for p in model.by_kind("port") if p.module == m.key),  # type: ignore[attr-defined]
        )
        for m in model.by_kind("module")
        if getattr(m, "block", None) == key
    )
    interface, source = _interface(model, block, profile, modules)
    layout: list[str] = []
    for value in profile.layout.values():
        templates = (value,) if isinstance(value, str) else value
        layout.extend(_layout_template(t, block) for t in templates)
    engine, agent = _other_outputs(rules, resolved.profile.blocks)
    existing = {
        path: sha256_file(root / path)
        for path in dict.fromkeys(write_paths)
        if (root / path).is_file()
    }
    naming, naming_ref = _naming(profile, root)
    return PlanEnv(
        block=block,
        requirements=tuple(r for r in reqs if r.block == block),
        other_requirements=tuple(r for r in reqs if r.block != block),
        interface=interface,
        interface_source=source,
        existing_modules=modules,
        layout=tuple(dict.fromkeys(layout)),
        engine_outputs=engine,
        agent_outputs=agent,
        module_outputs=tuple(
            (r.id, t) for r in rules if r.foreach == PLAN_MODULES for t in r.outputs
        ),
        existing=existing,
        built=_built(root, rules, block),
        limits=resolved.profile.plan,
        naming=naming,
        naming_ref=naming_ref,
    )


@dataclass(frozen=True)
class PlanReport:
    """A block's plan file, checked: the plan (when it parses) and every issue."""

    block: str
    path: str
    exists: bool
    plan: Plan | None
    issues: tuple[Issue, ...]
    sha256: str | None
    env: PlanEnv | None

    @property
    def ok(self) -> bool:
        """The plan exists, parses and has no error issue."""
        return self.plan is not None and not any(i.severity == "error" for i in self.issues)


def check_plan_file(
    root: Path,
    block: str,
    *,
    plan_path: str | None = None,
    resolved: ResolvedProfile | None = None,
    rules: Sequence[RuleSpec] | None = None,
    check_existing: bool = True,
) -> PlanReport:
    """Read, parse and validate `block`'s plan in the project at `root`.

    Raises `PlanEnvError` when there is nothing to check it against (see `load_env`).
    """
    rel = _fill(plan_path or DEFAULT_PLAN_PATH, {"block": block}) or DEFAULT_PLAN_PATH
    path = root / rel
    if not path.is_file():
        return PlanReport(block, rel, False, None, (), None, None)
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    plan, issues = parse_plan(text, plan_path=rel)
    if plan is None:
        return PlanReport(block, rel, True, None, tuple(issues), sha256_bytes(raw), None)
    writes = [w.path for m in plan.modules for w in m.writes]
    env = load_env(root, block, resolved=resolved, rules=rules, write_paths=writes)
    issues = validate_plan(plan, env, plan_path=rel, source=text, check_existing=check_existing)
    return PlanReport(block, rel, True, plan, tuple(issues), sha256_bytes(raw), env)


# --- the check plugin -----------------------------------------------------------------


class PlanCheck:
    """Checks a block's plan file against the Design Model, the profile and the graph."""

    id = "plan_check"
    name = "Plan"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        block = ctx.params.get("block")
        if not block:
            return error_result(spec, "plan_check runs per block: no block param", start)
        template = spec.args.get("plan", DEFAULT_PLAN_PATH)
        if not isinstance(template, str):
            return error_result(spec, "args['plan'] must be a path template string", start)
        try:
            report = check_plan_file(ctx.repo_root, block, plan_path=template)
        except PlanEnvError as exc:
            return error_result(spec, str(exc), start, rule="plan.env")

        if not report.exists:
            issues: tuple[Issue, ...] = (
                Issue(
                    file=report.path,
                    rule="plan.missing",
                    severity="info",
                    msg=f"block {block!r} has no plan yet",
                ),
            )
        else:
            issues = report.issues
        status: CheckStatus = "fail" if any(i.severity == "error" for i in issues) else "pass"
        hashes = [(report.path, report.sha256 or "none")]
        if report.env is not None:
            hashes.extend(sorted(report.env.existing.items()))
        key = compute_idempotency_key(spec.id, {**spec.args, "block": block}, hashes)
        return CheckResult(
            check_id=spec.id,
            status=status,
            issues=issues,
            duration_s=time.monotonic() - start,
            idempotency_key=key,
        )


__all__ = [
    "DEFAULT_PLAN_PATH",
    "PLAN_MODULES",
    "ModuleInfo",
    "PlanCheck",
    "PlanEnv",
    "PlanEnvError",
    "PlanReport",
    "PortInfo",
    "ReqInfo",
    "check_plan_file",
    "dependency_depth",
    "load_env",
    "parse_plan",
    "project_rules",
    "topo_order",
    "validate_plan",
]

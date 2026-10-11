"""What an `rtl-author` task gets in its context (DESIGN.md 5.1, 5.3; task M2-04).

Only the inputs an RTL module is written from, never a testbench:

- `module`: the module's entry of the block's approved plan (summary, requirement ids,
  interface ports, dependencies, write set, budget), with the plan's top, assumptions
  and open questions;
- `requirements`: the module's requirements, with their ids and text (the Design
  Model's; the plan names them);
- `interface`: the module's ports. A port the Design Model knows (the block's spec
  ports, else its top RTL module's) is shown as the model has it (`source: model`); an
  internal port between the block's modules is shown as the approved plan gives it
  (`source: plan`). Clock and reset come from the model's block;
- `dependencies`: the modules it depends on, declarations only: name and ports, from
  the Design Model when it knows the module, else from the approved plan;
- `naming`: the project's naming rules, rendered as text (`naming_text`): the dynamic
  part of the `org/naming` skill;
- `style`: the exemplars' text (`style.exemplars`, each capped, labelled "imitate this
  style") and the style profile measured from them (`chipgraph.checks.style`). An
  exemplar the role must not read (`is_denied`, e.g. a testbench) is refused: its entry
  says so and holds no text.

`spec_side` is the block's spec slice (registers, interrupts, clock and reset) with the
requirements cut to the module's: what the model input `block/<block>` shows.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from chipgraph.checks._naming_rules import NamingRules
from chipgraph.checks.naming import _load_rules, _resolve_rules_path, _RulesError
from chipgraph.checks.plan_check import PlanEnv, PlanEnvError, check_plan_file
from chipgraph.checks.style import style_profile
from chipgraph.core.config.loader import ResolvedProfile
from chipgraph.core.contracts import RuleSpec
from chipgraph.core.model import DesignModel
from chipgraph.packs.digital_rtl.plan.model import Plan, PlanModule
from chipgraph.packs.dv.interface import spec_slice

MAX_EXEMPLAR_CHARS = 12_000
"""Longest text of one exemplar in the context; longer files are cut."""
MAX_EXEMPLARS_CHARS = 36_000
"""Most exemplar text in one context; exemplars past it are listed without text."""
DEFAULT_APPROVED = "plan/{block}.approved.json"

IMITATE = (
    "imitate this style: indentation, begin/end placement, port declarations, reset and "
    "clocking, signal suffixes, comments. Do not copy its logic."
)


def _naming_ref(resolved: ResolvedProfile, block: str) -> str | None:
    profile = resolved.for_block(block)
    if profile.naming.rules is not None:
        return profile.naming.rules
    cfg = profile.adapters.get("naming")
    dumped = cfg.model_dump() if cfg is not None else {}
    rules = dumped.get("rules")
    return rules if dumped.get("use") == "naming" and isinstance(rules, str) else None


def naming_text(rules: NamingRules, ref: str) -> str:
    """The naming rules as instructions a model can follow."""
    lines = [
        f"Naming rules ({rules.document} v{rules.version}, from {ref}). Every name you "
        "declare must follow them; the `naming` check rejects the module otherwise.",
        "",
        "Allowed names, per kind (the whole name must match the pattern):",
    ]
    for kind, rule in rules.identifiers.items():
        lines.append(f"- {kind}: `{rule.pattern}` ({rule.rule}): {rule.message}")
    if rules.lexical:
        lines.extend(["", "Lexical rules, for every name:"])
        for lex in rules.lexical:
            unless = f", unless it matches `{lex.allow_if_matches}`" if lex.allow_if_matches else ""
            lines.append(f"- {lex.rule}: no match of `{lex.forbid_pattern}`{unless}: {lex.message}")
    if rules.vocabulary is not None and rules.vocabulary.replace:
        words = ", ".join(f"{bad} -> {good}" for bad, good in rules.vocabulary.replace.items())
        lines.extend(["", f"Vocabulary ({rules.vocabulary.rule}): {words}"])
    lines.extend(
        [
            "",
            f"Do not use the exemption comment `// {rules.ignore_comment}`: it is for people.",
        ]
    )
    return "\n".join(lines) + "\n"


def _naming(root: Path, resolved: ResolvedProfile, block: str) -> dict[str, Any] | None:
    ref = _naming_ref(resolved, block)
    if ref is None:
        return None
    try:
        rules, _ = _load_rules(_resolve_rules_path(ref, root))
    except _RulesError as exc:
        return {"rules": ref, "note": str(exc)}
    return {"rules": ref, "text": naming_text(rules, ref)}


def _exemplars(
    root: Path,
    resolved: ResolvedProfile,
    is_denied: Callable[[str], bool],
) -> dict[str, Any]:
    style = resolved.profile.style
    entries: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    used = 0
    for rel in style.exemplars:
        entry: dict[str, Any] = {"path": rel}
        path = root / rel
        if is_denied(rel):
            entry["refused"] = (
                f"{rel} is a file this role must not read (a testbench or test): it is not "
                "shown. Remove it from the profile's style.exemplars."
            )
        elif not path.is_file():
            entry["missing"] = True
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
            texts[rel] = text
            if used >= MAX_EXEMPLARS_CHARS:
                entry["omitted"] = "the context's exemplar budget is used up"
            else:
                cap = min(MAX_EXEMPLAR_CHARS, MAX_EXEMPLARS_CHARS - used)
                if len(text) > cap:
                    entry["truncated"] = True
                    text = text[:cap]
                used += len(text)
                entry["label"] = IMITATE
                entry["content"] = text
        entries.append(entry)
    out: dict[str, Any] = {"exemplars": entries, "enforce": list(style.enforce)}
    if texts:
        profile = style_profile(texts)
        out["profile"] = profile.describe()
    return out


def _ports(ports: Sequence[Any], source: str) -> list[dict[str, Any]]:
    return [
        {"name": p.name, "direction": p.direction, "width": p.width, "source": source}
        for p in ports
    ]


def _interface(
    plan: Plan, module: PlanModule, env: PlanEnv, slice_: Mapping[str, Any]
) -> dict[str, Any]:
    model_ports = {p.name: p for p in env.interface}
    ports: list[dict[str, Any]] = []
    for port in module.interface.ports:
        known = model_ports.get(port.name)
        if known is not None and not port.internal:
            ports.extend(_ports([known], "model"))
        else:
            ports.extend(_ports([port], "plan"))
    return {
        "module": module.name,
        "top": plan.top == module.name,
        "model_interface": env.interface_source,
        "ports": ports,
        "clock": slice_.get("clock"),
        "reset": slice_.get("reset"),
    }


def _dependencies(plan: Plan, module: PlanModule, env: PlanEnv) -> list[dict[str, Any]]:
    existing = {m.name: m for m in env.existing_modules}
    out: list[dict[str, Any]] = []
    for name in dict.fromkeys(module.depends_on):
        found = existing.get(name)
        planned = plan.module(name)
        if found is not None:
            out.append({"name": name, "source": "model", "ports": _ports(found.ports, "model")})
        elif planned is not None:
            out.append(
                {
                    "name": name,
                    "source": "plan",
                    "summary": planned.summary,
                    "ports": _ports(planned.interface.ports, "plan"),
                }
            )
    return out


def approved_plan_path(root: Path, block: str, approved: str | None = None) -> str | None:
    """The plan file an approved plan artifact names (None when there is none)."""
    rel = (approved or DEFAULT_APPROVED).replace("{block}", block)
    path = root / rel
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plan = data.get("plan") if isinstance(data, dict) else None
    return plan if isinstance(plan, str) else None


def rtl_author_context(
    root: Path,
    resolved: ResolvedProfile,
    rules: Sequence[RuleSpec],
    *,
    block: str,
    module: str | None,
    model: DesignModel | None,
    is_denied: Callable[[str], bool],
    approved: str | None = None,
) -> dict[str, Any]:
    """The `rtl` section of an `rtl-author` task's context (see the module docstring)."""
    out: dict[str, Any] = {"block": block, "module_name": module}
    out["naming"] = _naming(root, resolved, block)
    out["style"] = _exemplars(root, resolved, is_denied)
    slice_ = spec_slice(model, block) if model is not None else {}
    plan_path = approved_plan_path(root, block, approved)
    if module is None or plan_path is None:
        out["note"] = "no approved plan entry for this task: write from the spec and model"
        out["requirements"] = list(slice_.get("requirements", []))
        return out
    try:
        report = check_plan_file(
            root, block, plan_path=plan_path, resolved=resolved, rules=rules, check_existing=False
        )
    except PlanEnvError as exc:
        out["note"] = str(exc)
        return out
    plan, env = report.plan, report.env
    entry = plan.module(module) if plan is not None else None
    if plan is None or env is None or entry is None:
        out["note"] = f"module {module!r} is not in the plan {plan_path}"
        return out
    wanted = set(entry.reqs)
    out["module"] = {
        **entry.model_dump(mode="json"),
        "plan": plan_path,
        "plan_top": plan.top,
        "plan_assumptions": list(plan.assumptions),
        "plan_open_questions": list(plan.open_questions),
    }
    out["requirements"] = [
        {"id": r.ref, "text": r.text} for r in env.requirements if r.refs & wanted
    ]
    out["interface"] = _interface(plan, entry, env, slice_)
    out["dependencies"] = _dependencies(plan, entry, env)
    return out


def spec_side(model: DesignModel, block: str, reqs: Sequence[str] | None) -> dict[str, Any]:
    """The block's spec slice with only the requirements in `reqs` (all when None)."""
    slice_ = spec_slice(model, block)
    if reqs is not None:
        wanted = set(reqs)
        slice_["requirements"] = [r for r in slice_["requirements"] if r["id"] in wanted]
    return slice_


__all__ = [
    "IMITATE",
    "MAX_EXEMPLARS_CHARS",
    "MAX_EXEMPLAR_CHARS",
    "approved_plan_path",
    "naming_text",
    "rtl_author_context",
    "spec_side",
]

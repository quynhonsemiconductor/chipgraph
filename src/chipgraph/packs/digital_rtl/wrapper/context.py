"""What an `rtl-author` task of rule `digital-rtl/wrapper` gets: the `rtl.wrapper` section.

The author writes a wrapper's source, never its expansion, and cannot run emacs. Its
context holds what the source is written from, and no IP body:

- `wrapper`: the profile's entry (name, block, IP module, IP files), the source it writes
  and the file the generator writes from it;
- `vendor`: the IP module's declaration only, read here with pyslang from the IP files:
  its parameters and its ports (name, direction, type, packed and unpacked dimensions),
  never a line of its body, a comment or a line number;
- `interface`: the wrapper's own ports when the project knows them: the Design Model's
  (the block's spec ports when the wrapper is the block's top module, else the model's
  RTL ports of the module), else the block's approved plan's entry of that name; else
  none, and the author names them from the IP ports and the naming rules;
- `naming` (next to `wrapper`, as for an RTL module): the project's naming rules as text;
- `exemplar`: the profile's wrapper exemplar, when it lists one (its text is an input
  of the task);
- `generator`: how the source is expanded (the variables the generator pins, the Lisp
  the source may carry), so the author knows what emacs will do with its template.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from chipgraph.core.config.loader import ResolvedProfile
from chipgraph.core.contracts import RuleSpec
from chipgraph.core.model import DesignModel
from chipgraph.packs.digital_rtl.plan.model import Plan
from chipgraph.packs.digital_rtl.rtl.context import _naming, approved_plan_path
from chipgraph.packs.digital_rtl.wrapper import lisp
from chipgraph.packs.digital_rtl.wrapper.common import (
    GEN_RULE,
    Wrapper,
    WrapperError,
    find_wrapper,
    inside,
    paths,
)
from chipgraph.packs.digital_rtl.wrapper.emacs import PINNED
from chipgraph.packs.dv.declaration import ModuleDeclaration, extract_declaration
from chipgraph.packs.dv.interface import _rtl_ports, interface_for, top_module

INSTRUCTIONS = (
    " Everything about the wrapper is in `rtl.wrapper`: the IP module's port declaration "
    "(`vendor`), the wrapper's ports when the project knows them (`interface`), the naming "
    "rules (`rtl.naming.text`) and how the generator expands your source (`generator`). Write "
    "the source only (the template and the structure, never the expanded port list): on "
    "submit the engine expands it with emacs and `wrap_check` reports what the expansion "
    "got wrong, with the expanded lines."
)
"""What `get_context` adds to a wrapper task's instructions."""


_SHOWN = frozenset(
    {
        "verilog-auto-inst-param-value",
        "verilog-auto-inst-vector",
        "verilog-auto-inst-sort",
        "verilog-auto-arg-sort",
        "verilog-auto-star-expand",
        "verilog-auto-input-ignore-regexp",
        "verilog-auto-output-ignore-regexp",
        "verilog-case-fold",
    }
)
"""The pinned variables the author's template can depend on (the rest is layout)."""


def _vendor(root: Path, wrapper: Wrapper) -> tuple[dict[str, Any], ModuleDeclaration | None]:
    files = []
    for rel in wrapper.cfg.vendor_files:
        try:
            files.append(inside(root, rel))
        except WrapperError:
            continue
    decl = extract_declaration(files, wrapper.cfg.vendor_module)
    if decl is None:
        return {
            "module": wrapper.cfg.vendor_module,
            "note": (
                f"no declaration of `{wrapper.cfg.vendor_module}` was found in the IP files: "
                "report needs_human"
            ),
        }, None
    view = decl.model_dump(mode="json", exclude={"schema_version"})
    for item in (*view["parameters"], *view["ports"]):
        item.pop("schema_version", None)
    view["note"] = (
        "the IP module's header only (parameters and ports), read by the engine: map every "
        "port in the AUTO_TEMPLATE; never edit the IP's files"
    )
    return view, decl


def _plan_ports(root: Path, block: str, name: str) -> list[dict[str, Any]] | None:
    rel = approved_plan_path(root, block)
    if rel is None:
        return None
    try:
        plan = Plan.model_validate(yaml.safe_load((root / rel).read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    module = plan.module(name)
    if module is None:
        return None
    return [
        {"name": p.name, "direction": p.direction, "width": p.width, "source": "plan"}
        for p in module.interface.ports
    ]


def _interface(root: Path, model: DesignModel | None, block: str, name: str) -> dict[str, Any]:
    if model is not None:
        if top_module(model, block) == name:
            known = interface_for(name, model=model, block=block)
            if known.source in ("spec", "model_rtl"):
                return {
                    "source": known.source,
                    "ports": [
                        {"name": p.name, "direction": p.direction, "width": p.width}
                        for p in known.ports
                    ],
                    "note": "the wrapper is the block's top module: these are its ports",
                }
        rtl = _rtl_ports(model, name)
        if rtl:
            return {
                "source": "model_rtl",
                "ports": [
                    {"name": p.name, "direction": p.direction, "width": p.width} for p in rtl
                ],
                "note": "the Design Model's ports of the existing wrapper: keep them",
            }
    planned = _plan_ports(root, block, name)
    if planned:
        return {"source": "plan", "ports": planned, "note": "the approved plan's ports"}
    return {
        "source": None,
        "ports": [],
        "note": (
            "the project names no ports for this wrapper yet: derive each one from an IP "
            "port, renamed by the naming rules (the AUTO_TEMPLATE does the renaming)"
        ),
    }


def _out_template(rules: Sequence[RuleSpec]) -> str | None:
    rule = next((r for r in rules if r.id == GEN_RULE), None)
    if rule is None or rule.run is None:
        return None
    out = rule.run.args.get("out")
    return out if isinstance(out, str) else None


def wrapper_author_context(
    root: Path,
    resolved: ResolvedProfile,
    rules: Sequence[RuleSpec],
    *,
    params: dict[str, str],
    outputs: Sequence[str],
    model: DesignModel | None,
    is_denied: Callable[[str], bool],
) -> dict[str, Any]:
    """The `rtl` section of a wrapper task's context (see the module docstring)."""
    block = params.get("block")
    out: dict[str, Any] = {"block": block, "wrapper_name": params.get("wrapper")}
    try:
        wrapper = find_wrapper(resolved.profile, params)
    except WrapperError as exc:
        out["note"] = f"{exc}: report needs_human"
        return out
    src_default, gen_default = paths(wrapper, None, _out_template(rules))
    source = outputs[0] if outputs else src_default
    vendor, _ = _vendor(root, wrapper)
    entry: dict[str, Any] = {
        "name": wrapper.name,
        "block": wrapper.block,
        "source": source,
        "generated": gen_default,
        "vendor_module": wrapper.cfg.vendor_module,
        "vendor_files": list(wrapper.cfg.vendor_files),
        "vendor": vendor,
        "interface": _interface(root, model, wrapper.block, wrapper.name),
        "generator": {
            "tool": "emacs_auto",
            "command": "emacs -Q --batch <source copy> -f verilog-batch-auto",
            "pinned": {k: v for k, v in PINNED if k in _SHOWN},
            "pinned_others": f"{len(PINNED) - len(_SHOWN)} more verilog-mode variables, "
            "at verilog-mode's defaults or fixed for layout only",
            "library": "the IP files above (verilog-library-files); nothing else is read",
            "file_local_variables": "ignored: do not write a Local Variables block",
            "lisp_functions": sorted(lisp.FUNCTIONS),
            "lisp_variables": sorted(lisp.VARIABLES),
            "auto_lisp_may_set": lisp.SETQ_VARIABLE.pattern,
        },
    }
    exemplar = wrapper.cfg.exemplar
    if exemplar is not None:
        entry["exemplar"] = (
            {"path": exemplar, "refused": "a file this role must not read: it is not shown"}
            if is_denied(exemplar)
            else {
                "path": exemplar,
                "label": (
                    "a wrapper of this project: imitate its structure (port groups, comments, "
                    "template layout), not its IP or its names"
                ),
                "note": "its text is in this task's `inputs`",
            }
        )
    out["wrapper"] = entry
    out["naming"] = _naming(root, resolved, wrapper.block)
    return out


__all__ = ["INSTRUCTIONS", "wrapper_author_context"]

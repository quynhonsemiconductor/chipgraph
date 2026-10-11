"""The context of a task whose role must not see some artifacts (DESIGN.md 5.1, 5.3 F1).

`get_context` of runtime `claude-code` uses one of these for a task of a role with
`read_policy.deny_kinds` (`guarded_context` picks it): `GuardedContext` for a role that
must not see the RTL (the testbench Author), `RtlAuthorContext` for one that must not see
the testbench (the RTL Author). Both give the model inputs, the extra context sections
and the block's spec documents; the read policy itself (what is refused, what is
filtered) is the same engine for both (`chipgraph.core.runtime.roles.policy`).

**Not the RTL** (`GuardedContext`): its Design Model inputs are not the general model
slices, which name modules, RTL ports and RTL files, but

- ``block/<b>``: the block's spec side (`chipgraph.packs.dv.interface.spec_slice`):
  requirements with their ids, registers and fields, interrupts, memory regions, clock
  and reset;
- ``interface/<name>`` (a module, or a block for its top module): the interface from
  `project_interface` (spec ports, else the model's RTL ports, else the module's
  declaration only), also given as the context's `interface` section;
- anything else: a note, no content.

The context also gets a `requirements` section (every requirement of the task's block,
with its id), and the block's spec documents (the profile layout's `spec` paths) as
inputs when the rule does not list them. Nothing else derived from RTL goes in: no RTL
file, body text, line number or path.

**Not the testbench** (`RtlAuthorContext`, M2-04): the model input ``block/<b>`` is the
block's spec side with the requirements cut to the module's, ``interface/<module>`` points
to the `rtl` section, and the `rtl` section holds the module's approved plan entry, its
requirements, interface and dependencies, the naming rules as text and the style
exemplars (`chipgraph.packs.digital_rtl.rtl.context`). An exemplar the role must not read
is refused there. No other project file's text. A wrapper's task (a `wrapper` param,
M2-12) gets `rtl.wrapper` instead: the IP's port declaration, the wrapper's ports and the
naming rules (`chipgraph.packs.digital_rtl.wrapper.context`), never the IP's body.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from chipgraph.app.context import AppContext
from chipgraph.core.contracts import RuleInstance, RuleSpec
from chipgraph.core.model import DesignModel, ModelStore, ModelStoreError, default_model_db_path
from chipgraph.core.runtime.roles import RoleSpec
from chipgraph.packs.dv.interface import (
    Interface,
    block_requirements,
    project_interface,
    spec_slice,
)

TB_STATIC = "tb_static"
"""The adapter id whose args (`top`, `files`, `filelist`) say where a module's declaration
is, so the context and the `tb_static` check see the same interface."""


def spec_documents(ctx: AppContext, block: str | None) -> list[str]:
    """The block's spec documents that exist: the profile layout's `spec` paths."""
    if block is None:
        return []
    value = ctx.require_profile().for_block(block).layout.get("spec")
    templates = [value] if isinstance(value, str) else list(value or ())
    found: list[str] = []
    for template in templates:
        rel = template.replace("{block}", block).replace("{BLOCK}", block.upper())
        if "{" in rel:
            continue
        paths = sorted(ctx.root.glob(rel)) if set("*?[") & set(rel) else [ctx.root / rel]
        for path in paths:
            if path.is_file():
                name = path.relative_to(ctx.root).as_posix()
                if name not in found:
                    found.append(name)
    return found


def _load_model(ctx: AppContext) -> DesignModel | None:
    db = default_model_db_path(ctx.layout.root)
    if not db.is_file():
        return None
    try:
        return ModelStore(db).read()
    except (ModelStoreError, OSError, ValueError):
        return None


class GuardedContext:
    """The model inputs and extra sections of one task that must not see RTL."""

    instructions = (
        " The ports you may use are in `interface` (every port names its `source`), "
        "the block's requirements in `requirements`."
    )
    """What `get_context` adds to the task's instructions."""

    def __init__(self, ctx: AppContext, instance: RuleInstance) -> None:
        self.ctx = ctx
        self.instance = instance
        self.block = instance.params.get("block")
        self._model: DesignModel | None = None
        self._loaded = False
        self.interface: Interface | None = None

    @property
    def model(self) -> DesignModel | None:
        """The ingested Design Model, or None when there is none yet."""
        if not self._loaded:
            self._loaded = True
            self._model = _load_model(self.ctx)
        return self._model

    def _profile_parts(self, block: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
        resolved = self.ctx.require_profile()
        profile = resolved.for_block(block) if block is not None else resolved.profile
        cfg = profile.adapters.get(TB_STATIC)
        args = cfg.model_dump() if cfg is not None else {}
        return args, dict(profile.layout)

    def _interface(self, name: str) -> Interface:
        blocks = self.ctx.require_profile().profile.blocks
        params = dict(self.instance.params)
        block: str | None
        if name in blocks or name == self.block:
            block = name
        else:
            block = self.block
            params["module"] = name
        args, layout = self._profile_parts(block)
        return project_interface(
            self.ctx.root, model=self.model, block=block, params=params, args=args, layout=layout
        )

    def spec_documents(self) -> list[str]:
        """The block's spec documents that exist: the profile layout's `spec` paths."""
        return spec_documents(self.ctx, self.block)

    def model_input(self, key: str) -> dict[str, Any]:
        """The context entry of the model input `key`."""
        entry: dict[str, Any] = {"model_key": key}
        kind, _, name = key.partition("/")
        if kind == "interface" and name:
            self.interface = self._interface(name)
            entry["section"] = "interface"
            entry["note"] = "see the context's `interface` section"
            return entry
        if kind == "block" and name:
            model = self.model
            if model is None:
                entry["note"] = "not available from the Design Model (run `chipgraph ingest` first)"
                return entry
            entry["content"] = json.dumps(spec_slice(model, name), indent=1, sort_keys=True)
            return entry
        entry["note"] = f"model input {key!r} is not available to this role"
        return entry

    def sections(self) -> dict[str, Any]:
        """The extra context sections: `interface` (when an input asked for it) and
        `requirements` (the block's, with ids)."""
        out: dict[str, Any] = {}
        if self.interface is not None:
            out["interface"] = self.interface.view()
        model = self.model
        if model is not None and self.block is not None:
            out["requirements"] = block_requirements(model, self.block)
        return out


class RtlAuthorContext:
    """The model inputs and extra sections of one task that must not see a testbench."""

    instructions = (
        " Everything about the module is in `rtl`: its approved plan entry (`rtl.module`), "
        "its requirements with ids, its ports (`rtl.interface`, every port names its "
        "`source`), the modules it connects to, the naming rules (`rtl.naming.text`) and "
        "the style exemplars to imitate (`rtl.style`). The engine formats your file before "
        "the other checks run."
    )
    """What `get_context` adds to the task's instructions."""

    def __init__(
        self,
        ctx: AppContext,
        instance: RuleInstance,
        rules: Sequence[RuleSpec],
        is_denied: Callable[[str], bool],
    ) -> None:
        self.ctx = ctx
        self.instance = instance
        self.rules = list(rules)
        self.is_denied = is_denied
        self.block = instance.params.get("block")
        self.module = instance.params.get("module")
        self.wrapper = instance.params.get("wrapper")
        self.model = _load_model(ctx)
        self._section: dict[str, Any] | None = None
        if self.wrapper is not None:  # a wrapper's source (M2-12): `rtl.wrapper`
            from chipgraph.packs.digital_rtl.wrapper.context import INSTRUCTIONS

            self.instructions = INSTRUCTIONS

    def section(self) -> dict[str, Any]:
        """The `rtl` section (built once)."""
        if self._section is None:
            from chipgraph.packs.digital_rtl.rtl.context import rtl_author_context

            if self.block is None:
                self._section = {"note": "this task has no block param"}
            elif self.wrapper is not None:
                from chipgraph.packs.digital_rtl.wrapper.context import wrapper_author_context

                self._section = wrapper_author_context(
                    self.ctx.root,
                    self.ctx.require_profile(),
                    self.rules,
                    params=dict(self.instance.params),
                    outputs=[r.path for r in self.instance.outputs if r.path is not None],
                    model=self.model,
                    is_denied=self.is_denied,
                )
            else:
                self._section = rtl_author_context(
                    self.ctx.root,
                    self.ctx.require_profile(),
                    self.rules,
                    block=self.block,
                    module=self.module,
                    model=self.model,
                    is_denied=self.is_denied,
                )
        return self._section

    def spec_documents(self) -> list[str]:
        """The block's spec documents that exist: the profile layout's `spec` paths."""
        return spec_documents(self.ctx, self.block)

    def model_input(self, key: str) -> dict[str, Any]:
        """The context entry of the model input `key`."""
        from chipgraph.packs.digital_rtl.rtl.context import spec_side

        entry: dict[str, Any] = {"model_key": key}
        kind, _, name = key.partition("/")
        if kind == "interface" and name and self.wrapper is not None:
            entry["section"] = "rtl.wrapper.interface"
            entry["note"] = "see the context's `rtl.wrapper.interface` and `rtl.wrapper.vendor`"
            return entry
        if kind == "interface" and name:
            entry["section"] = "rtl.interface"
            entry["note"] = "see the context's `rtl.interface` and `rtl.dependencies`"
            return entry
        if kind == "block" and name:
            if self.model is None:
                entry["note"] = "not available from the Design Model (run `chipgraph ingest` first)"
                return entry
            module = self.section().get("module")
            reqs = list(module.get("reqs", ())) if isinstance(module, dict) else None
            side = spec_side(self.model, name, reqs)
            entry["content"] = json.dumps(side, indent=1, sort_keys=True)
            return entry
        entry["note"] = f"model input {key!r} is not available to this role"
        return entry

    def sections(self) -> dict[str, Any]:
        """The extra context section: `rtl`."""
        return {"rtl": self.section()}


def guarded_context(
    ctx: AppContext,
    role: RoleSpec | None,
    instance: RuleInstance,
    rules: Sequence[RuleSpec],
    is_denied: Callable[[str], bool],
) -> GuardedContext | RtlAuthorContext | None:
    """The guarded context of a task whose role must not see some kinds, else None.

    A role that must not see `rtl` gets `GuardedContext`; one that must not see `tb`
    (and may see RTL) gets `RtlAuthorContext`.
    """
    if role is None or role.read_policy.mode != "deny" or not role.read_policy.deny_kinds:
        return None
    kinds = role.read_policy.deny_kinds
    if "rtl" in kinds:
        return GuardedContext(ctx, instance)
    if "tb" in kinds:
        return RtlAuthorContext(ctx, instance, rules, is_denied)
    return None


__all__ = ["TB_STATIC", "GuardedContext", "RtlAuthorContext", "guarded_context", "spec_documents"]

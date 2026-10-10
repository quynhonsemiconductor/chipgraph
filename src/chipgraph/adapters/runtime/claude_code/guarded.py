"""The context of a task whose role must not see some artifacts (the testbench Author).

`get_context` of runtime `claude-code` uses this for a task of a role with
`read_policy.deny_kinds` (DESIGN.md 5.1, 5.3 F1): its Design Model inputs are not the
general model slices, which name modules, RTL ports and RTL files, but

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
"""

from __future__ import annotations

import json
from typing import Any

from chipgraph.app.context import AppContext
from chipgraph.core.contracts import RuleInstance
from chipgraph.core.model import DesignModel, ModelStore, ModelStoreError, default_model_db_path
from chipgraph.packs.dv.interface import (
    Interface,
    block_requirements,
    project_interface,
    spec_slice,
)

TB_STATIC = "tb_static"
"""The adapter id whose args (`top`, `files`, `filelist`) say where a module's declaration
is, so the context and the `tb_static` check see the same interface."""


class GuardedContext:
    """The model inputs and extra sections of one task that must not see RTL."""

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
            db = default_model_db_path(self.ctx.layout.root)
            if db.is_file():
                try:
                    self._model = ModelStore(db).read()
                except (ModelStoreError, OSError, ValueError):
                    self._model = None
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
        if self.block is None:
            return []
        _, layout = self._profile_parts(self.block)
        value = layout.get("spec")
        templates = [value] if isinstance(value, str) else list(value or ())
        found: list[str] = []
        for template in templates:
            rel = template.replace("{block}", self.block).replace("{BLOCK}", self.block.upper())
            if "{" in rel:
                continue
            paths = (
                sorted(self.ctx.root.glob(rel)) if set("*?[") & set(rel) else [self.ctx.root / rel]
            )
            for path in paths:
                if path.is_file():
                    name = path.relative_to(self.ctx.root).as_posix()
                    if name not in found:
                        found.append(name)
        return found

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


__all__ = ["TB_STATIC", "GuardedContext"]

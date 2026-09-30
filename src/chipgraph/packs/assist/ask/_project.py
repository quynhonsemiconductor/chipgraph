"""The project as `/ask` sees it: the stored Design Model, its indexed documents, labels.

`AskProject.load` opens the project's model store (it never ingests: a missing store is
an error telling the user to run `chipgraph ingest`). Entities read from an `nda` file are
hidden from `/ask` like `nda` documents are: never shown, never citable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.core.model import (
    DesignModel,
    ModelQuery,
    ModelStore,
    QueryError,
    default_model_db_path,
)
from chipgraph.core.model.entities import EntityBase
from chipgraph.core.state.artifacts import LabelRules
from chipgraph.packs.assist.ask._db import DocumentLines

_HEX_FIELDS = frozenset({"offset", "base", "address"})
_SKIP_FIELDS = frozenset({"schema_version", "key", "kind", "name", "source"})
_MAX_LIST = 24


@dataclass
class AskProject:
    """The stored model, a query API on it, the indexed documents and the data labels."""

    root: Path
    store: ModelStore
    model: DesignModel
    query: ModelQuery
    docs: DocumentLines
    labels: LabelRules

    @classmethod
    def load(cls, ctx: AppContext) -> AskProject:
        """Open the project's model store; `AppError` if there is none yet."""
        db_path = default_model_db_path(ctx.root)
        if not db_path.is_file():
            raise AppError("no Design Model yet: run `chipgraph ingest`")
        store = ModelStore(db_path)
        model = store.read()
        return cls(
            root=ctx.root,
            store=store,
            model=model,
            query=ModelQuery(model, store),
            docs=DocumentLines(db_path),
            labels=ctx.store.labels,
        )

    # --- visibility -------------------------------------------------------------------

    def visible(self, entity: EntityBase) -> bool:
        """False for an entity read from an `nda` file: `/ask` never shows or cites it."""
        file = entity.source.file
        return not (file and self.labels.label_for(file) == "nda")

    def get(self, key: str) -> EntityBase | None:
        """The visible entity `key`, or None."""
        entity = self.model.get(key)
        return entity if entity is not None and self.visible(entity) else None

    # --- entity facts -----------------------------------------------------------------

    def block_of(self, entity: EntityBase) -> str | None:
        """The block key an entity belongs to, from its typed fields, attrs or owner."""
        block = getattr(entity, "block", None)
        if isinstance(block, str) and block:
            return block
        attr = entity.attrs.get("block")
        if isinstance(attr, str) and attr:
            return attr
        if entity.kind == "block":
            return entity.key
        for owner_field in ("module", "register"):
            owner_key = getattr(entity, owner_field, None)
            if isinstance(owner_key, str) and owner_key:
                owner = self.model.get(owner_key)
                if owner is not None:
                    return self.block_of(owner)
        # Keys like `open_item:<block>.<id>` or `register:<block>.<name>`.
        _, _, rest = entity.key.partition(":")
        head, dot, _ = rest.partition(".")
        if dot and f"block:{head}" in self.model.entities:
            return f"block:{head}"
        return None

    def blocks_of(self, entity: EntityBase) -> set[str]:
        """Every block an entity belongs to: `block_of`, plus an `attrs.blocks` name list."""
        blocks = {b for b in (self.block_of(entity),) if b}
        names = entity.attrs.get("blocks")
        if isinstance(names, list):
            blocks |= {f"block:{n}" for n in names if isinstance(n, str) and n}
        return blocks

    def module_of(self, entity: EntityBase) -> str | None:
        module = getattr(entity, "module", None)
        if isinstance(module, str) and module:
            return module
        return entity.key if entity.kind == "module" else None

    def defined_at(self, entity: EntityBase) -> str | None:
        """`path:line` where the entity was read from, when that file is indexed."""
        file, line = entity.source.file, entity.source.line
        if file and line and self.docs.is_indexed(file):
            return f"{file}:{line}"
        return None

    def summary(self, entity: EntityBase) -> str:
        """A one-line, factual summary of an entity, from its typed fields and relations."""
        parts = [f"{entity.kind} {entity.name}"]
        facts = _fields(entity)
        extra = self._relations_summary(entity)
        body = "; ".join(f"{k}={_fmt(k, v)}" for k, v in facts.items())
        if extra:
            body = f"{body}; {extra}" if body else extra
        return f"{parts[0]}: {body}" if body else parts[0]

    def _relations_summary(self, entity: EntityBase) -> str:
        try:
            if entity.kind == "block":
                return self._block_summary(entity)
            if entity.kind == "module":
                return self._module_summary(entity)
            if entity.kind == "requirement":
                trace = self.query.trace(entity.key)
                bits = []
                verifies = self._visible_keys(trace.verifies)
                implements = self._visible_keys(trace.implements)
                if verifies:
                    bits.append(f"verified_by={_names(verifies)}")
                if implements:
                    bits.append(f"implemented_by={_names(implements)}")
                return "; ".join(bits)
        except QueryError:
            return ""
        children = self._visible_keys(
            [r.dst for r in self.model.relations if r.src == entity.key and r.kind == "contains"]
        )
        if children:
            return f"contains={_names(children)}"
        return ""

    def _block_summary(self, block: EntityBase) -> str:
        info = self.query.block(block.name)
        members: dict[str, list[str]] = {}
        for entity in self.model.entities.values():
            if entity.kind in ("block", "port", "field") or not self.visible(entity):
                continue
            if self.block_of(entity) == block.key:
                members.setdefault(entity.kind, []).append(entity.key)
        for kind, keys in (("module", info.modules), ("clock", info.clocks)):
            for key in self._visible_keys(keys):
                if key not in members.setdefault(kind, []):
                    members[kind].append(key)
        bits = [f"{kind}s={_names(sorted(keys))}" for kind, keys in sorted(members.items())]
        resets = self._visible_keys(info.resets)
        if resets:
            bits.append(f"resets={_names(resets)}")
        return "; ".join(bits)

    def _module_summary(self, module: EntityBase) -> str:
        info = self.query.module(module.name)
        ports = self._visible_keys(info.ports)
        parameters = self._visible_keys(info.parameters)
        instances = [i for i in info.instances if self.get(i["module_key"]) is not None]
        parents = [i for i in info.instantiated_by if self.get(i["module_key"]) is not None]
        bits = [f"ports={_names(ports)}"] if ports else []
        if parameters:
            bits.append(f"parameters={_names(parameters)}")
        if instances:
            bits.append(
                "instances="
                + ", ".join(f"{i['instance_name']} ({i['module_key']})" for i in instances)
            )
        if parents:
            bits.append(
                "instantiated_by="
                + ", ".join(f"{i['module_key']} as {i['instance_name']}" for i in parents)
            )
        return "; ".join(bits)

    def _visible_keys(self, keys: list[str]) -> list[str]:
        """`keys` without the entities `/ask` may not show (unknown ones are kept)."""
        return [k for k in keys if (e := self.model.get(k)) is None or self.visible(e)]


def _fields(entity: EntityBase) -> dict[str, object]:
    data = entity.model_dump(mode="json")
    out: dict[str, object] = {}
    for key, value in data.items():
        if key in _SKIP_FIELDS or value in (None, "", [], {}):
            continue
        if key == "attrs":
            for attr_key, attr_value in value.items():
                if attr_value not in (None, "", [], {}):
                    out[attr_key] = attr_value
            continue
        out[key] = value
    return out


def _fmt(key: str, value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        if key in _HEX_FIELDS:
            return f"{value:#x}"
        if value >= 0x100:
            return f"{value} ({value:#x})"
        return str(value)
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _names(keys: list[str]) -> str:
    shown = keys[:_MAX_LIST]
    more = f" (+{len(keys) - len(shown)} more)" if len(keys) > len(shown) else ""
    return "[" + ", ".join(shown) + "]" + more


__all__ = ["AskProject"]

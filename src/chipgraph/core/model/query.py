"""Query API on the Design Model.

All query results are frozen pydantic models suitable for JSON serialization.
"""

from __future__ import annotations

import difflib
from collections import defaultdict
from collections.abc import Iterable
from fnmatch import fnmatch
from typing import Any

from pydantic import BaseModel, Field

from chipgraph.core.model.entities import EntityBase
from chipgraph.core.model.model import DesignModel
from chipgraph.core.model.store import ModelStore


class QueryError(ValueError):
    """Raised when a query cannot be satisfied (unknown key, name, etc.).

    Message includes close matches (via difflib) to help the user find what they meant.
    """


def _close_matches(name: str, choices: Iterable[str]) -> list[str]:
    """Return the N closest matches from `choices` to `name`, in score descending order."""
    return difflib.get_close_matches(name, list(choices), n=5, cutoff=0.6)


class BlockInfo(BaseModel):
    """Result of `model.block(name)`: the block and what it contains."""

    model_config = {"frozen": True}

    key: str = Field(description="Model key of the block")
    name: str = Field(description="Block name")
    owner: str | None = Field(default=None, description="Block owner")
    path: str | None = Field(default=None, description="Repo-relative path")
    modules: list[str] = Field(default_factory=list, description="Model keys of contained modules")
    ports: list[str] = Field(
        default_factory=list, description="Model keys of ports from contained modules"
    )
    registers: list[str] = Field(
        default_factory=list, description="Model keys of contained registers"
    )
    interrupts: list[str] = Field(
        default_factory=list, description="Model keys of contained interrupts"
    )
    memory_regions: list[str] = Field(
        default_factory=list, description="Model keys of memory regions"
    )
    clocks: list[str] = Field(
        default_factory=list, description="Model keys of clocks (from contained modules)"
    )
    resets: list[str] = Field(
        default_factory=list, description="Model keys of resets (from contained modules)"
    )
    requirements: list[str] = Field(
        default_factory=list, description="Model keys of requirements for this block"
    )
    open_items: list[str] = Field(
        default_factory=list, description="Model keys of open items for this block"
    )


class ModuleInfo(BaseModel):
    """Result of `model.module(name)`: a single RTL module and its contents."""

    model_config = {"frozen": True}

    key: str = Field(description="Model key of the module")
    name: str = Field(description="Module name")
    file: str | None = Field(default=None, description="Repo-relative file this module is in")
    block: str | None = Field(default=None, description="Model key of the owning block")
    ports: list[str] = Field(default_factory=list, description="Model keys of this module's ports")
    parameters: list[str] = Field(
        default_factory=list, description="Model keys of this module's parameters"
    )
    instances: list[dict[str, str]] = Field(
        default_factory=list,
        description="Child modules: list of {instance_name, module_key} dicts",
    )
    instantiated_by: list[dict[str, str]] = Field(
        default_factory=list,
        description="Parent modules: list of {instance_name, module_key} dicts",
    )


class EntityMatch(BaseModel):
    """A single entity matching a `find` query."""

    model_config = {"frozen": True}

    key: str = Field(description="Model key")
    kind: str = Field(description="Entity kind")
    name: str = Field(description="Entity name")
    source: str = Field(description="Source file or description")


class TraceResult(BaseModel):
    """Result of `model.trace(key)`: requirements and links."""

    model_config = {"frozen": True}

    key: str = Field(description="The entity key being traced")
    kind: str = Field(description="The entity kind")
    name: str = Field(description="The entity name")
    # For requirement entities:
    implements: list[str] = Field(
        default_factory=list, description="Entities implementing this requirement"
    )
    verifies: list[str] = Field(
        default_factory=list, description="Tests verifying this requirement"
    )
    derives_from: list[str] = Field(
        default_factory=list, description="Requirements this requirement derives from"
    )
    no_test: bool = Field(
        default=False, description="True if this requirement has no verifying test"
    )
    no_implementation: bool = Field(
        default=False, description="True if this requirement has no implementation"
    )
    # For any entity:
    traced_by: list[str] = Field(
        default_factory=list,
        description="Model keys of requirements that trace to this entity",
    )


class ImpactNode(BaseModel):
    """A single entity in an impact graph result."""

    model_config = {"frozen": True}

    key: str = Field(description="Model key")
    kind: str = Field(description="Entity kind")
    name: str = Field(description="Entity name")
    path: list[str] = Field(
        description="Path from the root to this node (as relation kinds and entity keys)"
    )


class ImpactResult(BaseModel):
    """Result of `model.impact(key)`: downstream entities grouped by kind."""

    model_config = {"frozen": True}

    key: str = Field(description="The change root key")
    max_depth: int = Field(description="Depth limit for the traversal")
    by_kind: dict[str, list[ImpactNode]] = Field(
        default_factory=dict, description="Entities grouped by their kind"
    )


class NeighborResult(BaseModel):
    """Result of `model.neighbors(key)`: adjacent entities."""

    model_config = {"frozen": True}

    key: str = Field(description="The entity key")
    incoming: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Relations where this entity is the destination: {src_key, relation_kind}",
    )
    outgoing: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Relations where this entity is the source: {dst_key, relation_kind}",
    )


class SearchResult(BaseModel):
    """A single result from full-text search."""

    model_config = {"frozen": True}

    text: str = Field(description="Matched text snippet")
    score: float = Field(description="Relevance score (higher is better)")
    citation: str = Field(description="Model key or file:line")


class ModelQuery:
    """The query API on a `DesignModel`, with optional backing `ModelStore` for search."""

    def __init__(self, model: DesignModel, store: ModelStore | None = None) -> None:
        """Initialize with a model (required) and optional store (for full-text search)."""
        self.model = model
        self.store = store

    def block(self, name: str) -> BlockInfo:
        """Return the block and all contained entities."""
        # Find the block by name (exact match via find)
        all_blocks = self.model.by_kind("block")
        block = None
        for b in all_blocks:
            if b.name == name:
                block = b
                break

        if block is None:
            matches = _close_matches(name, [e.name for e in all_blocks])
            msg = f"block {name!r} not found"
            if matches:
                msg += f"; did you mean: {', '.join(matches)}"
            raise QueryError(msg)

        # Collect contained entities via relations
        modules: list[EntityBase] = []
        for rel in self.model.relations:
            if rel.src == block.key and rel.kind == "contains":
                m = self.model.get(rel.dst)
                if m and m.kind == "module":
                    modules.append(m)

        module_keys = [m.key for m in modules]

        # Collect ports and clocks/resets from modules in this block
        ports: list[EntityBase] = []
        clocks: set[str] = set()
        resets: set[str] = set()
        for mod in modules:
            for entity in self.model.entities.values():
                if entity.kind == "port" and getattr(entity, "module", None) == mod.key:
                    ports.append(entity)
                    # Collect port-level clock and reset
                    clock = getattr(entity, "clock", None)
                    if isinstance(clock, str):
                        clocks.add(clock)
                    reset = getattr(entity, "reset", None)
                    if isinstance(reset, str):
                        resets.add(reset)

        # Registers, interrupts, memory regions
        registers: list[EntityBase] = []
        interrupts: list[EntityBase] = []
        memory_regions: list[EntityBase] = []
        for entity in self.model.entities.values():
            if entity.kind == "register" and getattr(entity, "block", None) == block.key:
                registers.append(entity)
            elif entity.kind == "interrupt" and getattr(entity, "block", None) == block.key:
                interrupts.append(entity)
            elif entity.kind == "memory_region" and getattr(entity, "block", None) == block.key:
                memory_regions.append(entity)

        # Find clocks and resets by key
        block_clocks = sorted(list(clocks))
        block_resets = sorted(list(resets))

        # Requirements: find all requirements that implement/verify entities within this block
        block_entity_keys = {block.key}
        for mod in modules:
            block_entity_keys.add(mod.key)
            for port in ports:
                block_entity_keys.add(port.key)
        for reg in registers:
            block_entity_keys.add(reg.key)
        for inter in interrupts:
            block_entity_keys.add(inter.key)

        requirements_set: set[str] = set()
        for rel in self.model.relations:
            if rel.dst in block_entity_keys and rel.kind in (
                "implements",
                "verifies",
                "derives_from",
            ):
                # Get the source of the implements relation
                src_entity = self.model.get(rel.src)
                if src_entity and src_entity.kind == "requirement":
                    requirements_set.add(rel.src)
        requirements = sorted(list(requirements_set))

        # Open items: entities with kind "open_item" that relate to this block
        open_items_set: set[str] = set()
        for rel in self.model.relations:
            src_entity = self.model.get(rel.src)
            if src_entity and src_entity.kind == "open_item" and rel.dst in block_entity_keys:
                open_items_set.add(rel.src)
        open_items = sorted(list(open_items_set))

        return BlockInfo(
            key=block.key,
            name=block.name,
            owner=getattr(block, "owner", None),
            path=getattr(block, "path", None),
            modules=module_keys,
            ports=[p.key for p in ports],
            registers=[r.key for r in registers],
            interrupts=[i.key for i in interrupts],
            memory_regions=[m.key for m in memory_regions],
            clocks=block_clocks,
            resets=block_resets,
            requirements=requirements,
            open_items=open_items,
        )

    def module(self, name: str) -> ModuleInfo:
        """Return the module, its ports, parameters, instances, and who instantiates it."""
        # Find module by name
        modules = self.model.find(kind="module", name=name)
        if not modules:
            matches = _close_matches(
                name, [e.name for e in self.model.by_kind("module") if isinstance(e.name, str)]
            )
            msg = f"module {name!r} not found"
            if matches:
                msg += f"; did you mean: {', '.join(matches)}"
            raise QueryError(msg)

        mod = modules[0]

        # Ports and parameters
        ports = self.model.find(kind="port", module=mod.key)
        parameters = self.model.find(kind="parameter", module=mod.key)

        # Instances: children (this module instantiates)
        instances: list[dict[str, str]] = []
        for rel in self.model.relations:
            if rel.src == mod.key and rel.kind == "instantiates":
                child = self.model.get(rel.dst)
                if child:
                    instance_name = rel.attrs.get("instance_name", "")
                    instances.append({"instance_name": str(instance_name), "module_key": rel.dst})

        # Instantiated by: parents (which modules instantiate this)
        instantiated_by: list[dict[str, str]] = []
        for rel in self.model.relations:
            if rel.dst == mod.key and rel.kind == "instantiates":
                parent = self.model.get(rel.src)
                if parent:
                    instance_name = rel.attrs.get("instance_name", "")
                    instantiated_by.append(
                        {"instance_name": str(instance_name), "module_key": rel.src}
                    )

        return ModuleInfo(
            key=mod.key,
            name=mod.name,
            file=getattr(mod, "file", None),
            block=getattr(mod, "block", None),
            ports=[p.key for p in ports],
            parameters=[p.key for p in parameters],
            instances=instances,
            instantiated_by=instantiated_by,
        )

    def find(
        self,
        kind: str | None = None,
        name: str | None = None,
        limit: int = 100,
        **attrs: object,
    ) -> list[EntityMatch]:
        """Find entities by kind, name (glob via fnmatch), and/or attributes.

        Returns up to `limit` results. Raises QueryError if the query is invalid.
        """
        results: list[EntityBase] = []

        for entity in self.model.entities.values():
            if kind is not None and entity.kind != kind:
                continue

            if name is not None and not fnmatch(entity.name, name):
                continue

            # Check typed attributes
            if attrs and not _matches(entity, attrs):
                continue

            results.append(entity)

        results = results[:limit]
        return [
            EntityMatch(
                key=e.key,
                kind=e.kind,
                name=e.name,
                source=e.source.file or e.source.extractor or "unknown",
            )
            for e in results
        ]

    def trace(self, key: str) -> TraceResult:
        """Trace a requirement or any entity: show requirements and links.

        For requirement entities: shows implements, verifies, derives_from, and flags.
        For other entities: shows requirements that point to them.
        """
        entity = self.model.get(key)
        if entity is None:
            matches = _close_matches(key, list(self.model.entities.keys()))
            msg = f"entity {key!r} not found"
            if matches:
                msg += f"; did you mean: {', '.join(matches)}"
            raise QueryError(msg)

        implements: list[str] = []
        verifies: list[str] = []
        derives_from: list[str] = []
        no_test = False
        no_implementation = False
        traced_by: list[str] = []

        # If this is a requirement, trace outward
        if entity.kind == "requirement":
            # implements: relations where this is src and kind is "implements"
            for rel in self.model.relations:
                if rel.src == key and rel.kind == "implements":
                    implements.append(rel.dst)
                # verifies: test verifies requirement (relation dst is requirement)
                elif rel.dst == key and rel.kind == "verifies":
                    verifies.append(rel.src)
                # derives_from: relations where this is src and kind is "derives_from"
                elif rel.src == key and rel.kind == "derives_from":
                    derives_from.append(rel.dst)

            # Check for no_test and no_implementation
            no_test = len(verifies) == 0
            no_implementation = len(implements) == 0

        # Any entity: find requirements that trace to it (verifies / implements reverse)
        for rel in self.model.relations:
            if rel.dst == key and rel.kind in ("verifies", "implements"):
                traced_by.append(rel.src)

        return TraceResult(
            key=key,
            kind=entity.kind,
            name=entity.name,
            implements=implements,
            verifies=verifies,
            derives_from=derives_from,
            no_test=no_test,
            no_implementation=no_implementation,
            traced_by=traced_by,
        )

    def impact(self, key: str, max_depth: int = 5) -> ImpactResult:
        """Compute downstream impact from a change at `key`.

        Traverses `contains`, `instantiates`, `connects`, `derives_from`, `implements`,
        `verifies` relations downstream from the root, up to `max_depth` levels.
        Returns entities grouped by kind, with the path to each.
        """
        entity = self.model.get(key)
        if entity is None:
            matches = _close_matches(key, list(self.model.entities.keys()))
            msg = f"entity {key!r} not found"
            if matches:
                msg += f"; did you mean: {', '.join(matches)}"
            raise QueryError(msg)

        # Downstream relations: those that go "forward" in the design
        downstream_kinds = {
            "contains",
            "instantiates",
            "connects",
            "derives_from",
            "implements",
            "verifies",
        }

        # BFS with depth tracking
        visited: set[str] = {key}
        by_kind: dict[str, list[ImpactNode]] = defaultdict(list)

        queue: list[tuple[str, list[str], int]] = [
            (key, [], 0)
        ]  # (entity_key, path_to_here, depth)

        while queue:
            current, path, depth = queue.pop(0)

            # Find outgoing relations
            for rel in self.model.relations:
                if rel.src == current and rel.kind in downstream_kinds:
                    dst = rel.dst
                    if dst not in visited and depth < max_depth:
                        visited.add(dst)
                        # Path includes the relation kind and destination key
                        new_path = [*path, rel.kind, dst]
                        dst_entity = self.model.get(dst)
                        if dst_entity:
                            by_kind[dst_entity.kind].append(
                                ImpactNode(
                                    key=dst,
                                    kind=dst_entity.kind,
                                    name=dst_entity.name,
                                    path=new_path,
                                )
                            )
                            # Continue BFS for next level
                            queue.append((dst, new_path, depth + 1))

        # Sort each kind's list by key for determinism
        for kind in by_kind:
            by_kind[kind].sort(key=lambda n: n.key)

        return ImpactResult(key=key, max_depth=max_depth, by_kind=dict(by_kind))

    def neighbors(
        self,
        key: str,
        kinds: Iterable[str] | None = None,
        relation: str | None = None,
        direction: str = "out",
    ) -> NeighborResult:
        """Find adjacent entities (one hop) in the specified direction.

        Args:
            key: The entity key.
            kinds: If given, filter destinations by these entity kinds.
            relation: If given, filter relations by this kind.
            direction: "out" (forward), "in" (backward), or "both" (both).

        Returns: Lists of incoming and outgoing edges from this entity.
        """
        entity = self.model.get(key)
        if entity is None:
            matches = _close_matches(key, list(self.model.entities.keys()))
            msg = f"entity {key!r} not found"
            if matches:
                msg += f"; did you mean: {', '.join(matches)}"
            raise QueryError(msg)

        kinds_set = set(kinds) if kinds is not None else None
        incoming: list[dict[str, Any]] = []
        outgoing: list[dict[str, Any]] = []

        for rel in self.model.relations:
            if relation is not None and rel.kind != relation:
                continue

            if direction in ("in", "both") and rel.dst == key:
                src_entity = self.model.get(rel.src)
                if src_entity is not None and (kinds_set is None or src_entity.kind in kinds_set):
                    incoming.append({"src_key": rel.src, "relation_kind": rel.kind})

            if direction in ("out", "both") and rel.src == key:
                dst_entity = self.model.get(rel.dst)
                if dst_entity is not None and (kinds_set is None or dst_entity.kind in kinds_set):
                    outgoing.append({"dst_key": rel.dst, "relation_kind": rel.kind})

        return NeighborResult(key=key, incoming=incoming, outgoing=outgoing)

    def search(
        self,
        text: str,
        kinds: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Full-text search on the backing store.

        Raises QueryError if no store is available.
        """
        if self.store is None:
            raise QueryError("full-text search requires a ModelStore")

        kinds_list = list(kinds) if kinds is not None else None
        hits = self.store.search(text, kinds=kinds_list, limit=limit)

        return [SearchResult(text=hit.text, score=hit.score, citation=hit.citation) for hit in hits]


def _matches(entity: EntityBase, attrs: dict[str, object]) -> bool:
    """Check if entity's typed fields and attrs match the given dict."""
    for key, value in attrs.items():
        # Try typed field first
        if hasattr(entity, key):
            if getattr(entity, key) != value:
                return False
        # Try attrs dict
        elif key in entity.attrs:
            if entity.attrs[key] != value:
                return False
        else:
            # Neither typed field nor attrs: no match
            return False
    return True

"""MCP tools for the Design Model query API (M1-02).

Each tool opens the project's model db at `default_model_db_path(layout)`. If the db
does not exist, raises a tool error "no Design Model yet: run `chipgraph ingest`".
"""

from __future__ import annotations

from typing import Any

from chipgraph.app.context import AppContext
from chipgraph.core.model import (
    ModelQuery,
    ModelStore,
    QueryError,
    default_model_db_path,
)


async def model_block(ctx: AppContext, name: str) -> dict[str, Any]:
    """Retrieve a block and what it contains."""
    db_path = default_model_db_path(ctx.layout.root)
    if not db_path.is_file():
        raise ValueError("no Design Model yet: run `chipgraph ingest`")

    store = ModelStore(db_path)
    model = store.read()
    query = ModelQuery(model, store)

    try:
        result = query.block(name)
        return result.model_dump(mode="json")
    except QueryError as exc:
        raise ValueError(str(exc)) from exc


async def model_module(ctx: AppContext, name: str) -> dict[str, Any]:
    """Retrieve a module, its ports, parameters, instances, and who instantiates it."""
    db_path = default_model_db_path(ctx.layout.root)
    if not db_path.is_file():
        raise ValueError("no Design Model yet: run `chipgraph ingest`")

    store = ModelStore(db_path)
    model = store.read()
    query = ModelQuery(model, store)

    try:
        result = query.module(name)
        return result.model_dump(mode="json")
    except QueryError as exc:
        raise ValueError(str(exc)) from exc


async def model_find(
    ctx: AppContext,
    kind: str | None = None,
    name: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Find entities by kind, name (glob via fnmatch)."""
    db_path = default_model_db_path(ctx.layout.root)
    if not db_path.is_file():
        raise ValueError("no Design Model yet: run `chipgraph ingest`")

    store = ModelStore(db_path)
    model = store.read()
    query = ModelQuery(model, store)

    try:
        results = query.find(kind=kind, name=name, limit=limit)
        return {"results": [r.model_dump(mode="json") for r in results]}
    except QueryError as exc:
        raise ValueError(str(exc)) from exc


async def model_trace(ctx: AppContext, key: str) -> dict[str, Any]:
    """Trace a requirement or entity: show requirements and links.

    For requirement entities: shows implements, verifies, derives_from, and flags.
    For other entities: shows requirements that point to them.
    """
    db_path = default_model_db_path(ctx.layout.root)
    if not db_path.is_file():
        raise ValueError("no Design Model yet: run `chipgraph ingest`")

    store = ModelStore(db_path)
    model = store.read()
    query = ModelQuery(model, store)

    try:
        result = query.trace(key)
        return result.model_dump(mode="json")
    except QueryError as exc:
        raise ValueError(str(exc)) from exc


async def model_impact(ctx: AppContext, key: str, max_depth: int = 5) -> dict[str, Any]:
    """Compute downstream impact from a change at `key`.

    Traverses `contains`, `instantiates`, `connects`, `derives_from`, `implements`,
    `verifies` relations downstream, up to `max_depth` levels.
    """
    db_path = default_model_db_path(ctx.layout.root)
    if not db_path.is_file():
        raise ValueError("no Design Model yet: run `chipgraph ingest`")

    store = ModelStore(db_path)
    model = store.read()
    query = ModelQuery(model, store)

    try:
        result = query.impact(key, max_depth=max_depth)
        return result.model_dump(mode="json")
    except QueryError as exc:
        raise ValueError(str(exc)) from exc


async def model_neighbors(
    ctx: AppContext,
    key: str,
    kinds: list[str] | None = None,
    relation: str | None = None,
    direction: str = "out",
) -> dict[str, Any]:
    """Find adjacent entities (one hop) in the specified direction.

    Args:
        key: The entity key.
        kinds: If given, filter destinations by these entity kinds.
        relation: If given, filter relations by this kind.
        direction: "out" (forward), "in" (backward), or "both" (both).
    """
    db_path = default_model_db_path(ctx.layout.root)
    if not db_path.is_file():
        raise ValueError("no Design Model yet: run `chipgraph ingest`")

    store = ModelStore(db_path)
    model = store.read()
    query = ModelQuery(model, store)

    try:
        result = query.neighbors(key, kinds=kinds, relation=relation, direction=direction)
        return result.model_dump(mode="json")
    except QueryError as exc:
        raise ValueError(str(exc)) from exc


async def model_search(
    ctx: AppContext,
    text: str,
    kinds: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Full-text search over entities and documents."""
    db_path = default_model_db_path(ctx.layout.root)
    if not db_path.is_file():
        raise ValueError("no Design Model yet: run `chipgraph ingest`")

    store = ModelStore(db_path)
    model = store.read()
    query = ModelQuery(model, store)

    try:
        results = query.search(text, kinds=kinds, limit=limit)
        return {"results": [r.model_dump(mode="json") for r in results]}
    except QueryError as exc:
        raise ValueError(str(exc)) from exc

"""Shared helpers for the model-based cross checks (M1-07, DESIGN.md 4.4).

Cross checks read the Design Model that `chipgraph ingest` wrote to the project's
`ModelStore`; they never run extractors themselves. A missing store is a check `error`
telling the user to run `chipgraph ingest` first.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from chipgraph.core.model.model import DesignModel
from chipgraph.core.model.store import ModelStore, default_model_db_path
from chipgraph.core.plugin_api.types import ToolContext


class ModelUnavailable(Exception):
    """Raised when there is no Design Model to check (ingest has not run)."""


@dataclass(frozen=True, slots=True)
class LoadedModel:
    """The stored model, plus the hash of the inputs it was built from."""

    model: DesignModel
    build_inputs_hash: str | None
    db_path: Path


def load_model(repo_root: Path) -> LoadedModel:
    """Read the model `chipgraph ingest` wrote for `repo_root`.

    Raises `ModelUnavailable` with a message naming the command to run when the store
    does not exist yet.
    """
    db_path = default_model_db_path(repo_root)
    if not db_path.is_file():
        raise ModelUnavailable(
            f"no Design Model at {db_path}; run `chipgraph ingest` before the cross checks"
        )
    model = ModelStore(db_path).read()
    return LoadedModel(model=model, build_inputs_hash=_build_inputs_hash(db_path), db_path=db_path)


def block_param(ctx: ToolContext) -> str | None:
    """The block this check run is scoped to (`chipgraph check --block`), if any."""
    value = ctx.params.get("block")
    return value if isinstance(value, str) and value else None


def _build_inputs_hash(db_path: Path) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'build_inputs_hash'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return str(row[0]) if row else None


__all__ = ["LoadedModel", "ModelUnavailable", "block_param", "load_model"]

"""`ModelStore`: the SQLite-backed, rebuildable index for a `DesignModel` (DESIGN.md 4.5).

The index lives at ``.chipgraph/state/cache/model.db`` by default (`default_model_db_path`)
and is never the source of truth: it is always rebuildable from the project's own spec
and RTL files (M1-06 `ingest`). `write()` replaces the whole file atomically so a reader
never observes a half-written database, and `read()` reconstructs an equivalent
`DesignModel` from it.

The store also holds an FTS5 index, both over entities (name plus their text-ish
fields/attrs) and over registered documents (`add_document`), for the `/ask` full-text
search in a later task (M1-13). If the running Python's sqlite3 lacks the FTS5
extension, `ModelStore()` raises `ModelStoreError` immediately, rather than failing
later on first use.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from chipgraph.core.model.entities import EntityBase
from chipgraph.core.model.model import DesignModel
from chipgraph.core.model.registry import DEFAULT_ENTITY_KINDS, EntityKinds
from chipgraph.core.model.relations import Relation
from chipgraph.core.state.layout import StateLayout

_SCHEMA_VERSION = 1


class ModelStoreError(RuntimeError):
    """Raised for store-level problems, e.g. a SQLite build without the FTS5 extension."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A single full-text search result: either a model entity, or a document line."""

    text: str
    score: float
    key: str | None = None
    file: str | None = None
    line: int | None = None

    @property
    def citation(self) -> str:
        """A human-readable citation: the model key, or ``file:line``."""
        if self.key is not None:
            return self.key
        if self.file is not None and self.line is not None:
            return f"{self.file}:{self.line}"
        if self.file is not None:
            return self.file
        raise ValueError("SearchHit has neither a key nor a file to cite")


def default_model_db_path(root: Path) -> Path:
    """The default `ModelStore` path for a project rooted at `root`."""
    return StateLayout(root).cache_dir / "model.db"


class ModelStore:
    """Reads and writes a `DesignModel` to a single SQLite file at `path`."""

    def __init__(self, path: Path, entity_kinds: EntityKinds | None = None) -> None:
        _check_fts5_available()
        self.path = path
        self._entity_kinds = entity_kinds if entity_kinds is not None else DEFAULT_ENTITY_KINDS

    def write(self, model: DesignModel, *, build_inputs_hash: str | None = None) -> None:
        """Atomically replace the store's content with `model`.

        Writes a fresh database to a temp file next to `path`, then `os.replace`s it
        into place. If anything fails before the replace, `path` is left untouched.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            conn = sqlite3.connect(tmp_path)
            try:
                _init_schema(conn)
                _write_entities(conn, model.entities.values())
                _write_relations(conn, model.relations)
                _write_meta(conn, build_inputs_hash=build_inputs_hash)
                conn.commit()
            finally:
                conn.close()
            os.replace(tmp_path, self.path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def read(self) -> DesignModel:
        """Return the `DesignModel` currently stored, or an empty model if none exists."""
        if not self.path.is_file():
            return DesignModel.build([], [])
        conn = sqlite3.connect(self.path)
        try:
            entities = [
                self._entity_kinds.parse(json.loads(raw))
                for (raw,) in conn.execute("SELECT json FROM entities")
            ]
            relations = [
                Relation.model_validate(json.loads(raw))
                for (raw,) in conn.execute("SELECT json FROM relations")
            ]
        finally:
            conn.close()
        return DesignModel.build(entities, relations)

    def add_document(
        self,
        path: str,
        text: str,
        anchors: Sequence[tuple[int, str]] | None = None,
    ) -> None:
        """Register a document's text for full-text search, replacing any prior copy.

        Stores one row per line so hits can cite `file:line`. By default, lines are
        `text.splitlines()` numbered from 1; pass `anchors` (explicit `(line, text)`
        pairs) when the caller already knows the true source line for each chunk (e.g.
        after stripping something from the original file).
        """
        conn = self._connect_ensuring_schema()
        try:
            conn.execute("DELETE FROM documents WHERE path = ?", (path,))
            conn.execute("DELETE FROM documents_fts WHERE path = ?", (path,))
            rows = anchors if anchors is not None else list(enumerate(text.splitlines(), start=1))
            conn.executemany(
                "INSERT INTO documents (path, line, text) VALUES (?, ?, ?)",
                [(path, line, line_text) for line, line_text in rows],
            )
            conn.executemany(
                "INSERT INTO documents_fts (path, line, text) VALUES (?, ?, ?)",
                [(path, line, line_text) for line, line_text in rows],
            )
            conn.commit()
        finally:
            conn.close()

    def search(
        self,
        query: str,
        kinds: Iterable[str] | None = None,
        limit: int = 20,
    ) -> tuple[SearchHit, ...]:
        """Full-text search over entities and (unless `kinds` is given) documents.

        Returns hits ordered by relevance (best first). Each hit cites either a model
        `key` (entity) or `file`/`line` (document).
        """
        if not self.path.is_file():
            return ()
        conn = sqlite3.connect(self.path)
        try:
            hits: list[SearchHit] = []
            kind_set = set(kinds) if kinds is not None else None
            entity_rows = conn.execute(
                "SELECT key, kind, text, bm25(entities_fts) FROM entities_fts "
                "WHERE entities_fts MATCH ? ORDER BY bm25(entities_fts) LIMIT ?",
                (query, limit),
            )
            for key, kind, text, score in entity_rows:
                if kind_set is not None and kind not in kind_set:
                    continue
                hits.append(SearchHit(key=key, text=text, score=score))
            if kind_set is None:
                doc_rows = conn.execute(
                    "SELECT path, line, text, bm25(documents_fts) FROM documents_fts "
                    "WHERE documents_fts MATCH ? ORDER BY bm25(documents_fts) LIMIT ?",
                    (query, limit),
                )
                for doc_path, line, text, score in doc_rows:
                    hits.append(SearchHit(file=doc_path, line=line, text=text, score=score))
        finally:
            conn.close()
        hits.sort(key=lambda hit: hit.score)
        return tuple(hits[:limit])

    def _connect_ensuring_schema(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.path.is_file()
        conn = sqlite3.connect(self.path)
        if is_new:
            _init_schema(conn)
            conn.commit()
        return conn


def _check_fts5_available() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_check USING fts5(x)")
    except sqlite3.OperationalError as exc:
        raise ModelStoreError(
            "this Python's sqlite3 build lacks the FTS5 extension, which ModelStore "
            "requires for full-text search over the Design Model; rebuild Python (or its "
            "sqlite3 dependency) with FTS5 enabled"
        ) from exc
    finally:
        conn.close()


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE entities (
            key  TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            json TEXT NOT NULL
        );
        CREATE TABLE relations (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            src  TEXT NOT NULL,
            dst  TEXT NOT NULL,
            json TEXT NOT NULL
        );
        CREATE TABLE meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE entities_fts USING fts5(key UNINDEXED, kind UNINDEXED, text);
        CREATE TABLE documents (
            path TEXT NOT NULL,
            line INTEGER NOT NULL,
            text TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE documents_fts USING fts5(path UNINDEXED, line UNINDEXED, text);
        """
    )


def _write_entities(conn: sqlite3.Connection, entities: Iterable[EntityBase]) -> None:
    for entity in entities:
        payload = json.dumps(entity.model_dump(mode="json"))
        conn.execute(
            "INSERT INTO entities (key, kind, name, json) VALUES (?, ?, ?, ?)",
            (entity.key, entity.kind, entity.name, payload),
        )
        conn.execute(
            "INSERT INTO entities_fts (key, kind, text) VALUES (?, ?, ?)",
            (entity.key, entity.kind, _entity_search_text(entity)),
        )


def _entity_search_text(entity: EntityBase) -> str:
    parts = [entity.name]
    for field_name in ("text", "rationale"):
        value = getattr(entity, field_name, None)
        if isinstance(value, str):
            parts.append(value)
    for value in entity.attrs.values():
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


def _write_relations(conn: sqlite3.Connection, relations: Iterable[Relation]) -> None:
    for relation in relations:
        payload = json.dumps(relation.model_dump(mode="json"))
        conn.execute(
            "INSERT INTO relations (kind, src, dst, json) VALUES (?, ?, ?, ?)",
            (relation.kind, relation.src, relation.dst, payload),
        )


def _write_meta(conn: sqlite3.Connection, *, build_inputs_hash: str | None) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(_SCHEMA_VERSION),)
    )
    if build_inputs_hash is not None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('build_inputs_hash', ?)", (build_inputs_hash,)
        )

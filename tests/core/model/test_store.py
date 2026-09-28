"""Tests for `ModelStore`: the SQLite-backed, rebuildable Design Model index."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chipgraph.core.model.entities import BlockEntity, ModuleEntity, RequirementEntity
from chipgraph.core.model.keys import make_key
from chipgraph.core.model.model import DesignModel
from chipgraph.core.model.relations import Relation
from chipgraph.core.model.store import (
    ModelStore,
    ModelStoreError,
    _check_fts5_available,
    default_model_db_path,
)


def _relation_sort_key(relation: Relation) -> tuple[str, str, str]:
    return (relation.kind, relation.src, relation.dst)


def _sample_model() -> DesignModel:
    block = BlockEntity(key=make_key("block", "timer"), name="timer", owner="nghia")
    module = ModuleEntity(key=make_key("module", "tiny_timer"), name="tiny_timer", block=block.key)
    requirement = RequirementEntity(
        key=make_key("requirement", "REQ-TIM-004"),
        name="REQ-TIM-004",
        text="the counter shall stop at zero when AUTO_RELOAD is disabled",
    )
    relations = (
        Relation(kind="contains", src=block.key, dst=module.key),
        Relation(kind="derives_from", src=requirement.key, dst=block.key),
    )
    return DesignModel.build([block, module, requirement], relations)


def test_default_model_db_path(tmp_path: Path) -> None:
    expected = tmp_path / ".chipgraph" / "state" / "cache" / "model.db"
    assert default_model_db_path(tmp_path) == expected


def test_read_of_missing_db_returns_empty_model(tmp_path: Path) -> None:
    store = ModelStore(tmp_path / "model.db")
    model = store.read()
    assert model.entities == {}
    assert model.relations == ()


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    store = ModelStore(tmp_path / "model.db")
    model = _sample_model()
    store.write(model)

    reloaded = store.read()

    assert reloaded.entities == model.entities
    # Relation.attrs is a dict, so Relation is unhashable; compare as sorted tuples.
    assert sorted(reloaded.relations, key=_relation_sort_key) == sorted(
        model.relations, key=_relation_sort_key
    )


def test_delete_cache_and_write_again_gives_the_same_result(tmp_path: Path) -> None:
    db_path = tmp_path / "model.db"
    store = ModelStore(db_path)
    model = _sample_model()

    store.write(model)
    first_read = store.read()

    db_path.unlink()
    store.write(model)
    second_read = store.read()

    assert first_read.entities == second_read.entities
    assert sorted(first_read.relations, key=_relation_sort_key) == sorted(
        second_read.relations, key=_relation_sort_key
    )


def test_write_replaces_prior_content_rather_than_appending(tmp_path: Path) -> None:
    store = ModelStore(tmp_path / "model.db")
    store.write(_sample_model())

    smaller_model = DesignModel.build([BlockEntity(key="block:only", name="only")])
    store.write(smaller_model)

    reloaded = store.read()
    assert set(reloaded.entities) == {"block:only"}


def test_write_records_build_inputs_hash_in_meta(tmp_path: Path) -> None:
    db_path = tmp_path / "model.db"
    store = ModelStore(db_path)
    store.write(_sample_model(), build_inputs_hash="deadbeef" * 8)

    conn = sqlite3.connect(db_path)
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()
    assert rows["schema_version"] == "1"
    assert rows["build_inputs_hash"] == "deadbeef" * 8


def test_search_finds_requirement_text_by_key(tmp_path: Path) -> None:
    store = ModelStore(tmp_path / "model.db")
    store.write(_sample_model())

    hits = store.search("AUTO_RELOAD")

    assert any(h.key == "requirement:REQ-TIM-004" for h in hits)
    hit = next(h for h in hits if h.key == "requirement:REQ-TIM-004")
    assert hit.citation == "requirement:REQ-TIM-004"


def test_search_finds_document_line_and_cites_file_line(tmp_path: Path) -> None:
    store = ModelStore(tmp_path / "model.db")
    store.write(_sample_model())
    store.add_document(
        "doc/specs/QNSC_TIMER_MAS.md",
        "# Timer MAS\n"
        "REQ-TIM-004: the counter shall stop at zero when AUTO_RELOAD is disabled.\n"
        "REQ-TIM-009: the counter shall wrap around when AUTO_RELOAD is enabled.\n",
    )

    hits = store.search("disabled", kinds=None)

    doc_hits = [h for h in hits if h.file is not None]
    assert doc_hits, "expected at least one document hit"
    assert doc_hits[0].citation == "doc/specs/QNSC_TIMER_MAS.md:2"
    assert doc_hits[0].line == 2


def test_search_kinds_filter_excludes_documents_and_other_kinds(tmp_path: Path) -> None:
    store = ModelStore(tmp_path / "model.db")
    store.write(_sample_model())
    store.add_document("doc/x.md", "AUTO_RELOAD appears here too\n")

    hits = store.search("AUTO_RELOAD", kinds=["requirement"])

    assert hits
    assert all(h.file is None for h in hits)
    assert all(h.key is not None and h.key.startswith("requirement:") for h in hits)


def test_add_document_replaces_prior_copy_of_the_same_path(tmp_path: Path) -> None:
    store = ModelStore(tmp_path / "model.db")
    store.add_document("doc/x.md", "first version mentions kittens\n")
    store.add_document("doc/x.md", "second version mentions puppies\n")

    assert store.search("kittens") == ()
    hits = store.search("puppies")
    assert any(h.file == "doc/x.md" for h in hits)


def test_search_hit_citation_requires_a_key_or_file() -> None:
    from chipgraph.core.model.store import SearchHit

    hit = SearchHit(text="x", score=0.0)
    with pytest.raises(ValueError, match="neither"):
        _ = hit.citation


def test_atomic_write_failure_leaves_old_db_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "model.db"
    store = ModelStore(db_path)
    store.write(_sample_model())
    original_bytes = db_path.read_bytes()

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated failure mid-write")

    monkeypatch.setattr("chipgraph.core.model.store.os.replace", _boom)

    with pytest.raises(OSError, match="simulated failure"):
        store.write(DesignModel.build([BlockEntity(key="block:other", name="other")]))

    assert db_path.read_bytes() == original_bytes
    # No leftover temp files next to the db.
    assert list(tmp_path.glob(".*tmp*")) == []

    reloaded = store.read()
    assert set(reloaded.entities) == set(_sample_model().entities)


def test_atomic_write_failure_during_db_build_leaves_old_db_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "model.db"
    store = ModelStore(db_path)
    store.write(_sample_model())
    original_bytes = db_path.read_bytes()

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated failure while writing entities")

    monkeypatch.setattr("chipgraph.core.model.store._write_entities", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        store.write(DesignModel.build([BlockEntity(key="block:other", name="other")]))

    assert db_path.read_bytes() == original_bytes
    assert list(tmp_path.glob(".*tmp*")) == []


def test_check_fts5_available_passes_on_this_python() -> None:
    _check_fts5_available()


def test_model_store_raises_clear_error_without_fts5(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = sqlite3.connect

    class _NoFts5Connection:
        def execute(self, sql: str, *args: object) -> None:
            if "USING fts5" in sql:
                raise sqlite3.OperationalError("no such module: fts5")

        def close(self) -> None:
            pass

    def _fake_connect(path: object) -> object:
        if path == ":memory:":
            return _NoFts5Connection()
        return real_connect(path)  # type: ignore[arg-type]

    monkeypatch.setattr("chipgraph.core.model.store.sqlite3.connect", _fake_connect)

    with pytest.raises(ModelStoreError, match="FTS5"):
        ModelStore(Path("unused.db"))

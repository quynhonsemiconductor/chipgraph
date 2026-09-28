"""Tests for `chipgraph.core.engine.records.RecordStore`."""

from __future__ import annotations

from pathlib import Path

from chipgraph.core.engine.graph import ProductionRecord
from chipgraph.core.engine.records import RecordStore
from chipgraph.core.state.layout import StateLayout

SHA = "a" * 64
SHA2 = "b" * 64


def _record(instance_id: str, *, inputs_hash: str = SHA) -> ProductionRecord:
    return ProductionRecord(
        instance_id=instance_id,
        inputs_hash=inputs_hash,
        output_hashes={".:a.txt": SHA},
    )


def test_get_missing_returns_none(tmp_path: Path) -> None:
    store = RecordStore(StateLayout(tmp_path))
    assert store.get("p/a[]") is None


def test_put_then_get_round_trip(tmp_path: Path) -> None:
    store = RecordStore(StateLayout(tmp_path))
    record = _record("p/a[]")
    store.put(record)
    assert store.get("p/a[]") == record


def test_put_overwrites_previous_record(tmp_path: Path) -> None:
    store = RecordStore(StateLayout(tmp_path))
    store.put(_record("p/a[]", inputs_hash=SHA))
    store.put(_record("p/a[]", inputs_hash=SHA2))
    got = store.get("p/a[]")
    assert got is not None
    assert got.inputs_hash == SHA2


def test_delete_removes_record(tmp_path: Path) -> None:
    store = RecordStore(StateLayout(tmp_path))
    store.put(_record("p/a[]"))
    store.delete("p/a[]")
    assert store.get("p/a[]") is None


def test_delete_missing_is_a_no_op(tmp_path: Path) -> None:
    store = RecordStore(StateLayout(tmp_path))
    store.delete("p/never-existed[]")  # must not raise


def test_all_returns_every_stored_record(tmp_path: Path) -> None:
    store = RecordStore(StateLayout(tmp_path))
    store.put(_record("p/a[]"))
    store.put(_record("p/b[]"))
    assert store.all() == {
        "p/a[]": _record("p/a[]"),
        "p/b[]": _record("p/b[]"),
    }


def test_write_is_atomic_no_tmp_file_left_behind(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    store = RecordStore(layout)
    store.put(_record("p/a[]"))
    records_dir = layout.state_dir / "records"
    files = list(records_dir.glob("*"))
    assert len(files) == 1
    assert not files[0].name.endswith(".tmp")


def test_instance_ids_with_special_characters_are_hashed_to_a_safe_filename(
    tmp_path: Path,
) -> None:
    store = RecordStore(StateLayout(tmp_path))
    iid = "digital-rtl/rtl_module[block=timer,module=cnt]"
    store.put(_record(iid))
    assert store.get(iid) is not None

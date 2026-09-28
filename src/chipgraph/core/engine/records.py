"""Persisted `ProductionRecord`s: what the scheduler recorded the last time each rule
instance finished, kept across runs so staleness (`graph.compute_staleness`) can be
computed the next time the engine looks at this project.

Each record lives at ``layout.state_dir / "records" / <sha256(instance_id)>.json``,
written atomically (tmp file + `os.replace`), one file per instance.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from chipgraph.core.engine.graph import ProductionRecord
from chipgraph.core.state.layout import StateLayout


class RecordStore:
    """Reads and writes `ProductionRecord`s, one JSON file per instance id."""

    def __init__(self, layout: StateLayout) -> None:
        self.layout = layout
        self._dir = layout.state_dir / "records"

    def _path(self, instance_id: str) -> Path:
        digest = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.json"

    def get(self, instance_id: str) -> ProductionRecord | None:
        """Return the record for `instance_id`, or None if it was never recorded."""
        path = self._path(instance_id)
        if not path.exists():
            return None
        return ProductionRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def put(self, record: ProductionRecord) -> None:
        """Atomically write `record`, replacing any previous record for the same instance."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(record.instance_id)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    def delete(self, instance_id: str) -> None:
        """Remove the record for `instance_id`, if any. A no-op if it has none."""
        self._path(instance_id).unlink(missing_ok=True)

    def all(self) -> dict[str, ProductionRecord]:
        """Return every stored record, keyed by instance id."""
        if not self._dir.exists():
            return {}
        result: dict[str, ProductionRecord] = {}
        for path in self._dir.glob("*.json"):
            record = ProductionRecord.model_validate_json(path.read_text(encoding="utf-8"))
            result[record.instance_id] = record
        return result


__all__ = ["RecordStore"]

"""Idempotency store: DESIGN.md 6.2, "Mỗi lần gọi tool có khóa idempotency, nên resume
không chạy lại sim hay synth đã xong" (every tool call has an idempotency key, so resume
does not re-run simulation or synthesis that already finished).

Results are stored as `CheckResult` JSON, one file per key, named by the key's SHA-256
hash, under the run's directory.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

from chipgraph.core.contracts.check import CheckResult
from chipgraph.core.state.layout import StateLayout


def make_key(parts: Mapping[str, str]) -> str:
    """A stable idempotency key: SHA-256 over `parts`, sorted by key, as ``k=v`` lines.

    Callers pass whatever makes a tool call reproducible: the command, cwd, input hashes,
    tool version, and so on.
    """
    lines = "\n".join(f"{k}={parts[k]}" for k in sorted(parts))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """Stores and retrieves `CheckResult`s by idempotency key, scoped to one run."""

    def __init__(self, layout: StateLayout, run_id: str) -> None:
        self.layout = layout
        self.run_id = run_id
        self._dir = layout.run_dir(run_id) / "results"

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.json"

    def has(self, key: str) -> bool:
        return self._path(key).exists()

    def get(self, key: str) -> CheckResult | None:
        path = self._path(key)
        if not path.exists():
            return None
        return CheckResult.model_validate_json(path.read_text(encoding="utf-8"))

    def put(self, result: CheckResult) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(result.idempotency_key)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp_path, path)


__all__ = ["IdempotencyStore", "make_key"]

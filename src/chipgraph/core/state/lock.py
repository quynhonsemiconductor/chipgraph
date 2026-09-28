"""Per-block locks: DESIGN.md 6.2, "một block chỉ có một run ghi tại một thời điểm".

A lock is a small JSON file created atomically (`O_CREAT | O_EXCL`) under
`StateLayout.locks_dir`. It records the owner, the process id and host that took it, and
when. A lock whose owning process is no longer alive on the same host — or that has been
held past `stale_after_s` — is considered abandoned and may be taken over.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from chipgraph.core.state.layout import StateLayout

_BLOCK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DEFAULT_STALE_AFTER_S = 12 * 60 * 60


class LockHeldError(Exception):
    """Another live owner already holds this block's lock."""

    def __init__(self, block: str, owner: str) -> None:
        super().__init__(f"block {block!r} is locked by {owner!r}")
        self.block = block
        self.owner = owner


@dataclass(frozen=True, slots=True)
class _LockRecord:
    owner: str
    pid: int
    host: str
    since: str


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it: treat as alive.
        return True
    except OSError:
        return False
    return True


def _read_record(path: Path) -> _LockRecord:
    data = json.loads(path.read_text(encoding="utf-8"))
    return _LockRecord(owner=data["owner"], pid=data["pid"], host=data["host"], since=data["since"])


def _write_record_atomic(path: Path, record: _LockRecord) -> None:
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "owner": record.owner,
                    "pid": record.pid,
                    "host": record.host,
                    "since": record.since,
                },
                f,
            )
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise


class BlockLock:
    """A context manager that holds an exclusive lock on `block` for the duration of a `with`.

    Usage::

        with BlockLock(layout, block="timer", owner="run-123") as lock:
            ...  # only one live owner may be inside this block at a time
            lock.took_over_stale  # True if a dead/stale lock was reclaimed
    """

    def __init__(
        self,
        layout: StateLayout,
        block: str,
        owner: str,
        *,
        stale_after_s: float = _DEFAULT_STALE_AFTER_S,
    ) -> None:
        if not _BLOCK_NAME_RE.match(block):
            raise ValueError(f"invalid block name: {block!r}")
        self.layout = layout
        self.block = block
        self.owner = owner
        self.stale_after_s = stale_after_s
        self.took_over_stale = False
        self._path = layout.locks_dir / f"{block}.lock"
        self._acquired = False

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        self.layout.locks_dir.mkdir(parents=True, exist_ok=True)
        record = _LockRecord(
            owner=self.owner,
            pid=os.getpid(),
            host=socket.gethostname(),
            since=datetime.now(UTC).isoformat(),
        )
        try:
            _write_record_atomic(self._path, record)
        except FileExistsError:
            self._resolve_conflict(record)
            return
        self._acquired = True
        self.took_over_stale = False

    def _resolve_conflict(self, new_record: _LockRecord) -> None:
        try:
            existing = _read_record(self._path)
        except (OSError, json.JSONDecodeError, KeyError):
            # Unreadable/corrupt lock file: treat as stale and take it over.
            self._take_over(new_record)
            return

        same_host = existing.host == new_record.host
        alive = same_host and _pid_alive(existing.pid)
        if alive:
            stale = self._is_stale(existing)
            if not stale:
                raise LockHeldError(self.block, existing.owner)
        self._take_over(new_record)

    def _is_stale(self, existing: _LockRecord) -> bool:
        try:
            since = datetime.fromisoformat(existing.since)
        except ValueError:
            return True
        age_s = (datetime.now(UTC) - since).total_seconds()
        return age_s > self.stale_after_s

    def _take_over(self, new_record: _LockRecord) -> None:
        tmp_path = self._path.with_suffix(".lock.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "owner": new_record.owner,
                    "pid": new_record.pid,
                    "host": new_record.host,
                    "since": new_record.since,
                },
                f,
            )
        os.replace(tmp_path, self._path)
        self._acquired = True
        self.took_over_stale = True

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            current = _read_record(self._path)
        except (OSError, json.JSONDecodeError, KeyError):
            return
        if current.owner == self.owner and current.pid == os.getpid():
            with contextlib.suppress(FileNotFoundError):
                self._path.unlink()
        self._acquired = False

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


__all__ = ["BlockLock", "LockHeldError"]

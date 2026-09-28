"""Tests for `chipgraph.core.state.lock`."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from chipgraph.core.state.layout import StateLayout
from chipgraph.core.state.lock import BlockLock, LockHeldError


def test_acquire_and_release(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    lock = BlockLock(layout, block="timer", owner="run-1")
    with lock:
        assert lock.path.exists()
        assert lock.took_over_stale is False
    assert not lock.path.exists()


def test_second_acquirer_gets_lock_held_error(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    with BlockLock(layout, block="timer", owner="run-1"):
        with pytest.raises(LockHeldError) as exc_info:
            BlockLock(layout, block="timer", owner="run-2").acquire()
        assert "run-1" in str(exc_info.value)


def test_stale_lock_with_dead_pid_is_taken_over(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = proc.pid
    proc.wait()  # process has now exited, but the pid is still known

    layout.locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = layout.locks_dir / "timer.lock"
    lock_path.write_text(
        json.dumps(
            {
                "owner": "stale-owner",
                "pid": dead_pid,
                "host": __import__("socket").gethostname(),
                "since": "2000-01-01T00:00:00+00:00",
            }
        )
    )

    lock = BlockLock(layout, block="timer", owner="run-2")
    lock.acquire()
    try:
        assert lock.took_over_stale is True
        data = json.loads(lock_path.read_text())
        assert data["owner"] == "run-2"
    finally:
        lock.release()


def test_stale_after_timeout_is_taken_over_even_if_alive(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    layout.locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = layout.locks_dir / "timer.lock"
    lock_path.write_text(
        json.dumps(
            {
                "owner": "stale-owner",
                "pid": __import__("os").getpid(),  # our own pid: definitely alive
                "host": __import__("socket").gethostname(),
                "since": "2000-01-01T00:00:00+00:00",
            }
        )
    )

    lock = BlockLock(layout, block="timer", owner="run-2", stale_after_s=1)
    lock.acquire()
    try:
        assert lock.took_over_stale is True
    finally:
        lock.release()


def test_release_does_not_delete_someone_elses_lock(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    first = BlockLock(layout, block="timer", owner="run-1")
    first.acquire()

    impostor = BlockLock(layout, block="timer", owner="run-2")
    # Simulate holding a stale reference without having actually acquired it.
    impostor._acquired = True

    impostor.release()
    assert first.path.exists()
    first.release()


def test_bad_block_name_rejected(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    with pytest.raises(ValueError, match="block"):
        BlockLock(layout, block="Bad Name!", owner="run-1")

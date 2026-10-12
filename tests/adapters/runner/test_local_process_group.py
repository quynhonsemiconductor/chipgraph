"""Security review of M2-12 (finding 15): a timeout or a cancellation kills the child's whole
process group, so a grandchild that keeps the output pipes open cannot block the run."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from chipgraph.adapters.runner.local import LocalRunner

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")

#: A child that starts a grandchild holding stdout and stderr open, records its pid, and
#: then sleeps itself.
_SPAWNER = """
import subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(grandchild.pid))
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie still answers kill(0): ask ps whether it is one
    state = os.popen(f"ps -o stat= -p {pid}").read().strip()
    return bool(state) and not state.startswith("Z")


def _wait_gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _pid(path: Path) -> int:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError("the child never wrote its grandchild's pid")


def test_a_timeout_kills_the_grandchild_that_holds_the_pipes(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    start = time.monotonic()
    result = asyncio.run(
        LocalRunner().run(
            [sys.executable, "-c", _SPAWNER, str(pid_file)], cwd=tmp_path, timeout_s=1.0
        )
    )
    assert result.timed_out is True
    assert time.monotonic() - start < 15, "communicate() blocked on the grandchild's pipes"
    assert _wait_gone(_pid(pid_file))


def test_cancelling_the_run_kills_the_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"

    async def main() -> None:
        task = asyncio.create_task(
            LocalRunner().run([sys.executable, "-c", _SPAWNER, str(pid_file)], cwd=tmp_path)
        )
        while not (pid_file.is_file() and pid_file.read_text()):
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(main(), timeout=30))
    assert _wait_gone(_pid(pid_file))


def test_a_normal_run_is_unchanged(tmp_path: Path) -> None:
    result = asyncio.run(LocalRunner().run([sys.executable, "-c", "print(1)"], cwd=tmp_path))
    assert (result.returncode, result.stdout.strip(), result.timed_out) == (0, "1", False)

"""`LocalRunner`: runs commands as a direct subprocess on the local machine."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from chipgraph.core.plugin_api.types import RunResult

_MISSING_EXECUTABLE_RETURNCODE = 127
_POSIX = os.name == "posix"
_KILL_GRACE_S = 5.0
"""How long, after a timeout's kill, the output pipes may stay open."""


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill `proc` and, on POSIX, every process of its process group (its children)."""
    if _POSIX:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass  # the group is gone: kill the child itself, if it is still there
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


class LocalRunner:
    """Runs `cmd` as an argument list via `asyncio.create_subprocess_exec`.

    Never invokes a shell: `cmd` is always an argument vector. `env` is merged on top
    of the current process environment (`os.environ`). On POSIX the child starts in a
    session (and process group) of its own, so a timeout, or the caller being cancelled,
    kills the child and everything it started: a grandchild that keeps the output pipes
    open cannot block the run after the kill. A timeout returns a `RunResult` with
    `timed_out=True`, keeping whatever output was captured before the kill (if the pipes
    are still open `_KILL_GRACE_S` after it, the output is dropped). A missing executable
    is reported as `returncode=127` rather than raising.
    """

    name = "local"

    async def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> RunResult:
        full_env = dict(os.environ)
        if env is not None:
            full_env.update(env)

        start = time.monotonic()
        extra: dict[str, Any] = {"start_new_session": True} if _POSIX else {}
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                env=full_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **extra,
            )
        except FileNotFoundError as exc:
            duration_s = time.monotonic() - start
            return RunResult(
                returncode=_MISSING_EXECUTABLE_RETURNCODE,
                stdout="",
                stderr=f"executable not found: {exc}",
                duration_s=duration_s,
            )
        except OSError as exc:
            duration_s = time.monotonic() - start
            return RunResult(
                returncode=_MISSING_EXECUTABLE_RETURNCODE,
                stdout="",
                stderr=f"failed to start command: {exc}",
                duration_s=duration_s,
            )

        timed_out = False
        try:
            if timeout_s is None:
                stdout_bytes, stderr_bytes = await proc.communicate()
            else:
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout_s
                    )
                except TimeoutError:
                    timed_out = True
                    _kill_tree(proc)
                    try:
                        stdout_bytes, stderr_bytes = await asyncio.wait_for(
                            proc.communicate(), timeout=_KILL_GRACE_S
                        )
                    except TimeoutError:
                        await proc.wait()
                        stdout_bytes = b""
                        stderr_bytes = b"output dropped: its pipes stayed open after the kill"
        except BaseException:
            _kill_tree(proc)  # cancelled (e.g. Ctrl-C): leave nothing running
            raise
        finally:
            duration_s = time.monotonic() - start

        returncode = proc.returncode if proc.returncode is not None else -9
        return RunResult(
            returncode=returncode,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_s=duration_s,
            timed_out=timed_out,
        )

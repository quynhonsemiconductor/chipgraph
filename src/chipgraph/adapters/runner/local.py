"""`LocalRunner`: runs commands as a direct subprocess on the local machine."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from chipgraph.core.plugin_api.types import RunResult

_MISSING_EXECUTABLE_RETURNCODE = 127


class LocalRunner:
    """Runs `cmd` as an argument list via `asyncio.create_subprocess_exec`.

    Never invokes a shell: `cmd` is always an argument vector. `env` is merged on top
    of the current process environment (`os.environ`). A timeout kills the child
    process (and reaps it) and returns a `RunResult` with `timed_out=True`, keeping
    whatever output was captured before the kill. A missing executable is reported as
    `returncode=127` rather than raising.
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
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                env=full_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
                    proc.kill()
                    stdout_bytes, stderr_bytes = await proc.communicate()
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

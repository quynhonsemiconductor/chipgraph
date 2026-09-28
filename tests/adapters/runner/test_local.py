"""Tests for `LocalRunner`. Async calls run via `asyncio.run`, no pytest-asyncio needed."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.core.plugin_api.protocols import Runner
from chipgraph.core.plugin_api.types import RunResult


def _run(
    runner: LocalRunner,
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_s: float | None = None,
) -> RunResult:
    return asyncio.run(runner.run(cmd, cwd=cwd, env=env, timeout_s=timeout_s))


def test_is_a_runner() -> None:
    runner = LocalRunner()
    assert isinstance(runner, Runner)
    assert runner.name == "local"


def test_captures_stdout_and_returncode(tmp_path: Path) -> None:
    result = _run(LocalRunner(), [sys.executable, "-c", "print('hello')"], cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr == ""
    assert result.duration_s >= 0
    assert result.timed_out is False


def test_captures_stderr_and_nonzero_exit(tmp_path: Path) -> None:
    result = _run(
        LocalRunner(),
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        cwd=tmp_path,
    )
    assert result.returncode == 3
    assert result.stderr.strip() == "boom"


def test_env_is_merged_over_os_environ(tmp_path: Path) -> None:
    result = _run(
        LocalRunner(),
        [sys.executable, "-c", "import os; print(os.environ.get('CHIPGRAPH_TEST_VAR', ''))"],
        cwd=tmp_path,
        env={"CHIPGRAPH_TEST_VAR": "sentinel"},
    )
    assert result.stdout.strip() == "sentinel"


def test_env_inherits_os_environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHIPGRAPH_INHERITED_VAR", "from-parent")
    result = _run(
        LocalRunner(),
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('CHIPGRAPH_INHERITED_VAR', ''))",
        ],
        cwd=tmp_path,
        env={"OTHER_VAR": "1"},
    )
    assert result.stdout.strip() == "from-parent"


def test_runs_in_given_cwd(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("here")
    result = _run(
        LocalRunner(),
        [sys.executable, "-c", "import pathlib; print(sorted(pathlib.Path().glob('*')))"],
        cwd=tmp_path,
    )
    assert "marker.txt" in result.stdout


def test_timeout_kills_process(tmp_path: Path) -> None:
    result = _run(
        LocalRunner(),
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout_s=0.2,
    )
    assert result.timed_out is True
    assert result.returncode != 0


def test_missing_executable_returns_127(tmp_path: Path) -> None:
    result = _run(
        LocalRunner(),
        ["chipgraph-definitely-not-a-real-executable-xyz"],
        cwd=tmp_path,
    )
    assert result.returncode == 127
    assert result.stderr != ""

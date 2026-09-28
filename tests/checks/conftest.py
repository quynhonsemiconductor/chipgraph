"""Shared fixtures for built-in check tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from chipgraph.core.plugin_api.types import RunResult, ToolContext


class FakeRunner:
    """A `Runner` that never actually runs anything; the built-in checks never call it."""

    name = "fake-runner"

    async def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> RunResult:
        raise AssertionError("built-in checks must not run subprocess commands")


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


def make_ctx(
    repo_root: Path, runner: FakeRunner, *, env: dict[str, str] | None = None
) -> ToolContext:
    return ToolContext(repo_root=repo_root, runner=runner, env=env or {})


def write(root: Path, rel: str, content: str = "") -> Path:
    """Write `content` to `root/rel`, creating parent directories as needed."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path

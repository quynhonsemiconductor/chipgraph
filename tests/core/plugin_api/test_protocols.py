"""Tests that fake implementations satisfy (or fail) the plugin_api protocols."""

import asyncio
from pathlib import Path

from fakes import (
    FakeAgentRuntime,
    FakeCheck,
    FakeExtractor,
    FakeFormatAdapter,
    FakeGenerator,
    FakeLlmProvider,
    FakeLogParser,
    FakeReviewAdapter,
    FakeRunner,
    FakeToolAdapter,
    FakeVcsAdapter,
    NotAProtocol,
)

from chipgraph.core.plugin_api.protocols import (
    AgentRuntime,
    Check,
    Extractor,
    FormatAdapter,
    Generator,
    LlmProvider,
    LogParser,
    ReviewAdapter,
    Runner,
    ToolAdapter,
    VcsAdapter,
)


def test_fakes_satisfy_their_protocols() -> None:
    assert isinstance(FakeRunner(), Runner)
    assert isinstance(FakeLogParser(), LogParser)
    assert isinstance(FakeToolAdapter(), ToolAdapter)
    assert isinstance(FakeCheck(), Check)
    assert isinstance(FakeVcsAdapter(), VcsAdapter)
    assert isinstance(FakeReviewAdapter(), ReviewAdapter)
    assert isinstance(FakeLlmProvider(), LlmProvider)
    assert isinstance(FakeAgentRuntime(), AgentRuntime)
    assert isinstance(FakeFormatAdapter(), FormatAdapter)
    assert isinstance(FakeExtractor(), Extractor)
    assert isinstance(FakeGenerator(), Generator)


def test_object_missing_methods_fails_every_protocol() -> None:
    obj = NotAProtocol()
    assert not isinstance(obj, Runner)
    assert not isinstance(obj, LogParser)
    assert not isinstance(obj, ToolAdapter)
    assert not isinstance(obj, Check)
    assert not isinstance(obj, VcsAdapter)
    assert not isinstance(obj, ReviewAdapter)
    assert not isinstance(obj, LlmProvider)
    assert not isinstance(obj, AgentRuntime)
    assert not isinstance(obj, FormatAdapter)
    assert not isinstance(obj, Extractor)
    assert not isinstance(obj, Generator)


def test_runner_run_is_async_and_works() -> None:
    runner = FakeRunner()

    async def _go() -> None:
        result = await runner.run(["echo", "hi"], cwd=Path("."))
        assert result.returncode == 0
        assert result.stdout == "ok"

    asyncio.run(_go())

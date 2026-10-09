"""Shared helpers for the LLM adapter tests (no network, no API key)."""

from __future__ import annotations

from chipgraph.core.contracts import DataLabel
from chipgraph.core.plugin_api.types import LlmMessage, LlmRequest


def make_request(
    *texts: str,
    model: str = "claude-sonnet-5-5",
    system: str | None = None,
    labels: tuple[DataLabel, ...] = (),
    max_tokens: int = 64,
) -> LlmRequest:
    """A request with an optional system message and one user message per text."""
    messages = [LlmMessage(role="system", content=system)] if system is not None else []
    messages += [LlmMessage(role="user", content=t) for t in texts or ("hello there",)]
    return LlmRequest(model=model, messages=tuple(messages), max_tokens=max_tokens, labels=labels)


class SleepRecorder:
    """An injectable async sleep that records the delays instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)

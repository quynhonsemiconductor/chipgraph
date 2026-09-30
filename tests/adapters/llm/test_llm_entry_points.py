"""The three LLM providers are found through their entry points."""

from __future__ import annotations

from chipgraph.adapters.llm import AnthropicProvider, FakeProvider
from chipgraph.core.plugin_api import Registry


def test_llm_providers_are_discovered() -> None:
    registry = Registry()
    registry.discover()
    assert {"fake", "anthropic", "anthropic-compatible"} <= set(registry.names("llm"))

    fake = registry.get("llm", "fake")
    assert isinstance(fake, FakeProvider)

    anthropic = registry.get("llm", "anthropic")
    assert isinstance(anthropic, AnthropicProvider)
    assert (anthropic.name, anthropic.compatible, anthropic.local) == ("anthropic", False, False)

    compatible = registry.get("llm", "anthropic-compatible")
    assert isinstance(compatible, AnthropicProvider)
    assert (compatible.name, compatible.compatible) == ("anthropic-compatible", True)

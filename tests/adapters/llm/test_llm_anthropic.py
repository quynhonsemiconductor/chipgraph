"""`AnthropicProvider` against the real SDK with an `httpx2.MockTransport`: no network, no key."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from llm_helpers import SleepRecorder, make_request

from chipgraph.adapters.llm import (
    AnthropicProvider,
    GuardedProvider,
    LlmError,
    NdaBlocked,
    ProviderConfigError,
    ProviderError,
    RetryPolicy,
    anthropic_compatible,
)
from chipgraph.core.config.models import ModelsCfg
from chipgraph.core.plugin_api.protocols import LlmProvider

KEY_ENV = {"ANTHROPIC_API_KEY": "test-key-anthropic", "GLM_KEY": "test-key-glm"}

Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture(autouse=True)
def _clean_sdk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def _message(
    text: str = "hi back", *, model: str = "claude-sonnet-5-5-20260401", **usage: int
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 3, **usage},
    }


class _Server:
    """A scripted Messages API: records every request, answers from `responses` in order."""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses) or [httpx2.Response(200, json=_message())]
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self))

    def body(self, index: int = 0) -> dict[str, Any]:
        return dict(json.loads(self.requests[index].content))


def _provider(server: _Server, config: dict[str, str] | None = None) -> AnthropicProvider:
    return AnthropicProvider(config, env=KEY_ENV, http_client=server.client())


def test_is_an_llm_provider_and_builds_without_sdk_or_key() -> None:
    provider = AnthropicProvider(env={})
    assert isinstance(provider, LlmProvider)
    assert (provider.name, provider.local) == ("anthropic", False)
    compatible = anthropic_compatible()
    assert (compatible.name, compatible.compatible) == ("anthropic-compatible", True)


def test_maps_the_request_and_the_response() -> None:
    server = _Server(httpx2.Response(200, json=_message("hi back")))
    request = make_request("first", "second", system="you are terse", max_tokens=99)
    response = asyncio.run(_provider(server).complete(request))

    sent = server.requests[0]
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"
    assert sent.headers["x-api-key"] == "test-key-anthropic"
    assert server.body() == {
        "model": "claude-sonnet-5-5",
        "max_tokens": 99,
        "system": "you are terse",
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ],
    }
    assert response.text == "hi back"
    assert (response.input_tokens, response.output_tokens) == (12, 3)
    assert response.model == "claude-sonnet-5-5-20260401"


def test_no_system_field_without_a_system_message() -> None:
    server = _Server()
    asyncio.run(_provider(server).complete(make_request("q")))
    assert "system" not in server.body()


def test_cache_tokens_count_as_input() -> None:
    server = _Server(
        httpx2.Response(
            200, json=_message(cache_creation_input_tokens=100, cache_read_input_tokens=50)
        )
    )
    response = asyncio.run(_provider(server).complete(make_request()))
    assert response.input_tokens == 162


def test_a_request_with_only_system_messages_is_refused() -> None:
    request = make_request(system="only")
    request = request.model_copy(update={"messages": request.messages[:1]})
    server = _Server()
    with pytest.raises(LlmError, match="user or assistant"):
        asyncio.run(_provider(server).complete(request))
    assert server.requests == []


def test_anthropic_compatible_uses_base_url_key_env_and_bearer() -> None:
    server = _Server(httpx2.Response(200, json=_message(model="glm-4.6")))
    models = ModelsCfg(
        providers={
            "glm": {
                "base_url": "https://glm.example.test/api/anthropic",
                "api_key_env": "GLM_KEY",
                "auth": "bearer",
            }
        }
    )
    provider = AnthropicProvider.from_models(
        models, "glm", env=KEY_ENV, http_client=server.client()
    )
    assert provider.compatible is True
    response = asyncio.run(provider.complete(make_request(model="glm-4.6")))
    sent = server.requests[0]
    assert str(sent.url) == "https://glm.example.test/api/anthropic/v1/messages"
    assert sent.headers["authorization"] == "Bearer test-key-glm"
    assert "x-api-key" not in sent.headers
    assert response.model == "glm-4.6"


def test_anthropic_compatible_needs_base_url_and_key_env() -> None:
    no_url = anthropic_compatible({"api_key_env": "GLM_KEY"}, env=KEY_ENV)
    with pytest.raises(ProviderConfigError, match=r"needs models\.providers\..*\.base_url"):
        asyncio.run(no_url.complete(make_request()))
    no_key_env = anthropic_compatible({"base_url": "https://glm.example.test"}, env=KEY_ENV)
    with pytest.raises(ProviderConfigError, match="api_key_env"):
        asyncio.run(no_key_env.complete(make_request()))


def test_missing_key_env_names_the_variable() -> None:
    provider = AnthropicProvider({"api_key_env": "MY_TEAM_KEY"}, env={})
    with pytest.raises(ProviderConfigError, match="environment variable MY_TEAM_KEY is not set"):
        asyncio.run(provider.complete(make_request()))
    default = AnthropicProvider(env={"ANTHROPIC_API_KEY": ""})
    with pytest.raises(ProviderConfigError, match="ANTHROPIC_API_KEY is not set"):
        asyncio.run(default.complete(make_request()))


def test_a_key_in_api_key_env_is_rejected_without_echoing_it() -> None:
    with pytest.raises(ProviderConfigError, match="not the key itself") as info:
        AnthropicProvider({"api_key_env": "sk-ant-secret-value"})
    assert "sk-ant-secret-value" not in str(info.value)


def test_bad_options_are_rejected() -> None:
    with pytest.raises(ProviderConfigError, match="unknown option"):
        AnthropicProvider({"base-url": "x"})
    with pytest.raises(ProviderConfigError, match="local must be"):
        AnthropicProvider({"local": "yes"})
    with pytest.raises(ProviderConfigError, match="auth must be"):
        AnthropicProvider({"auth": "basic"})
    with pytest.raises(ProviderConfigError, match="timeout_s"):
        AnthropicProvider({"timeout_s": "soon"})


def test_missing_extra_gives_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)  # makes `import anthropic` fail
    provider = AnthropicProvider(env=KEY_ENV)  # building needs no SDK
    with pytest.raises(ProviderConfigError, match=r"chipgraph\[llm\]"):
        asyncio.run(provider.complete(make_request()))


def test_local_and_nda() -> None:
    nda = make_request(labels=("nda",))
    cloud = _Server()
    with pytest.raises(NdaBlocked):
        asyncio.run(_provider(cloud).complete(nda))
    assert cloud.requests == []
    local_server = _Server()
    local = _provider(local_server, {"base_url": "http://gpu01.test:8000", "local": "true"})
    assert local.local is True
    assert asyncio.run(local.complete(nda)).text == "hi back"


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (529, True), (400, False), (401, False), (404, False)],
)
def test_http_errors_map_to_provider_errors(status: int, retryable: bool) -> None:
    body = {"type": "error", "error": {"type": "some_error", "message": "boom"}}
    server = _Server(httpx2.Response(status, json=body, headers={"retry-after": "2"}))
    with pytest.raises(ProviderError) as info:
        asyncio.run(_provider(server).complete(make_request()))
    assert info.value.status == status
    assert info.value.retryable is retryable
    assert info.value.retry_after_s == 2.0
    assert len(server.requests) == 1  # the SDK's own retries are off


@pytest.mark.parametrize(
    "error", [httpx2.ConnectError("refused"), httpx2.ReadTimeout("slow")], ids=["conn", "timeout"]
)
def test_connection_errors_and_timeouts_are_retryable(error: Exception) -> None:
    server = _Server(error)
    with pytest.raises(ProviderError) as info:
        asyncio.run(_provider(server).complete(make_request()))
    assert info.value.retryable is True
    assert info.value.status is None


def test_guarded_anthropic_retries_overload_then_succeeds() -> None:
    overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    server = _Server(
        httpx2.Response(529, json=overloaded),
        httpx2.Response(200, json=_message("finally")),
    )
    sleep = SleepRecorder()
    guard = GuardedProvider(
        _provider(server), retry=RetryPolicy(max_tries=3), sleep=sleep, rand=lambda: 0.0
    )
    response = asyncio.run(guard.complete(make_request()))
    assert response.text == "finally"
    assert len(server.requests) == 2
    assert sleep.delays == [0.5]
    assert guard.usage.input_tokens == 12
    assert guard.usage.cost_usd > 0  # claude-sonnet-5-5-20260401 has a default price


def test_guarded_anthropic_does_not_retry_a_bad_request() -> None:
    bad = {"type": "error", "error": {"type": "invalid_request_error", "message": "no"}}
    server = _Server(httpx2.Response(400, json=bad))
    sleep = SleepRecorder()
    guard = GuardedProvider(_provider(server), sleep=sleep, rand=lambda: 0.0)
    with pytest.raises(ProviderError):
        asyncio.run(guard.complete(make_request()))
    assert len(server.requests) == 1
    assert sleep.delays == []

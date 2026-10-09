"""`FakeProvider`: scripted replies, word-count tokens, scripted failures, `local`."""

from __future__ import annotations

import asyncio

import pytest
from llm_helpers import make_request

from chipgraph.adapters.llm import FakeProvider, LlmError, NdaBlocked, ProviderError
from chipgraph.core.plugin_api.protocols import LlmProvider
from chipgraph.core.plugin_api.types import LlmRequest


def test_is_an_llm_provider() -> None:
    fake = FakeProvider()
    assert isinstance(fake, LlmProvider)
    assert fake.name == "fake"
    assert fake.local is False


def test_echoes_the_last_message_by_default() -> None:
    response = asyncio.run(FakeProvider().complete(make_request("one two", "three four five")))
    assert response.text == "three four five"
    assert response.model == "claude-sonnet-5-5"


def test_queue_of_replies_in_order_then_runs_out() -> None:
    fake = FakeProvider(["first reply", "second"])
    assert asyncio.run(fake.complete(make_request())).text == "first reply"
    assert asyncio.run(fake.complete(make_request())).text == "second"
    with pytest.raises(LlmError, match="ran out"):
        asyncio.run(fake.complete(make_request()))


def test_reply_as_a_function_of_the_request() -> None:
    def reply(request: LlmRequest) -> str:
        return f"model={request.model} n={len(request.messages)}"

    fake = FakeProvider(reply)
    response = asyncio.run(fake.complete(make_request("a", "b", model="m1")))
    assert response.text == "model=m1 n=2"


def test_tokens_are_whitespace_words_and_deterministic() -> None:
    fake = FakeProvider(lambda _: "four words of output")
    request = make_request("one two three", system="be brief")
    first = asyncio.run(fake.complete(request))
    second = asyncio.run(fake.complete(request))
    assert (first.input_tokens, first.output_tokens) == (5, 4)
    assert first == second


def test_reports_a_configured_model_and_records_requests() -> None:
    fake = FakeProvider(model="served-model")
    request = make_request()
    assert asyncio.run(fake.complete(request)).model == "served-model"
    assert fake.requests == [request]


@pytest.mark.parametrize(("retryable", "status"), [(True, 503), (False, 400)])
def test_fails_n_times_then_replies(retryable: bool, status: int) -> None:
    fake = FakeProvider(["ok"], fail=2, retryable=retryable)
    for _ in range(2):
        with pytest.raises(ProviderError) as info:
            asyncio.run(fake.complete(make_request()))
        assert info.value.retryable is retryable
        assert info.value.status == status
    assert asyncio.run(fake.complete(make_request())).text == "ok"
    assert len(fake.requests) == 3


def test_refuses_nda_unless_local() -> None:
    nda = make_request(labels=("nda",))
    with pytest.raises(NdaBlocked):
        asyncio.run(FakeProvider().complete(nda))
    assert asyncio.run(FakeProvider(local=True).complete(nda)).text == "hello there"

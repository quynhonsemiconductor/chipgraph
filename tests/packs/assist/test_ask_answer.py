"""M1-13: the API-runtime answerer with a `FakeProvider`: good, invented, unknown, retry."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from ask_helpers import ingested_tinysoc, project

from chipgraph.adapters.llm import AnthropicProvider, FakeProvider
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.core.config.models import ModelsCfg, Profile
from chipgraph.packs.assist.ask import (
    AskResult,
    answer_question,
    make_provider,
    parse_answer,
    run_ask,
)

QUESTION = "What is the reset value of the timer COMPARE register?"


@pytest.fixture(scope="module")
def tinysoc(tmp_path_factory: pytest.TempPathFactory) -> AppContext:
    return ingested_tinysoc(tmp_path_factory.mktemp("ask") / "tinysoc")


def _reply(answer: str, citations: list[str], unknown: bool = False) -> str:
    return json.dumps({"answer": answer, "citations": citations, "unknown": unknown})


def _ask(ctx: AppContext, provider: FakeProvider, question: str = QUESTION) -> AskResult:
    return asyncio.run(answer_question(project(ctx), question, provider, model="fake-small"))


def test_a_good_answer_is_verified(tinysoc: AppContext) -> None:
    provider = FakeProvider(
        [_reply("COMPARE resets to 0.", ["model:register:timer.COMPARE", "rtl/tiny_timer.sv:29"])]
    )
    result = _ask(tinysoc, provider)
    assert result.status == "answered" and result.attempts == 1
    assert result.answer is not None and result.answer.answer == "COMPARE resets to 0."
    assert result.answer.citations == ("model:register:timer.COMPARE", "rtl/tiny_timer.sv:29")
    assert result.check is not None and result.check.ok
    # The model saw the question and the sources, with their citations, and nothing nda.
    [request] = provider.requests
    prompt = request.messages[-1].content
    assert QUESTION in prompt and "model:register:timer.COMPARE" in prompt
    assert request.messages[0].role == "system" and "ONLY" in request.messages[0].content
    assert "nda" not in request.labels and request.labels


def test_an_invented_citation_is_sent_back_then_rejected(tinysoc: AppContext) -> None:
    invented = _reply("COMPARE resets to 0x5.", ["doc/specs/TINY_TIMER_MAS.md:999"])
    provider = FakeProvider([invented, invented])
    result = _ask(tinysoc, provider)
    assert result.status == "rejected" and result.attempts == 2
    # The invented answer is never returned: the result is "I don't know".
    assert result.answer is not None and result.answer.unknown
    assert "0x5" not in result.answer.answer and result.answer.citations == ()
    assert result.check is not None and not result.check.ok
    assert any("line 999 does not exist" in r for r in result.check.reasons)
    retry = provider.requests[1].messages
    assert [m.role for m in retry] == ["system", "user", "assistant", "user"]
    assert "rejected" in retry[-1].content and "999" in retry[-1].content


def test_a_fixed_answer_on_retry_is_accepted(tinysoc: AppContext) -> None:
    provider = FakeProvider(
        [
            _reply("COMPARE resets to 0.", []),  # no citation: rejected
            _reply("COMPARE resets to 0.", ["doc/specs/TINY_TIMER_MAS.md:60"]),
        ]
    )
    result = _ask(tinysoc, provider)
    assert result.status == "answered" and result.attempts == 2
    assert result.answer is not None
    assert result.answer.citations == ("doc/specs/TINY_TIMER_MAS.md:60",)


def test_an_unknown_answer_is_kept(tinysoc: AppContext) -> None:
    provider = FakeProvider([_reply("I don't know: no UART in the sources.", [], unknown=True)])
    result = _ask(tinysoc, provider, "What is the baud rate of the tinysoc UART?")
    assert result.status == "unknown"
    assert result.answer is not None and result.answer.unknown and result.answer.citations == ()


def test_no_source_means_unknown_without_calling_the_model(tinysoc: AppContext) -> None:
    provider = FakeProvider([])  # any call would raise: the script is empty
    result = _ask(tinysoc, provider, "zebracorn quux?")
    assert result.status == "unknown" and result.attempts == 0
    assert result.answer is not None and result.answer.unknown
    assert provider.requests == []


def test_a_reply_that_is_not_json_is_rejected(tinysoc: AppContext) -> None:
    provider = FakeProvider(["COMPARE resets to 0.", "still prose"])
    result = _ask(tinysoc, provider)
    assert result.status == "rejected"
    assert result.check is not None and "not a JSON object" in result.check.reasons[0]


def test_parse_answer_accepts_fenced_json_and_ignores_extra_keys() -> None:
    fenced = '```json\n{"answer": "x", "citations": ["chip.yml:9"], "unknown": false, "n": 1}\n```'
    answer, error = parse_answer(fenced)
    assert error is None and answer is not None
    assert answer.citations == ("chip.yml:9",) and not answer.unknown
    assert parse_answer('Here: {"answer": "y"} done')[0] is not None
    for bad in ("", "[1, 2]", '{"answer": 3}', '{"answer": "x", "citations": "chip.yml:9"}'):
        assert parse_answer(bad)[0] is None, bad
    assert parse_answer('{"answer": "x", "unknown": "no"}')[1] == "'unknown' must be true or false"


def _profile(providers: dict[str, dict[str, str]]) -> Profile:
    return Profile(project="p", models=ModelsCfg(providers=providers))


def test_make_provider_from_the_profile() -> None:
    assert make_provider(_profile({})) is None
    assert isinstance(make_provider(_profile({"fake": {}})), FakeProvider)
    anthropic = make_provider(_profile({"anthropic": {}}))
    assert isinstance(anthropic, AnthropicProvider) and not anthropic.compatible
    two = _profile({"anthropic": {}, "glm": {"base_url": "https://x"}})
    with pytest.raises(AppError, match="--provider"):
        make_provider(two)
    glm = make_provider(two, "glm")
    assert isinstance(glm, AnthropicProvider) and glm.compatible
    with pytest.raises(AppError, match="no provider 'nope'"):
        make_provider(two, "nope")
    with pytest.raises(AppError, match="api_key_env"):
        make_provider(_profile({"anthropic": {"api_key_env": "sk-1"}}))


def test_run_ask_without_a_provider_returns_the_sources(tinysoc: AppContext) -> None:
    result = run_ask(tinysoc, QUESTION)
    assert result.status == "no_provider" and result.answer is None
    assert result.context.sources[0].citation == "model:register:timer.COMPARE"


def test_run_ask_with_a_provider_needs_the_small_tier(tmp_path: Path) -> None:
    ctx = ingested_tinysoc(tmp_path / "t", profile_extra="models:\n  providers:\n    fake: {}\n")
    with pytest.raises(AppError, match=r"models\.tiers\.small"):
        run_ask(ctx, QUESTION)

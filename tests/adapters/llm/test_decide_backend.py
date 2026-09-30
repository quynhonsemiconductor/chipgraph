"""`LlmDecideBackend` (M1-12): decide()'s model tiers over an LLM provider, no network.

`FakeProvider` plays the model; every call goes through M1-10's `GuardedProvider`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from chipgraph.adapters.llm import (
    FakeProvider,
    GuardedProvider,
    Ledger,
    NdaBlocked,
    ProviderConfigError,
)
from chipgraph.adapters.llm.decide_backend import (
    SYSTEM_PROMPT,
    LlmDecideBackend,
    parse_answer,
    question_prompt,
)
from chipgraph.core.config.models import DataCfg, DecideCfg, ModelsCfg
from chipgraph.core.contracts import Decision
from chipgraph.core.engine.decide import DecisionLog, ModelBackend, Question, decide
from chipgraph.core.plugin_api.types import LlmRequest

MODELS = ModelsCfg(tiers={"small": "small-model-1", "large": "large-model-1"})


def _question(**kw: object) -> Question:
    return Question(
        id="triage.log7",
        prompt="Why did the lint run fail?",
        choices=("infra", "rtl", "tb", "spec"),
        context="%Error: rtl/tiny_timer.sv:3: syntax error, unexpected endmodule",
        **kw,  # type: ignore[arg-type]
    )


def _reply(value: str, confidence: float, reason: str = "r") -> str:
    return json.dumps({"value": value, "confidence": confidence, "reason": reason})


# --- parsing ----------------------------------------------------------------------------


def test_parse_good_json() -> None:
    parsed = parse_answer('{"value": "rtl", "confidence": 0.92, "reason": "syntax error"}')
    assert (parsed.value, parsed.confidence, parsed.reason) == ("rtl", 0.92, "syntax error")


def test_parse_fenced_json() -> None:
    text = 'Here is my answer:\n```json\n{"value": "tb", "confidence": 0.7}\n```\nThanks.'
    parsed = parse_answer(text)
    assert (parsed.value, parsed.confidence, parsed.reason) == ("tb", 0.7, "")


def test_parse_json_inside_prose() -> None:
    parsed = parse_answer('I think {"value": "spec", "confidence": 1} fits best.')
    assert (parsed.value, parsed.confidence) == ("spec", 1.0)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "rtl",
        "no idea, sorry",
        '{"value": ',
        "[1, 2, 3]",
        '{"confidence": 0.9}',
        '{"value": 3, "confidence": 0.9}',
        "```json\nnot json\n```",
    ],
)
def test_parse_garbage_is_no_value_and_zero(text: str) -> None:
    parsed = parse_answer(text)
    assert (parsed.value, parsed.confidence) == (None, 0.0)


@pytest.mark.parametrize("confidence", ['"high"', "1.5", "-0.2", "true", "null", "NaN"])
def test_parse_bad_confidence_is_zero(confidence: str) -> None:
    parsed = parse_answer(f'{{"value": "rtl", "confidence": {confidence}}}')
    assert (parsed.value, parsed.confidence) == ("rtl", 0.0)


# --- requests ---------------------------------------------------------------------------


def test_the_request_asks_the_tier_model_for_json() -> None:
    backend = LlmDecideBackend(FakeProvider(), MODELS)
    request = backend.request(_question(labels=("internal",)), "large")
    assert request.model == "large-model-1"
    assert request.labels == ("internal",)
    system, user = request.messages
    assert (system.role, system.content) == ("system", SYSTEM_PROMPT)
    assert user.role == "user"
    assert user.content == question_prompt(_question())
    assert '"rtl"' in user.content and "unexpected endmodule" in user.content
    assert '"confidence"' in SYSTEM_PROMPT


def test_a_tier_without_a_model_is_a_config_error() -> None:
    backend = LlmDecideBackend(FakeProvider(), ModelsCfg(tiers={"small": "s"}))
    with pytest.raises(ProviderConfigError, match=r"models\.tiers\.large"):
        asyncio.run(backend.ask(_question(), "large"))


def test_a_bare_provider_is_guarded_and_a_guarded_one_kept() -> None:
    bare = LlmDecideBackend(FakeProvider(), MODELS)
    assert isinstance(bare.provider, GuardedProvider)
    guarded = GuardedProvider(FakeProvider(), ledger=Ledger())
    assert LlmDecideBackend(guarded, MODELS).provider is guarded
    assert isinstance(bare, ModelBackend)


def test_ask_returns_value_confidence_and_serving_model() -> None:
    fake = FakeProvider([_reply("rtl", 0.9, "syntax")], model="served-model")
    answer = asyncio.run(LlmDecideBackend(fake, MODELS).ask(_question(), "small"))
    assert (answer.value, answer.confidence, answer.reason) == ("rtl", 0.9, "syntax")
    assert answer.model == "served-model"
    assert fake.requests[0].model == "small-model-1"


# --- through decide() -------------------------------------------------------------------


def _decide(backend: LlmDecideBackend, tmp_path: Path, cfg: DecideCfg | None = None) -> object:
    log = DecisionLog(tmp_path / "decisions.jsonl")
    return asyncio.run(decide(_question(), backend=backend, cfg=cfg, log=log))


def test_small_confident_through_the_provider(tmp_path: Path) -> None:
    fake = FakeProvider([_reply("rtl", 0.95)])
    decision = _decide(LlmDecideBackend(fake, MODELS), tmp_path)
    assert decision == Decision(
        question_id="triage.log7", value="rtl", confidence=0.95, backend="small"
    )
    assert [r.model for r in fake.requests] == ["small-model-1"]


def test_small_unsure_then_large_through_the_provider(tmp_path: Path) -> None:
    def reply(request: LlmRequest) -> str:
        if request.model == "small-model-1":
            return "```json\n" + _reply("tb", 0.4) + "\n```"
        return _reply("rtl", 0.9)

    fake = FakeProvider(reply)
    decision = _decide(LlmDecideBackend(fake, MODELS), tmp_path)
    assert decision == Decision(
        question_id="triage.log7", value="rtl", confidence=0.9, backend="large"
    )
    assert [r.model for r in fake.requests] == ["small-model-1", "large-model-1"]
    entries = DecisionLog(tmp_path / "decisions.jsonl").read()
    assert [(e.event, e.model) for e in entries] == [
        ("escalated", "small-model-1"),
        ("decided", "large-model-1"),
    ]


def test_garbage_from_small_escalates(tmp_path: Path) -> None:
    fake = FakeProvider(["I cannot tell.", _reply("infra", 0.8)])
    decision = _decide(LlmDecideBackend(fake, MODELS), tmp_path)
    assert isinstance(decision, Decision)
    assert (decision.value, decision.backend) == ("infra", "large")


def test_nda_question_is_refused_by_a_cloud_provider(tmp_path: Path) -> None:
    fake = FakeProvider([_reply("rtl", 1.0)])
    backend = LlmDecideBackend(fake, MODELS)
    log = DecisionLog(tmp_path / "decisions.jsonl")
    with pytest.raises(NdaBlocked):
        asyncio.run(decide(_question(labels=("nda",)), backend=backend, log=log))
    assert fake.requests == []  # refused by the guard, before the provider saw it
    [entry] = log.read()
    assert entry.event == "error" and entry.error is not None and "NdaBlocked" in entry.error


def test_nda_question_goes_to_a_local_provider_when_allowed(tmp_path: Path) -> None:
    fake = FakeProvider([_reply("rtl", 1.0)], local=True)
    backend = LlmDecideBackend(fake, MODELS, data=DataCfg(nda_model="local"))
    log = DecisionLog(tmp_path / "decisions.jsonl")
    decision = asyncio.run(decide(_question(labels=("nda",)), backend=backend, log=log))
    assert isinstance(decision, Decision) and decision.value == "rtl"
    assert fake.requests[0].labels == ("nda",)
    blocked = LlmDecideBackend(FakeProvider(local=True), MODELS, data=DataCfg())
    with pytest.raises(NdaBlocked):
        asyncio.run(decide(_question(labels=("nda",)), backend=blocked, log=log))

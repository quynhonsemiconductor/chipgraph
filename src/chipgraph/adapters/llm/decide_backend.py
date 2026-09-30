"""`LlmDecideBackend`: `decide()`'s model tiers over an `LlmProvider` (API runtime).

Each question becomes one request, to the model the profile's `models.tiers` names for
the tier, asking for strict JSON::

    {"value": "<one of the choices>", "confidence": 0.0-1.0, "reason": "<one sentence>"}

The reply is read tolerantly (`parse_answer`): the JSON may be alone, in a fenced code
block, or inside other text; anything unreadable is an answer with no value and
confidence 0, which `decide()` escalates.

Every call goes through a `GuardedProvider` (a bare provider is wrapped in one), so the
'nda' rule, the run budget and retry apply: a question labeled 'nda' is refused before
any call unless the provider is local.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from chipgraph.adapters.llm._errors import ProviderConfigError
from chipgraph.adapters.llm.guard import GuardedProvider
from chipgraph.core.config.models import DataCfg, DecideTier, ModelsCfg
from chipgraph.core.engine.decide import ModelAnswer, Question
from chipgraph.core.plugin_api.protocols import LlmProvider
from chipgraph.core.plugin_api.types import LlmMessage, LlmRequest

DEFAULT_MAX_TOKENS = 300
"""Output tokens allowed for one answer: a short JSON object."""

SYSTEM_PROMPT = """\
You answer one multiple-choice question for a chip design tool.
Reply with a single JSON object and nothing else:
{"value": "<exactly one of the choices>", "confidence": <number from 0 to 1>, \
"reason": "<one short sentence>"}
- "value" must be copied exactly from the choices.
- "confidence" is how sure you are that "value" is right: 1 is certain, 0.5 is a guess.
- Use only the question and its context; if they do not tell, pick the most likely
  choice and give a low confidence."""
"""The system message of every decide request."""

_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)


@dataclass(frozen=True, slots=True)
class ParsedAnswer:
    """A model reply as read by `parse_answer`: `value` None when it had none."""

    value: str | None
    confidence: float
    reason: str


def question_prompt(question: Question) -> str:
    """The user message for `question`: the question, its choices, and its context."""
    choices = "\n".join(f"- {json.dumps(c, ensure_ascii=False)}" for c in question.choices)
    parts = [f"Question: {question.prompt}", f"Choices:\n{choices}"]
    if question.context:
        parts.append(f"Context:\n<<<\n{question.context}\n>>>")
    parts.append("Answer with the JSON object only.")
    return "\n\n".join(parts)


def _objects(text: str) -> list[object]:
    """JSON values found in `text`: the whole text, fenced blocks, then each `{...}`."""
    found: list[object] = []
    candidates = [text.strip(), *(m.group(1).strip() for m in _FENCE.finditer(text))]
    for candidate in candidates:
        try:
            found.append(json.loads(candidate))
        except ValueError:
            continue
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        found.append(value)
    return found


def _confidence(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    if not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return value


def parse_answer(text: str) -> ParsedAnswer:
    """Read `{"value", "confidence", "reason"}` from a model reply, tolerantly.

    The first JSON object with a string `value` wins. A missing or out-of-range
    `confidence` makes it 0; a reply with no such object is `value=None`, confidence 0.
    """
    for obj in _objects(text):
        if not isinstance(obj, dict) or not isinstance(obj.get("value"), str):
            continue
        confidence = _confidence(obj.get("confidence"))
        reason = obj.get("reason")
        return ParsedAnswer(
            value=obj["value"],
            confidence=confidence if confidence is not None else 0.0,
            reason=reason if isinstance(reason, str) else "",
        )
    return ParsedAnswer(value=None, confidence=0.0, reason="")


class LlmDecideBackend:
    """A `ModelBackend` for `decide()` that asks an LLM provider for structured JSON.

    `models.tiers[tier]` names the model per tier (`small`, `large`); a tier with no
    model is a `ProviderConfigError`. `provider` is wrapped in a `GuardedProvider`
    (with `data`, the profile's `data` section) unless it already is one.
    """

    name = "llm"

    def __init__(
        self,
        provider: LlmProvider,
        models: ModelsCfg,
        *,
        data: DataCfg | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.provider = (
            provider
            if isinstance(provider, GuardedProvider)
            else GuardedProvider(provider, data=data)
        )
        self.models = models
        self.max_tokens = max_tokens

    def model_for(self, tier: DecideTier) -> str:
        """The model id for `tier`, from `models.tiers`."""
        model = self.models.tiers.get(tier)
        if not model:
            raise ProviderConfigError(
                f"models.tiers.{tier} is not set: decide() needs a model for the {tier!r} tier"
            )
        return model

    def request(self, question: Question, tier: DecideTier) -> LlmRequest:
        """The request `ask` sends for `question` to `tier`."""
        return LlmRequest(
            model=self.model_for(tier),
            messages=(
                LlmMessage(role="system", content=SYSTEM_PROMPT),
                LlmMessage(role="user", content=question_prompt(question)),
            ),
            max_tokens=self.max_tokens,
            labels=question.labels,
        )

    async def ask(self, question: Question, tier: DecideTier) -> ModelAnswer:
        response = await self.provider.complete(self.request(question, tier))
        parsed = parse_answer(response.text)
        return ModelAnswer(
            value=parsed.value,
            confidence=parsed.confidence,
            model=response.model,
            reason=parsed.reason,
        )


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "SYSTEM_PROMPT",
    "LlmDecideBackend",
    "ParsedAnswer",
    "parse_answer",
    "question_prompt",
]

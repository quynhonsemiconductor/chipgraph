"""The API-runtime answerer behind `chipgraph ask`: retrieve, ask a model, check, retry.

`answer_question` runs the same contract as the Claude Code path (`/chipgraph:ask`): the
model sees only the sources `ask_context` returned, must reply with an `AskAnswer` JSON
object, and the reply goes through `ask_check`. A rejected reply is sent back once with the
reasons; an answer that still fails is never shown: the result is "I don't know" with the
reasons. With no source at all the model is not called.

The provider comes from the profile's `models.providers` (`make_provider`), wrapped in a
`GuardedProvider` (the `nda` rule, retry, accounting); the model id is `models.tiers.small`.
With no provider configured, `run_ask` returns the retrieved sources only (`no_provider`).
"""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.adapters.llm import AnthropicProvider, FakeProvider, GuardedProvider, LlmError
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.core.config.models import Profile
from chipgraph.core.contracts.types import DataLabel
from chipgraph.core.plugin_api.protocols import LlmProvider
from chipgraph.core.plugin_api.types import LlmMessage, LlmRequest
from chipgraph.packs.assist.ask._project import AskProject
from chipgraph.packs.assist.ask.check import check_answer
from chipgraph.packs.assist.ask.contract import (
    UNKNOWN_ANSWER,
    AskAnswer,
    AskCheck,
    AskContext,
)
from chipgraph.packs.assist.ask.retrieve import DEFAULT_LIMIT, retrieve

MAX_ATTEMPTS = 2
"""The first reply, plus one retry with the checker's reasons."""

MAX_TOKENS = 1024

AskStatus = Literal["answered", "unknown", "rejected", "no_provider"]

SYSTEM_PROMPT = """\
You answer one question about a chip design project, using ONLY the sources you are given
(entities of its Design Model and lines of its documents). Rules:

- Every fact in your answer must come from a source. Cite the sources you used, each by
  its exact `citation` string ('model:<key>' or 'path:line'), or by its `defined_at`.
- Never use outside knowledge and never guess a number, a name or a behaviour.
- If the sources do not answer the question, set "unknown" to true, say briefly what is
  missing in "answer", and cite nothing.

Reply with exactly one JSON object and nothing else:
{"answer": "<short answer>", "citations": ["<citation>", ...], "unknown": false}
"""


class AskResult(BaseModel):
    """What `chipgraph ask` prints: the checked answer (or none), and the sources."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    status: AskStatus = Field(
        description=(
            "'answered' or 'unknown' (a verified answer), 'rejected' (no reply passed the "
            "check: the answer is 'I don't know'), 'no_provider' (sources only)."
        )
    )
    question: str
    answer: AskAnswer | None = Field(default=None, description="The verified answer.")
    check: AskCheck | None = Field(default=None, description="The last check that ran.")
    attempts: int = Field(default=0, description="Model calls made.")
    context: AskContext = Field(description="The retrieved sources.")
    provider: str | None = Field(default=None, description="The provider used, if any.")
    model: str | None = Field(default=None, description="The model id used, if any.")


def make_provider(profile: Profile, name: str | None = None) -> LlmProvider | None:
    """The `LlmProvider` configured in `models.providers`, or None when there is none.

    `name` picks one provider; it is required when several are configured. `anthropic`
    is the Anthropic API, `fake` the scripted test provider, and any other name an
    Anthropic-compatible endpoint (`base_url` in its config).
    """
    providers = profile.models.providers
    if name is None:
        if not providers:
            return None
        if len(providers) > 1:
            raise AppError(
                f"several providers in models.providers ({', '.join(sorted(providers))}); "
                "pick one with --provider"
            )
        [name] = providers
    elif name not in providers:
        known = ", ".join(sorted(providers)) or "none"
        raise AppError(f"no provider {name!r} in models.providers (configured: {known})")
    if name == "fake":
        return FakeProvider(name="fake")
    try:
        return AnthropicProvider.from_models(profile.models, name)
    except LlmError as exc:
        raise AppError(str(exc)) from exc


def run_ask(
    ctx: AppContext,
    question: str,
    *,
    limit: int = DEFAULT_LIMIT,
    provider_name: str | None = None,
) -> AskResult:
    """`chipgraph ask`: answer with the profile's provider, or return the sources only."""
    profile = ctx.require_profile().profile
    project = AskProject.load(ctx)
    provider = make_provider(profile, provider_name)
    if provider is None:
        return AskResult(
            status="no_provider", question=question, context=retrieve(project, question, limit)
        )
    model = profile.models.tiers.get("small")
    if not model:
        raise AppError(
            f"provider {provider.name!r} is configured but models.tiers.small is not: set the "
            "model id /ask should use"
        )
    guarded = GuardedProvider(provider, data=profile.data)
    return asyncio.run(answer_question(project, question, guarded, model=model, limit=limit))


async def answer_question(
    project: AskProject,
    question: str,
    provider: LlmProvider,
    *,
    model: str,
    limit: int = DEFAULT_LIMIT,
) -> AskResult:
    """Retrieve, ask `provider`, check the reply, retry once with the reasons."""
    context = retrieve(project, question, limit)

    def result(
        status: AskStatus, answer: AskAnswer, check: AskCheck | None, attempts: int
    ) -> AskResult:
        return AskResult(
            status=status,
            question=question,
            answer=answer,
            check=check,
            attempts=attempts,
            context=context,
            provider=provider.name,
            model=model,
        )

    if context.no_sources:
        return result("unknown", AskAnswer(answer=UNKNOWN_ANSWER, unknown=True), None, 0)

    messages = [
        LlmMessage(role="system", content=SYSTEM_PROMPT),
        LlmMessage(role="user", content=_user_prompt(context)),
    ]
    labels = _labels(project, context)
    check: AskCheck | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = LlmRequest(
            model=model, messages=tuple(messages), max_tokens=MAX_TOKENS, labels=labels
        )
        try:
            response = await provider.complete(request)
        except LlmError as exc:
            raise AppError(f"/ask: the model call failed: {exc}") from exc
        parsed, parse_error = parse_answer(response.text)
        if parsed is None:
            check = AskCheck(ok=False, reasons=(parse_error or "not an answer object",))
        else:
            check = check_answer(project, parsed)
        if check.ok and check.answer is not None:
            status: AskStatus = "unknown" if check.answer.unknown else "answered"
            return result(status, check.answer, check, attempt)
        messages.append(LlmMessage(role="assistant", content=response.text))
        messages.append(LlmMessage(role="user", content=_retry_prompt(check)))

    fallback = AskAnswer(
        answer="I don't know: no answer could be verified against this project's sources.",
        unknown=True,
    )
    return result("rejected", fallback, check, MAX_ATTEMPTS)


def parse_answer(text: str) -> tuple[AskAnswer | None, str | None]:
    """An `AskAnswer` from a model reply (a JSON object, maybe fenced), or the error."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else ""
        raw = raw.rsplit("```", 1)[0]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        return None, "the reply is not a JSON object"
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        return None, f"the reply is not valid JSON: {exc.msg}"
    if not isinstance(data, dict):
        return None, "the reply is not a JSON object"
    answer = data.get("answer", "")
    citations = data.get("citations", [])
    unknown = data.get("unknown", False)
    if not isinstance(answer, str):
        return None, "'answer' must be a string"
    if not isinstance(citations, list) or not all(isinstance(c, str) for c in citations):
        return None, "'citations' must be a list of strings"
    if not isinstance(unknown, bool):
        return None, "'unknown' must be true or false"
    return AskAnswer(answer=answer, citations=tuple(citations), unknown=unknown), None


def _user_prompt(context: AskContext) -> str:
    sources = [s.model_dump(mode="json", exclude_defaults=True) for s in context.sources]
    return (
        f"Question: {context.question}\n\n"
        f"Sources ({len(sources)}), best first:\n"
        f"{json.dumps(sources, indent=1)}\n\n"
        f"{context.note}"
    )


def _retry_prompt(check: AskCheck) -> str:
    reasons = "\n".join(f"- {r}" for r in check.reasons)
    return (
        "Your answer was rejected by the citation check:\n"
        f"{reasons}\n"
        "Reply again with one JSON object. Cite only `citation` or `defined_at` strings from "
        "the sources above; if they do not answer the question, set unknown to true."
    )


def _labels(project: AskProject, context: AskContext) -> tuple[DataLabel, ...]:
    """The data labels of every file behind the sources (never 'nda': those are excluded)."""
    files: set[str] = set()
    for source in context.sources:
        for citation in (source.citation, source.defined_at):
            if citation and not citation.startswith("model:"):
                files.add(citation.rsplit(":", 1)[0])
        if source.kind == "model":
            entity = project.model.get(source.citation.removeprefix("model:"))
            if entity is not None and entity.source.file:
                files.add(entity.source.file)
    labels = {project.labels.label_for(f) for f in files} or {project.labels.default}
    return tuple(sorted(labels))


__all__ = [
    "MAX_ATTEMPTS",
    "SYSTEM_PROMPT",
    "AskResult",
    "AskStatus",
    "answer_question",
    "make_provider",
    "parse_answer",
    "run_ask",
]

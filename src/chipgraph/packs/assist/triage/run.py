"""`run_triage`: classify a failing log with `decide()` and build the `TriageReport`.

1. parse the log (`parse.parse_log`): issues, check results, simulation failures, excerpt;
2. ask `decide()` the question `question.build_question` builds, with the deterministic
   `rules` first and the model tiers after them;
3. turn the answer into a report: the deterministic summary and suggestion (`report`).

The model backend follows the profile's runtime: `claude-code` queues the question for
the `chipgraph:decider` subagent of the user's Claude Code session (the report is then
`deferred`: run `/chipgraph:triage`, which runs the decider loop and triages again);
an API runtime asks the `models.providers` provider through `LlmDecideBackend`. With no
backend only the rules answer, and a log no rule classifies is `undecided`.

A model's label is advice (DESIGN 4.8): it never blocks, and an answer under its tier's
threshold is marked `low_confidence`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal, cast

from chipgraph.adapters.llm import LlmError
from chipgraph.adapters.llm.decide_backend import LlmDecideBackend
from chipgraph.adapters.runtime.claude_code.decisions import ClaudeCodeDecideBackend
from chipgraph.app.context import AppContext
from chipgraph.core.config.models import Profile
from chipgraph.core.contracts.types import DataLabel
from chipgraph.core.engine.decide import (
    DecideError,
    DecisionLog,
    Deferred,
    ModelBackend,
    decide,
    low_confidence,
)
from chipgraph.packs.assist.ask.answer import make_provider
from chipgraph.packs.assist.triage.contract import LABELS, SpecLine, TriageLabel, TriageReport
from chipgraph.packs.assist.triage.parse import ParsedLog, parse_log
from chipgraph.packs.assist.triage.question import build_question, spec_lines, spec_sources
from chipgraph.packs.assist.triage.report import evidence, suggestion, summary
from chipgraph.packs.assist.triage.rules import (
    RuleHit,
    TriageFacts,
    decide_rules,
    is_rtl_path,
    is_tb_path,
    tb_patterns,
)

MAX_REPORT_ISSUES = 50

BackendChoice = ModelBackend | None | Literal["auto"]
"""`"auto"`: the profile's runtime decides; `None`: rules only; or a given backend."""


def default_backend(ctx: AppContext) -> ModelBackend | None:
    """The model backend for the profile's runtime, or None when there is none."""
    profile = ctx.require_profile().profile
    if profile.runtime == "claude-code":
        return ClaudeCodeDecideBackend(ctx.layout, profile.models)
    provider = make_provider(profile)
    if provider is None:
        return None
    return LlmDecideBackend(provider, profile.models, data=profile.data)


def _check_kinds(profile: Profile) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((check_id, cfg.use) for check_id, cfg in profile.adapters.items()))


def _check_id(parsed: ParsedLog, given: str | None) -> str | None:
    if given:
        return given
    ids = {c.check_id for c in parsed.failing_checks}
    return ids.pop() if len(ids) == 1 else None


def _source_label(ctx: AppContext, source: str | None) -> DataLabel:
    labels = ctx.store.labels
    if source is None or source == "-":
        return labels.default
    path = Path(source)
    if path.is_absolute():
        try:
            rel = path.resolve().relative_to(ctx.root.resolve()).as_posix()
        except ValueError:
            return labels.default
    else:
        rel = path.as_posix()
    return labels.label_for(rel)


def _logged(ctx: AppContext, question_id: str) -> tuple[str, str | None]:
    """The reason and model of the decision `decide()` logged last for `question_id`."""
    entries = [
        e
        for e in DecisionLog.for_layout(ctx.layout).read()
        if e.question_id == question_id and e.event == "decided"
    ]
    if not entries:
        return "", None
    return entries[-1].reason, entries[-1].model


def _spec_side(ctx: AppContext, parsed: ParsedLog, facts: TriageFacts) -> tuple[SpecLine, ...]:
    """For a `spec` label whose issues all point at RTL or testbenches (a port width the
    spec gets wrong is reported at the RTL port): the spec lines about those issues."""
    for issue in parsed.issues:
        path = issue.file
        if path and not is_rtl_path(path, facts.tb_patterns) and not is_tb_path(path):
            return ()
    query = " ".join(i.msg for i in parsed.issues[:2])
    specs, _ = spec_sources(ctx, query, facts, limit=3)
    return specs


async def triage_log(
    ctx: AppContext,
    log_text: str,
    *,
    source: str | None = None,
    check_id: str | None = None,
    backend: BackendChoice = "auto",
) -> TriageReport:
    """Classify `log_text` (a failing lint/sim/check log); see the module docstring."""
    profile = ctx.require_profile().profile
    parsed = parse_log(log_text)
    facts = TriageFacts(
        parsed=parsed,
        check_id=_check_id(parsed, check_id),
        check_kinds=_check_kinds(profile),
        tb_patterns=tb_patterns(profile.layout),
    )
    specs, spec_labels = spec_lines(ctx, parsed, facts)
    question = build_question(
        parsed, facts, specs=specs, labels=(_source_label(ctx, source), *spec_labels)
    )
    chosen = default_backend(ctx) if backend == "auto" else backend
    fired: list[RuleHit] = []
    base: dict[str, Any] = {
        "question_id": question.id,
        "check_id": facts.check_id,
        "source": source,
        "summary": summary(parsed),
        "issues": parsed.issues[:MAX_REPORT_ISSUES],
        "excerpt": parsed.excerpt,
        "spec_lines": specs,
    }

    try:
        outcome = await decide(
            question,
            rules=decide_rules(facts, fired),
            backend=chosen,
            cfg=profile.decide,
            log=DecisionLog.for_layout(ctx.layout),
        )
    except (DecideError, LlmError) as exc:
        if chosen is None:
            message = (
                "no rule classified this log and no model is configured: set "
                "models.providers (API runtime), or use runtime claude-code and run "
                "/chipgraph:triage in Claude Code"
            )
        else:
            message = f"no model answered: {exc}"
        return TriageReport(status="undecided", message=message, **base)

    if isinstance(outcome, Deferred):
        where = f" {source}" if source and source != "-" else " <log>"
        message = (
            f"no rule classified this log; it waits for the {outcome.tier} model tier"
            f" ({outcome.model or 'its model'}). In Claude Code run /chipgraph:triage{where}: "
            "it answers the queued question with the decider subagent and triages again."
        )
        return TriageReport(
            status="deferred",
            model=outcome.model,
            message=message,
            **base,
        )

    label = cast(TriageLabel, outcome.value)
    assert label in LABELS
    if label == "spec" and not specs:
        specs = _spec_side(ctx, parsed, facts)
        base["spec_lines"] = specs
    hit = fired[-1] if outcome.backend == "rule" and fired else None
    if hit is not None:
        reason, model = hit.reason, None
    else:
        reason, model = _logged(ctx, question.id)
    return TriageReport(
        status="decided",
        label=label,
        backend=outcome.backend,
        rule=hit.rule if hit is not None else None,
        confidence=outcome.confidence,
        low_confidence=low_confidence(outcome, profile.decide),
        suggestion=suggestion(label, parsed, facts, hit, specs),
        retry_without_counting=label == "infra",
        ask_person=label == "spec",
        evidence=evidence(label, parsed, facts, hit, specs),
        reason=reason,
        model=model,
        **base,
    )


def run_triage(
    ctx: AppContext,
    log_text: str,
    *,
    source: str | None = None,
    check_id: str | None = None,
    backend: BackendChoice = "auto",
) -> TriageReport:
    """`chipgraph triage`: `triage_log` in its own event loop."""
    return asyncio.run(triage_log(ctx, log_text, source=source, check_id=check_id, backend=backend))


__all__ = ["BackendChoice", "default_backend", "run_triage", "triage_log"]

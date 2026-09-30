"""`decide()` (M1-12): rule -> small model -> large model by threshold, and its log.

No model: `Scripted` is a `ModelBackend` that answers from a per-tier script.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chipgraph.core.config import DecideCfg, DecideOverride
from chipgraph.core.config.models import DecideTier
from chipgraph.core.contracts import Decision
from chipgraph.core.engine.decide import (
    DecideError,
    DecisionLog,
    DecisionLogEntry,
    Deferred,
    ModelAnswer,
    ModelBackend,
    Question,
    Undecided,
    decide,
    decision_log_path,
    low_confidence,
    rule_decision,
)
from chipgraph.core.state.layout import StateLayout

CHOICES = ("infra", "rtl", "tb", "spec")


class Scripted:
    """Answers each tier from its script: a `ModelAnswer`, a `Deferred` or an exception."""

    name = "scripted"

    def __init__(self, **script: ModelAnswer | Deferred | Exception) -> None:
        self.script = script
        self.asked: list[DecideTier] = []

    async def ask(self, question: Question, tier: DecideTier) -> ModelAnswer | Deferred:
        self.asked.append(tier)
        reply = self.script[tier]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _question(qid: str = "triage.log1", **kw: object) -> Question:
    return Question(
        id=qid,
        prompt="Why did this run fail?",
        choices=CHOICES,
        context="%Error: tb_top.sv:12: syntax error",
        **kw,  # type: ignore[arg-type]
    )


def _answer(value: str | None, confidence: float, model: str = "m") -> ModelAnswer:
    return ModelAnswer(value=value, confidence=confidence, model=model, reason="because")


@pytest.fixture
def log(tmp_path: Path) -> DecisionLog:
    return DecisionLog.for_layout(StateLayout(tmp_path))


def _decide(
    question: Question,
    log: DecisionLog,
    backend: ModelBackend | None = None,
    cfg: DecideCfg | None = None,
    rules: tuple[object, ...] = (),
) -> Decision | Deferred:
    return asyncio.run(
        decide(question, rules=rules, backend=backend, cfg=cfg, log=log)  # type: ignore[arg-type]
    )


# --- the three paths ------------------------------------------------------------------


def test_a_rule_answers_first_and_no_model_is_asked(log: DecisionLog) -> None:
    backend = Scripted(small=_answer("rtl", 0.99))

    def passes(q: Question) -> Decision | None:
        return None

    def infra_rule(q: Question) -> Decision | None:
        return rule_decision(q, "infra") if "syntax" in q.context else None

    decision = _decide(_question(), log, backend, rules=(passes, infra_rule))
    assert decision == Decision(
        question_id="triage.log1", value="infra", confidence=1.0, backend="rule"
    )
    assert backend.asked == []
    [entry] = log.read()
    assert (entry.event, entry.backend, entry.tier, entry.value) == (
        "decided",
        "rule",
        None,
        "infra",
    )
    assert entry.confidence == 1.0


def test_a_confident_small_answer_is_final(log: DecisionLog) -> None:
    backend = Scripted(small=_answer("tb", 0.9, "small-model"), large=_answer("rtl", 1.0))
    decision = _decide(_question(), log, backend)
    assert decision == Decision(
        question_id="triage.log1", value="tb", confidence=0.9, backend="small"
    )
    assert backend.asked == ["small"]
    [entry] = log.read()
    assert entry.event == "decided"
    assert (entry.backend, entry.tier, entry.model) == ("small", "small", "small-model")
    assert entry.threshold == 0.8
    assert not entry.low_confidence


def test_an_unsure_small_answer_escalates_to_large(log: DecisionLog) -> None:
    backend = Scripted(small=_answer("tb", 0.6, "s"), large=_answer("rtl", 0.95, "l"))
    decision = _decide(_question(), log, backend)
    assert decision == Decision(
        question_id="triage.log1", value="rtl", confidence=0.95, backend="large"
    )
    assert backend.asked == ["small", "large"]
    small, large = log.read()
    assert (small.event, small.tier, small.value, small.confidence) == (
        "escalated",
        "small",
        "tb",
        0.6,
    )
    assert (large.event, large.tier, large.value, large.model) == ("decided", "large", "rtl", "l")
    assert large.threshold == 0.5


def test_an_unsure_large_answer_is_returned_but_logged_low(log: DecisionLog) -> None:
    backend = Scripted(small=_answer("tb", 0.3), large=_answer("rtl", 0.4))
    cfg = DecideCfg()
    decision = _decide(_question(), log, backend, cfg)
    assert isinstance(decision, Decision)
    assert (decision.value, decision.backend, decision.confidence) == ("rtl", "large", 0.4)
    assert low_confidence(decision, cfg)
    events = [(e.event, e.tier, e.low_confidence) for e in log.read()]
    assert events == [
        ("escalated", "small", False),
        ("rejected", "large", False),
        ("decided", "large", True),
    ]


def test_the_best_valid_answer_wins_when_large_is_invalid(log: DecisionLog) -> None:
    backend = Scripted(small=_answer("tb", 0.6), large=_answer("not-a-choice", 0.99))
    decision = _decide(_question(), log, backend)
    assert isinstance(decision, Decision)
    assert (decision.value, decision.backend, decision.confidence) == ("tb", "small", 0.6)
    last = log.read()[-1]
    assert (last.event, last.backend, last.low_confidence) == ("decided", "small", True)


# --- invalid answers ------------------------------------------------------------------


def test_an_answer_outside_the_choices_counts_as_zero_and_escalates(log: DecisionLog) -> None:
    backend = Scripted(small=_answer("timing", 1.0), large=_answer("spec", 0.7))
    decision = _decide(_question(), log, backend)
    assert isinstance(decision, Decision)
    assert (decision.value, decision.backend) == ("spec", "large")
    small = log.read()[0]
    assert small.event == "escalated"
    assert small.value is None
    assert small.answer == "timing"
    assert small.confidence == 0.0


def test_an_unreadable_answer_escalates(log: DecisionLog) -> None:
    backend = Scripted(small=_answer(None, 0.0), large=_answer("infra", 0.9))
    decision = _decide(_question(), log, backend)
    assert isinstance(decision, Decision) and decision.value == "infra"


def test_a_choice_matches_ignoring_case_and_spaces(log: DecisionLog) -> None:
    decision = _decide(_question(), log, Scripted(small=_answer("  RTL ", 0.9)))
    assert isinstance(decision, Decision) and decision.value == "rtl"


def test_no_valid_answer_at_all_is_undecided(log: DecisionLog) -> None:
    backend = Scripted(small=_answer("x", 1.0), large=_answer("y", 1.0))
    with pytest.raises(Undecided) as info:
        _decide(_question(), log, backend)
    assert info.value.question_id == "triage.log1"
    assert [e.event for e in log.read()] == ["escalated", "rejected", "undecided"]


# --- thresholds and tiers from the profile --------------------------------------------


def test_the_small_threshold_is_configurable(log: DecisionLog) -> None:
    backend = Scripted(small=_answer("tb", 0.6), large=_answer("rtl", 0.9))
    decision = _decide(_question(), log, backend, DecideCfg(small_min_confidence=0.5))
    assert isinstance(decision, Decision) and decision.backend == "small"
    assert log.read()[0].threshold == 0.5


def test_the_large_threshold_is_configurable(log: DecisionLog) -> None:
    backend = Scripted(small=_answer("tb", 0.1), large=_answer("rtl", 0.6))
    cfg = DecideCfg(large_min_confidence=0.7)
    decision = _decide(_question(), log, backend, cfg)
    assert isinstance(decision, Decision) and low_confidence(decision, cfg)
    assert log.read()[-1].low_confidence
    assert not low_confidence(decision, DecideCfg())


def test_a_prefix_override_applies_to_its_questions_only(log: DecisionLog) -> None:
    cfg = DecideCfg(overrides={"triage.": DecideOverride(small_min_confidence=0.95)})
    triage = Scripted(small=_answer("tb", 0.9), large=_answer("rtl", 0.9))
    other = Scripted(small=_answer("tb", 0.9), large=_answer("rtl", 0.9))
    first = _decide(_question("triage.a"), log, triage, cfg)
    second = _decide(_question("risk.a"), log, other, cfg)
    assert isinstance(first, Decision) and first.backend == "large"
    assert isinstance(second, Decision) and second.backend == "small"


def test_a_disabled_large_tier_returns_the_small_answer_as_low(log: DecisionLog) -> None:
    backend = Scripted(small=_answer("tb", 0.6), large=_answer("rtl", 1.0))
    cfg = DecideCfg(enabled_tiers=("small",))
    decision = _decide(_question(), log, backend, cfg)
    assert isinstance(decision, Decision)
    assert (decision.value, decision.backend) == ("tb", "small")
    assert backend.asked == ["small"]
    assert low_confidence(decision, cfg)
    assert [(e.event, e.low_confidence) for e in log.read()] == [
        ("rejected", False),
        ("decided", True),
    ]


def test_only_the_large_tier(log: DecisionLog) -> None:
    backend = Scripted(small=_answer("tb", 1.0), large=_answer("rtl", 0.9))
    decision = _decide(_question(), log, backend, DecideCfg(enabled_tiers=("large",)))
    assert isinstance(decision, Decision) and decision.backend == "large"
    assert backend.asked == ["large"]


def test_no_tier_and_no_rule_is_an_error(log: DecisionLog) -> None:
    with pytest.raises(DecideError, match="every model tier is disabled"):
        _decide(_question(), log, Scripted(), DecideCfg(enabled_tiers=()))
    with pytest.raises(DecideError, match="no model backend"):
        _decide(_question(), log, None)


def test_rules_still_answer_with_every_tier_disabled(log: DecisionLog) -> None:
    decision = _decide(
        _question(),
        log,
        None,
        DecideCfg(enabled_tiers=()),
        rules=(lambda q: rule_decision(q, "spec", 0.7),),
    )
    assert isinstance(decision, Decision)
    assert (decision.value, decision.confidence, decision.backend) == ("spec", 0.7, "rule")


# --- deferral and errors --------------------------------------------------------------


def test_a_deferred_tier_is_returned_and_logged(log: DecisionLog) -> None:
    later = Deferred(question_id="triage.log1", tier="small", model="small-alias")
    backend = Scripted(small=later)
    assert _decide(_question(), log, backend) == later
    [entry] = log.read()
    assert (entry.event, entry.tier, entry.model) == ("deferred", "small", "small-alias")


def test_a_backend_error_is_logged_and_raised(log: DecisionLog) -> None:
    backend = Scripted(small=PermissionError("refused: nda"))
    with pytest.raises(PermissionError):
        _decide(_question(labels=("nda",)), log, backend)
    [entry] = log.read()
    assert entry.event == "error"
    assert entry.error is not None and "refused: nda" in entry.error


def test_a_rule_must_answer_its_own_question_with_a_choice(log: DecisionLog) -> None:
    def wrong_value(q: Question) -> Decision:
        return rule_decision(q, "timing")

    def wrong_question(q: Question) -> Decision:
        return Decision(question_id="other", value="rtl", confidence=1.0, backend="rule")

    def wrong_backend(q: Question) -> Decision:
        return Decision(question_id=q.id, value="rtl", confidence=1.0, backend="small")

    for rule, message in (
        (wrong_value, "not one of its choices"),
        (wrong_question, "answered question 'other'"),
        (wrong_backend, "backend 'small'"),
    ):
        with pytest.raises(DecideError, match=message):
            _decide(_question(), log, None, rules=(rule,))


# --- the question and the log ---------------------------------------------------------


def test_question_validation() -> None:
    with pytest.raises(ValueError):
        Question(id="q", prompt="p", choices=())
    with pytest.raises(ValueError, match="distinct"):
        Question(id="q", prompt="p", choices=("a", "A"))
    with pytest.raises(ValueError, match="empty"):
        Question(id="q", prompt="p", choices=("a", " "))
    q = Question(id="q", prompt="p", choices=("yes", "no"))
    assert q.match("Yes") == "yes"
    assert q.match("maybe") is None
    assert q.match(1) is None
    assert q.fingerprint() != q.model_copy(update={"context": "x"}).fingerprint()


def test_the_log_lives_in_the_state_dir_and_reads_back(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    path = decision_log_path(layout)
    assert path == tmp_path / ".chipgraph" / "state" / "decisions.jsonl"
    assert path.parent == layout.state_dir
    assert layout.decisions_dir not in path.parents
    log = DecisionLog(path)
    backend = Scripted(small=_answer("tb", 0.6, "s"), large=_answer("rtl", 0.9, "l"))
    _decide(_question(), log, backend)
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    entries = [DecisionLogEntry.model_validate_json(line) for line in lines]
    assert entries == log.read()
    for entry in entries:
        assert entry.question_id == "triage.log1"
        assert entry.duration_s >= 0
        assert entry.ts.tzinfo is not None
        assert entry.reason == "because"
    # the context (which may be sensitive) is never logged
    assert "syntax error" not in path.read_text()


def test_the_log_skips_a_partial_last_line(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decisions.jsonl")
    _decide(_question(), log, Scripted(small=_answer("tb", 0.9)))
    with open(log.path, "a", encoding="utf-8") as f:
        f.write('{"schema_version": 1, "quest')
    assert len(log.read()) == 1


def test_a_long_reason_is_cut(log: DecisionLog) -> None:
    long = ModelAnswer(value="tb", confidence=0.9, reason="x" * 5000)
    _decide(_question(), log, Scripted(small=long))
    assert len(log.read()[0].reason) == 500


def test_scripted_is_a_model_backend() -> None:
    assert isinstance(Scripted(), ModelBackend)
